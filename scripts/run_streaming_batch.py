#!/usr/bin/env python3
"""Resource-aware streaming batch orchestrator for the documentary workflow."""

import argparse
import concurrent.futures
import json
import math
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.request
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def required_models():
    manifest = json.loads((REPO / "model_manifest.json").read_text())
    return [entry["filename"] for entry in manifest["models_referenced_by_payload"]]


def parse_args():
    parser = argparse.ArgumentParser(description="Stream images, dual-GPU LTX, CPU rendering, QC, and Drive delivery across a batch.")
    parser.add_argument("manifest", help="Batch manifest JSON.")
    parser.add_argument("--ltx-workers", type=int, default=int(os.getenv("LTX_WORKERS", "2")))
    parser.add_argument("--comfy-urls", default=os.getenv("LTX_COMFY_URLS", "http://127.0.0.1:18188,http://127.0.0.1:18189"))
    parser.add_argument("--comfy-dirs", default=os.getenv("LTX_COMFY_DIRS", "/workspace/ComfyUI,/workspace/ComfyUI-gpu1"))
    parser.add_argument("--comfy-services", default=os.getenv("LTX_COMFY_SERVICES", "comfyui,comfyui-gpu1"))
    parser.add_argument("--ltx-wave-per-worker", type=int, default=int(os.getenv("LTX_WAVE_PER_WORKER", "30")))
    parser.add_argument("--render-workers", type=int, default=int(os.getenv("RENDER_WORKERS", "6")))
    parser.add_argument("--project-root", default="/workspace/projects")
    parser.add_argument("--drive-remote", default=os.getenv("RCLONE_REMOTE", "gdrive"))
    parser.add_argument("--drive-folder", default=os.getenv("DRIVE_OUTPUT_DIR", "Japan_Project_Render_Workflow/final"))
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument("--plan", action="store_true", help="Validate inputs and print the execution plan without starting paid work.")
    return parser.parse_args()


class State:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS phases (
                    production TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    detail TEXT,
                    PRIMARY KEY (production, phase)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    production TEXT,
                    phase TEXT,
                    message TEXT NOT NULL
                );
            """)

    def connect(self):
        return sqlite3.connect(self.path)

    def set(self, production, phase, status, detail=None):
        with self.lock, self.connect() as db:
            db.execute(
                "INSERT INTO phases(production,phase,status,detail) VALUES(?,?,?,?) "
                "ON CONFLICT(production,phase) DO UPDATE SET status=excluded.status, detail=excluded.detail, updated_at=CURRENT_TIMESTAMP",
                (production, phase, status, json.dumps(detail, ensure_ascii=False) if detail is not None else None),
            )
            db.execute("INSERT INTO events(production,phase,message) VALUES(?,?,?)", (production, phase, status))


def run(command, *, log=None, env=None):
    print("[exec]", " ".join(map(str, command)), flush=True)
    merged = os.environ.copy()
    if env:
        merged.update(env)
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as output:
            subprocess.run(command, check=True, env=merged, stdout=output, stderr=subprocess.STDOUT, text=True)
    else:
        subprocess.run(command, check=True, env=merged)


def api_ready(url):
    try:
        with urllib.request.urlopen(f"{url}/system_stats", timeout=5) as response:
            return response.status == 200
    except Exception:
        return False


def registry_ready(url, required_models):
    try:
        with urllib.request.urlopen(f"{url}/object_info", timeout=30) as response:
            payload = response.read().decode("utf-8", errors="replace")
        return all(model in payload for model in required_models)
    except Exception:
        return False


def comfy_version(url):
    with urllib.request.urlopen(f"{url}/system_stats", timeout=10) as response:
        payload = json.load(response)
    return str((payload.get("system") or {}).get("comfyui_version") or "")


def version_tuple(value):
    try:
        return tuple(int(part) for part in value.split(".")[:3])
    except ValueError:
        return ()


def wait_apis(urls, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(api_ready(url) for url in urls):
            return
        time.sleep(3)
    raise RuntimeError(f"ComfyUI APIs did not recover: {urls}")


def wait_registries(urls, timeout=240):
    models = required_models()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(registry_ready(url, models) for url in urls):
            return
        time.sleep(3)
    raise RuntimeError(f"ComfyUI model registries did not recover: {urls}")


def restart_comfy(services, urls):
    if not services:
        return
    run(["supervisorctl", "restart", *services])
    wait_apis(urls)
    wait_registries(urls)
    print("[comfy] all APIs ready after controlled recycle", flush=True)


def normalize_productions(manifest, project_root):
    base = Path(manifest).resolve().parent
    data = json.loads(Path(manifest).read_text())
    productions = data.get("productions") or []
    if not productions:
        raise ValueError("manifest must contain a non-empty productions array")
    result = []
    for index, raw in enumerate(productions):
        shot_json = Path(raw["shot_json"])
        voice = Path(raw["voice"])
        if not shot_json.is_absolute(): shot_json = base / shot_json
        if not voice.is_absolute(): voice = base / voice
        name = raw.get("name") or shot_json.stem.removesuffix("_final")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            raise ValueError(f"unsafe production name: {name!r}")
        project = Path(raw.get("project") or Path(project_root) / name)
        if not project.is_absolute(): project = base / project
        drive_name = raw.get("drive_name") or f"{name}_final_video.mp4"
        if Path(drive_name).name != drive_name or not drive_name.lower().endswith(".mp4"):
            raise ValueError(f"unsafe drive_name: {drive_name!r}")
        result.append({
            "index": index,
            "name": name,
            "shot_json": shot_json.resolve(),
            "voice": voice.resolve(),
            "project": project,
            "drive_name": drive_name,
        })
    names = [production["name"] for production in result]
    projects = [str(production["project"].resolve()) for production in result]
    if len(names) != len(set(names)):
        raise ValueError("production names must be unique")
    if len(projects) != len(set(projects)):
        raise ValueError("production project paths must be unique")
    return result


def preflight(productions, urls, no_upload=False, drive_remote="gdrive"):
    tools = ["node", "python3", "ffmpeg", "ffprobe"] + ([] if no_upload else ["rclone"])
    missing_tools = [name for name in tools if shutil.which(name) is None]
    if missing_tools:
        raise RuntimeError(f"missing required tools: {missing_tools}")
    if not no_upload:
        subprocess.run(
            ["rclone", "lsd", f"{drive_remote}:", "--max-depth", "1"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=60,
        )
    models = required_models()
    for url in urls:
        version = comfy_version(url)
        if version_tuple(version) < (0, 32, 0):
            raise RuntimeError(f"ComfyUI >= 0.32.0 required at {url}; found {version or 'unknown'}")
        if not registry_ready(url, models):
            raise RuntimeError(f"ComfyUI registry is not ready with all required models: {url}")
    for production in productions:
        for key in ("shot_json", "voice"):
            if not production[key].is_file():
                raise FileNotFoundError(production[key])
        items = json.loads(production["shot_json"].read_text())
        if not isinstance(items, list) or not items:
            raise ValueError(f"{production['name']}: shot JSON must be a non-empty array")
        previous_end = None
        source_ids = []
        for runtime_id, item in enumerate(items, 1):
            source_ids.append(str(item.get("id", runtime_id)))
            if not str(item.get("shot") or "").strip():
                raise ValueError(f"{production['name']}: empty image prompt at runtime ID {runtime_id}")
            start = round(float(item["start"]) * 25)
            end = round(float(item["end"]) * 25)
            if (previous_end is None and start != 0) or (previous_end is not None and start != previous_end):
                raise ValueError(f"{production['name']}: non-contiguous timeline at runtime ID {runtime_id}")
            if end <= start:
                raise ValueError(f"{production['name']}: empty frame interval at runtime ID {runtime_id}")
            previous_end = end
        if len(source_ids) != len(set(source_ids)):
            raise ValueError(f"{production['name']}: duplicate source IDs")
        voice_duration = float(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(production["voice"])
        ], text=True).strip())
        delta = voice_duration - float(items[-1]["end"])
        if abs(delta) > 1.0:
            raise ValueError(f"{production['name']}: voice/JSON duration delta is {delta:.3f}s")


def bootstrap(production):
    project = production["project"]
    for folder in ("inputs", "comfy_workflows", "assets", "logs", "final"):
        (project / folder).mkdir(parents=True, exist_ok=True)
    input_dst = project / "inputs" / "shot_list.json"
    if production["shot_json"] != input_dst.resolve():
        shutil.copy2(production["shot_json"], input_dst)
    voice_dst = project / "inputs" / f"voice{production['voice'].suffix}"
    if production["voice"] != voice_dst.resolve():
        shutil.copy2(production["voice"], voice_dst)
    shutil.copy2(REPO / "comfy_workflows" / "ltx-2.5-nvfp4-i2v.payload.json", project / "comfy_workflows" / "ltx-2.5-nvfp4-i2v.payload.json")
    for name in ("grain.mp4", "keyboard-typing-sound-effect-335503.mp3", "YujiBoku-Regular.ttf"):
        shutil.copy2(REPO / "assets" / name, project / "assets" / name)
    production["input_json"] = project / "inputs" / "shot_list.json"
    production["voice_in"] = voice_dst
    production["items"] = json.loads(production["input_json"].read_text())


def image_producer(productions, state, ready_events):
    for production in productions:
        (production["project"] / "images.failed").unlink(missing_ok=True)
    # Fill the expensive GPU queue first across the entire batch. Static assets
    # follow only after every production has its LTX source images dispatched.
    for media_type in ("video", "static"):
        for production in productions:
            name = production["name"]
            phase = f"images_{media_type}"
            failure_file = production["project"] / "images.failed"
            state.set(name, phase, "running")
            try:
                run([
                    "node", str(REPO / "scripts" / "generate_images_from_shot_json.js"),
                    "--input", str(production["input_json"]),
                    "--output", str(production["project"] / "generated_images"),
                    "--media-type", media_type,
                ], log=production["project"] / "logs" / f"images_{media_type}.log", env={"NODE_PATH": str(REPO / "node_modules")})
                state.set(name, phase, "complete")
                if media_type == "static":
                    state.set(name, "images", "complete")
                    ready_events[name].set()
            except Exception as exc:
                failure_file.write_text(str(exc) + "\n")
                state.set(name, phase, "failed", {"error": str(exc)})
                state.set(name, "images", "failed", {"error": str(exc)})
                for blocked in productions:
                    blocked_failure = blocked["project"] / "images.failed"
                    if not blocked_failure.exists():
                        blocked_failure.write_text(f"batch image producer stopped after {name}: {exc}\n")
                    ready_events[blocked["name"]].set()
                raise


def static_renderer(production, state, image_ready):
    image_ready.wait()
    name = production["name"]
    failure_file = production["project"] / "images.failed"
    if failure_file.exists():
        raise RuntimeError(failure_file.read_text(errors="replace"))
    state.set(name, "static_clips", "running")
    run([
        "python3", str(REPO / "scripts" / "render_final_video.py"), "--optimized", "--clips-only", "--media-type", "static",
        "--workers", str(production["render_workers"]), "--render-root", str(production["project"] / "render_optimized"),
        "--project", str(production["project"]), "--input-json", str(production["input_json"]),
        "--images-dir", str(production["project"] / "generated_images"), "--ltx-dir", str(production["project"] / "ltx_videos"),
        "--voice", str(production["voice_in"]), "--output", str(production["project"] / "final" / "final_video.mp4"),
    ], log=production["project"] / "logs" / "static_clips.log")
    state.set(name, "static_clips", "complete")


def run_ltx_wave(production, wave, wave_index, args, urls, dirs):
    tasks = queue.Queue()
    for runtime_id, _duration in wave:
        tasks.put(runtime_id)

    def gpu_worker(worker):
        completed = 0
        log_path = production["project"] / "logs" / f"ltx_wave_{wave_index}_worker_{worker}.log"
        while True:
            try:
                runtime_id = tasks.get_nowait()
            except queue.Empty:
                return completed
            command = [
                "python3", str(REPO / "scripts" / "run_ltx_videos.py"),
                "--project", str(production["project"]), "--comfy", dirs[worker], "--comfy-url", urls[worker],
                "--input-json", str(production["input_json"]), "--images-dir", str(production["project"] / "generated_images"),
                "--output-dir", str(production["project"] / "ltx_videos"),
                "--payload", str(production["project"] / "comfy_workflows" / "ltx-2.5-nvfp4-i2v.payload.json"),
                "--runtime-ids", str(runtime_id), "--wait-for-images",
                "--image-failure-file", str(production["project"] / "images.failed"),
                "--worker-label", f"wave_{wave_index}_worker_{worker}",
            ]
            try:
                run(command, log=log_path)
                completed += 1
            finally:
                tasks.task_done()

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.ltx_workers, thread_name_prefix=f"ltx-wave-{wave_index}") as pool:
        futures = [pool.submit(gpu_worker, worker) for worker in range(args.ltx_workers)]
        return sum(future.result() for future in futures)


def run_ltx(production, args, urls, dirs, services, state):
    name = production["name"]
    video_jobs = [
        (index, max(1, math.ceil(float(item["end"]) - float(item["start"]))))
        for index, item in enumerate(production["items"], 1)
        if (item.get("media_type") or "").lower() == "video"
    ]
    wave_size = args.ltx_wave_per_worker * args.ltx_workers
    state.set(name, "ltx", "running", {"jobs": len(video_jobs), "wave_size": wave_size})
    for wave_index, offset in enumerate(range(0, len(video_jobs), wave_size), 1):
        wave = video_jobs[offset:offset + wave_size]
        completed = run_ltx_wave(production, wave, wave_index, args, urls, dirs)
        if completed != len(wave):
            raise RuntimeError(f"LTX wave {wave_index} count mismatch: expected={len(wave)} completed={completed}")
        state.set(name, "ltx", "running", {"completed": min(offset + len(wave), len(video_jobs)), "jobs": len(video_jobs)})
        if offset + len(wave) < len(video_jobs):
            restart_comfy(services, urls)
    state.set(name, "ltx", "complete", {"jobs": len(video_jobs)})


def finalize(production, args, state, image_ready, static_future):
    name = production["name"]
    image_ready.wait()
    static_future.result()
    project = production["project"]
    state.set(name, "finalize", "running")
    run([
        "python3", str(REPO / "scripts" / "validate_project.py"), "--project", str(project),
        "--input-json", str(production["input_json"]), "--voice", str(production["voice_in"]),
        "--images-dir", str(project / "generated_images"), "--ltx-dir", str(project / "ltx_videos"),
    ], log=project / "logs" / "validate.log")
    final = project / "final" / production["drive_name"]
    run([
        "python3", str(REPO / "scripts" / "render_final_video.py"), "--optimized", "--workers", str(args.render_workers),
        "--render-root", str(project / "render_optimized"), "--project", str(project),
        "--input-json", str(production["input_json"]), "--images-dir", str(project / "generated_images"),
        "--ltx-dir", str(project / "ltx_videos"), "--voice", str(production["voice_in"]), "--output", str(final),
    ], log=project / "logs" / "final_render.log")
    report = project / "final" / "qc_report.json"
    run([
        "python3", str(REPO / "scripts" / "qc_final_video.py"), "--video", str(final),
        "--voice", str(production["voice_in"]), "--json", str(production["input_json"]), "--report", str(report),
    ], log=project / "logs" / "qc.log")
    detail = {"final": str(final), "qc_report": str(report)}
    if not args.no_upload:
        target = f"{args.drive_remote}:{args.drive_folder}/{production['drive_name']}"
        run(["rclone", "copyto", str(final), target])
        listing = subprocess.check_output(["rclone", "lsjson", target], text=True)
        listing_data = json.loads(listing)
        remote_item = listing_data[0] if isinstance(listing_data, list) else listing_data
        if int(remote_item.get("Size", -1)) != final.stat().st_size:
            raise RuntimeError(f"Drive size mismatch: local={final.stat().st_size} remote={remote_item.get('Size')}")
        link = subprocess.check_output(["rclone", "link", target], text=True).strip()
        if not link.startswith("http"):
            raise RuntimeError(f"rclone did not return a usable link: {link!r}")
        detail.update({"drive": listing_data, "link": link})
        (project / "final" / "drive_delivery.json").write_text(json.dumps(detail, ensure_ascii=False, indent=2) + "\n")
    state.set(name, "finalize", "complete", detail)
    print(f"[delivered] {name} {detail.get('link', final)}", flush=True)


def safe_finalize(production, args, state, image_ready, static_future):
    try:
        finalize(production, args, state, image_ready, static_future)
    except Exception as exc:
        state.set(production["name"], "finalize", "failed", {"error": str(exc)})
        raise


def main():
    args = parse_args()
    urls = [value.strip() for value in args.comfy_urls.split(",") if value.strip()]
    dirs = [value.strip() for value in args.comfy_dirs.split(",") if value.strip()]
    services = [value.strip() for value in args.comfy_services.split(",") if value.strip()]
    if args.ltx_workers < 1 or len(urls) < args.ltx_workers or len(dirs) < args.ltx_workers:
        raise SystemExit("ltx-workers requires matching comfy URLs and directories")
    if args.ltx_wave_per_worker < 1 or args.render_workers < 1:
        raise SystemExit("ltx-wave-per-worker and render-workers must be positive")
    if len(set(urls[:args.ltx_workers])) != args.ltx_workers or len(set(dirs[:args.ltx_workers])) != args.ltx_workers:
        raise SystemExit("each LTX worker requires a unique ComfyUI URL and directory")
    if not args.plan:
        for comfy_dir in dirs[:args.ltx_workers]:
            if not Path(comfy_dir).is_dir():
                raise SystemExit(f"ComfyUI directory does not exist: {comfy_dir}")
    if services and len(services) < args.ltx_workers:
        raise SystemExit("comfy-services must be empty or provide one service per LTX worker")
    manifest = Path(args.manifest).resolve()
    productions = normalize_productions(manifest, args.project_root)
    if args.plan:
        preflight(productions, [], True, args.drive_remote)
        plan = []
        for production in productions:
            items = json.loads(production["shot_json"].read_text())
            videos = [item for item in items if (item.get("media_type") or "").lower() == "video"]
            plan.append({
                "name": production["name"],
                "shots": len(items),
                "video_images": len(videos),
                "static_images": len(items) - len(videos),
                "ltx_waves": math.ceil(len(videos) / (args.ltx_wave_per_worker * args.ltx_workers)) if videos else 0,
                "project": str(production["project"]),
                "drive_name": production["drive_name"],
            })
        print(json.dumps({"ltx_workers": args.ltx_workers, "wave_per_worker": args.ltx_wave_per_worker, "productions": plan}, indent=2))
        return
    state = State(manifest.with_suffix(".state.sqlite3"))
    ready_events = {production["name"]: threading.Event() for production in productions}
    wait_apis(urls[:args.ltx_workers])
    preflight(productions, urls[:args.ltx_workers], args.no_upload, args.drive_remote)
    for production in productions:
        production["render_workers"] = args.render_workers
        bootstrap(production)
        state.set(production["name"], "bootstrap", "complete")

    image_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="images")
    static_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="static")
    final_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="final")
    image_future = image_pool.submit(image_producer, productions, state, ready_events)
    static_futures = {
        production["name"]: static_pool.submit(static_renderer, production, state, ready_events[production["name"]])
        for production in productions
    }
    final_futures = []
    try:
        for production_index, production in enumerate(productions):
            try:
                run_ltx(production, args, urls, dirs, services, state)
            except Exception as exc:
                state.set(production["name"], "ltx", "failed", {"error": str(exc)})
                raise
            final_futures.append(final_pool.submit(
                safe_finalize, production, args, state, ready_events[production["name"]], static_futures[production["name"]]
            ))
            if production_index + 1 < len(productions):
                restart_comfy(services, urls[:args.ltx_workers])
        image_future.result()
        for future in final_futures:
            future.result()
        deliveries = []
        for production in productions:
            delivery_path = production["project"] / "final" / "drive_delivery.json"
            entry = {"name": production["name"], "project": str(production["project"])}
            if delivery_path.exists():
                entry.update(json.loads(delivery_path.read_text()))
            deliveries.append(entry)
        delivery_summary = manifest.with_suffix(".delivery.json")
        delivery_summary.write_text(json.dumps({"productions": deliveries}, ensure_ascii=False, indent=2) + "\n")
        print(f"[batch-complete] {delivery_summary}", flush=True)
    finally:
        image_pool.shutdown(wait=True, cancel_futures=True)
        static_pool.shutdown(wait=True, cancel_futures=True)
        final_pool.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    main()
