import argparse
import hashlib, json, math, os, shutil, socket, subprocess, time, uuid, urllib.error, urllib.request
from pathlib import Path

FPS = 25
NATIVE_FPS = 24
SOURCE_WIDTH = 1280
SOURCE_HEIGHT = 704

def parse_args():
    parser = argparse.ArgumentParser(description="Render video shots with FastH3 8-Step V2 through ComfyUI.")
    parser.add_argument("--project", default="/workspace/japan_project", help="Working project directory on Vast.")
    parser.add_argument("--comfy", default="/workspace/ComfyUI", help="ComfyUI directory.")
    parser.add_argument("--input-json", default=None, help="Shot list JSON. Defaults to <project>/inputs/shot_list.json.")
    parser.add_argument("--images-dir", default=None, help="Generated image directory. Defaults to <project>/generated_images.")
    parser.add_argument("--output-dir", default=None, help="FastH3 video output directory. Defaults to <project>/fasth3_videos.")
    parser.add_argument("--payload", default=None, help="Defaults to <project>/comfy_workflows/fasth3-8step-i2v.payload.json.")
    parser.add_argument("--comfy-url", default="http://127.0.0.1:18188", help="ComfyUI API URL.")
    parser.add_argument("--fps", type=int, default=FPS)
    parser.add_argument("--shard-count", type=int, default=1, help="Number of parallel FastH3 workers.")
    parser.add_argument("--shard-index", type=int, default=0, help="Zero-based worker index.")
    parser.add_argument("--runtime-ids", default=None, help="Comma-separated runtime IDs to process. Applied after stable IDs are assigned.")
    parser.add_argument("--wait-for-images", action="store_true", help="Wait for upstream image generation instead of failing when an image is not ready.")
    parser.add_argument("--image-wait-timeout", type=int, default=3600, help="Maximum seconds to wait for each upstream image.")
    parser.add_argument("--image-failure-file", default=None, help="Abort image waiting if this upstream failure sentinel appears.")
    parser.add_argument("--worker-label", default=None, help="Unique label for this worker's JSONL audit log.")
    return parser.parse_args()

args = parse_args()
PROJECT = Path(args.project)
COMFY = Path(args.comfy)
INPUT_JSON = Path(args.input_json) if args.input_json else PROJECT / "inputs" / "shot_list.json"
IMAGES_DIR = Path(args.images_dir) if args.images_dir else PROJECT / "generated_images"
OUT_DIR = Path(args.output_dir) if args.output_dir else PROJECT / "fasth3_videos"
PAYLOAD = Path(args.payload) if args.payload else PROJECT / "comfy_workflows" / "fasth3-8step-i2v.payload.json"
IMAGE_FAILURE_FILE = Path(args.image_failure_file) if args.image_failure_file else None
if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
    raise SystemExit("shard-count must be >= 1 and shard-index must be in [0, shard-count)")
log_suffix = f"_{args.worker_label}" if args.worker_label else (f"_worker_{args.shard_index}" if args.shard_count > 1 else "_log")
LOG_PATH = OUT_DIR.with_name(OUT_DIR.name + log_suffix + ".jsonl")
INPUT_SUBDIR = "japan_project_i2v"
INPUT_DIR = COMFY / "input" / INPUT_SUBDIR
FPS = args.fps

INPUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

items = json.loads(INPUT_JSON.read_text())
for runtime_id, item in enumerate(items, 1):
    item["_runtime_id"] = runtime_id
video_items = [x for x in items if (x.get("media_type") or "").lower() == "video"]
if args.runtime_ids:
    selected_runtime_ids = {int(value) for value in args.runtime_ids.split(",") if value.strip()}
    video_items = [item for item in video_items if item["_runtime_id"] in selected_runtime_ids]

def shot_duration(item):
    return max(1, int(math.ceil(float(item["end"]) - float(item["start"]))))

def balanced_shards(items, count):
    shards = [[] for _ in range(count)]
    totals = [0 for _ in range(count)]
    # Longest-processing-time partitioning keeps uneven clips balanced while
    # runtime IDs remain stable for cache and final assembly.
    ordered = sorted(items, key=lambda item: (-shot_duration(item), item["_runtime_id"]))
    for item in ordered:
        target = min(range(count), key=lambda index: (totals[index], index))
        shards[target].append(item)
        totals[target] += shot_duration(item)
    for shard in shards:
        shard.sort(key=lambda item: item["_runtime_id"])
    return shards, totals

all_shards, shard_totals = balanced_shards(video_items, args.shard_count)
video_items = all_shards[args.shard_index]
base_payload = json.loads(PAYLOAD.read_text())["input"]["workflow_json"]

def log(entry):
    entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def request_json(url, data=None, timeout=60, retries=8):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            if data is None:
                with urllib.request.urlopen(url, timeout=timeout) as r:
                    return json.load(r)
            body = json.dumps(data).encode()
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except (urllib.error.URLError, ConnectionResetError, TimeoutError, socket.timeout) as exc:
            last_error = exc
            wait = min(30, 3 * attempt)
            print(f"[api-retry] {url} attempt={attempt}/{retries} error={exc} wait={wait}s", flush=True)
            time.sleep(wait)
    raise last_error

def prepare_image(shot_id):
    name = f"shot_{shot_id:03d}.png"
    src = IMAGES_DIR / name
    dst = INPUT_DIR / name
    if not src.exists() and args.wait_for_images:
        deadline = time.monotonic() + args.image_wait_timeout
        last_notice = 0.0
        while not src.exists():
            if IMAGE_FAILURE_FILE and IMAGE_FAILURE_FILE.exists():
                raise RuntimeError(f"upstream image generation failed: {IMAGE_FAILURE_FILE.read_text(errors='replace')[-2000:]}")
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError(f"timed out waiting for upstream image: {src}")
            if now - last_notice >= 30:
                print(f"[image-wait] shot_{shot_id:03d} remaining={int(deadline - now)}s", flush=True)
                last_notice = now
            time.sleep(2)
    if not src.exists():
        raise FileNotFoundError(src)
    if not dst.exists() or dst.stat().st_size != src.stat().st_size:
        shutil.copy2(src, dst)
    return f"{INPUT_SUBDIR}/{name}"

def expected_output(shot_id):
    return OUT_DIR / f"shot_{shot_id:03d}.mp4"

def target_frames(item):
    return round(float(item["end"]) * FPS) - round(float(item["start"]) * FPS)


def probe_video(path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,width,height,r_frame_rate,nb_frames:format=duration", "-of", "json", str(path)],
            check=True, capture_output=True, text=True,
        )
        data = json.loads(result.stdout)
        video = next(s for s in data["streams"] if s["codec_type"] == "video")
        return {
            "width": int(video["width"]), "height": int(video["height"]),
            "fps": video["r_frame_rate"], "frames": int(video["nb_frames"]),
            "duration": float(data["format"]["duration"]),
            "audio": any(s["codec_type"] == "audio" for s in data["streams"]),
        }
    except (subprocess.SubprocessError, KeyError, ValueError, json.JSONDecodeError, IndexError, StopIteration):
        return None


def valid_video(path, expected_frames):
    info = probe_video(path) if path.exists() and path.stat().st_size > 10000 else None
    valid = bool(info and info["width"] == SOURCE_WIDTH and info["height"] == SOURCE_HEIGHT
                 and info["fps"] == f"{FPS}/1" and info["frames"] == expected_frames
                 and not info["audio"])
    return valid, info


def source_fingerprint(image_path, motion_prompt, duration, seed):
    digest = hashlib.sha256()
    digest.update(PAYLOAD.read_bytes())
    digest.update(image_path.read_bytes())
    digest.update(json.dumps([motion_prompt, duration, seed, SOURCE_WIDTH, SOURCE_HEIGHT, FPS], ensure_ascii=False).encode())
    return digest.hexdigest()


def normalize_comfy_output(filename, subfolder, shot_id, frames):
    src = (COMFY / "output" / subfolder / filename).resolve()
    if not src.is_relative_to((COMFY / "output").resolve()) or not src.is_file():
        raise FileNotFoundError(src)
    raw = probe_video(src)
    if not raw or raw["width"] != SOURCE_WIDTH or raw["height"] != SOURCE_HEIGHT or raw["fps"] != f"{NATIVE_FPS}/1":
        raise RuntimeError(f"unexpected FastH3 output: {raw}")
    dst = expected_output(shot_id)
    tmp = dst.with_suffix(".partial.mp4")
    tmp.unlink(missing_ok=True)
    # Keep playback time unchanged while converting 24 -> 25 fps. The final
    # frame is held only if the model returns slightly fewer frames than needed.
    filters = f"fps={FPS},tpad=stop_mode=clone:stop_duration=1,trim=end_frame={frames},setpts=PTS-STARTPTS"
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
               "-vf", filters, "-frames:v", str(frames), "-c:v", "libx264", "-preset", "fast",
               "-crf", "18", "-pix_fmt", "yuv420p", "-an", str(tmp)]
    try:
        subprocess.run(command, check=True)
        ok, info = valid_video(tmp, frames)
        if not ok:
            raise RuntimeError(f"invalid normalized FastH3 output: {info}")
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return dst

def make_prompt(item):
    w = json.loads(json.dumps(base_payload))
    shot_id = item["_runtime_id"]
    duration = shot_duration(item)
    frames = target_frames(item)
    if frames <= 0:
        raise ValueError(f"shot_{shot_id:03d} has no timeline frames")
    motion_prompt = (item.get("edit") or {}).get("motion_prompt") or item.get("shot") or "subtle documentary motion"
    image_name = prepare_image(shot_id)
    seed = 1000000 + shot_id
    w["136"]["inputs"]["image"] = image_name
    w["105:104"]["inputs"].update({"prompt": motion_prompt, "width": SOURCE_WIDTH, "height": SOURCE_HEIGHT})
    w["105:111"]["inputs"]["value"] = float(duration)
    w["105:15"]["inputs"]["noise_seed"] = seed
    w["92"]["inputs"]["filename_prefix"] = f"japan_project_fasth3/shot_{shot_id:03d}"
    fingerprint = source_fingerprint(IMAGES_DIR / f"shot_{shot_id:03d}.png", motion_prompt, duration, seed)
    return w, duration, frames, motion_prompt, fingerprint


def run_one(item):
    shot_id = item["_runtime_id"]
    source_id = item.get("id", shot_id)
    dst = expected_output(shot_id)
    metadata = dst.with_suffix(".meta.json")
    prompt, duration, frames, motion_prompt, fingerprint = make_prompt(item)
    cached_ok, cached_info = valid_video(dst, frames)
    if cached_ok and metadata.is_file():
        try:
            cached_ok = json.loads(metadata.read_text()).get("fingerprint") == fingerprint
        except (ValueError, OSError):
            cached_ok = False
    else:
        cached_ok = False
    if cached_ok:
        print(f"[skip] shot_{shot_id:03d} source_id={source_id} valid={cached_info} {dst}", flush=True)
        return True
    print(f"[queue] shot_{shot_id:03d} source_id={source_id} duration={duration}s frames={frames}", flush=True)
    started = time.time()
    resp = request_json(f"{args.comfy_url}/prompt", {
        "prompt": prompt,
        "client_id": "japan-fasth3-" + str(uuid.uuid4()),
    })
    if resp.get("node_errors"):
        raise RuntimeError(json.dumps(resp["node_errors"], ensure_ascii=False))
    pid = resp["prompt_id"]
    empty_since = None
    last_queue_check = 0
    while True:
        hist = request_json(f"{args.comfy_url}/history/{pid}", timeout=60)
        if pid in hist:
            record = hist[pid]
            status = record.get("status", {})
            if status.get("status_str") == "error" or status.get("completed") is False:
                messages = status.get("messages") or []
                raise RuntimeError(f"ComfyUI execution failed for shot_{shot_id:03d}: {json.dumps(messages, ensure_ascii=False)[-4000:]}")
            outputs = record.get("outputs", {})
            images = (outputs.get("92") or {}).get("images") or []
            if not images:
                raise RuntimeError(f"no video output for shot_{shot_id:03d}: {json.dumps(outputs)[:1000]}")
            fileinfo = images[0]
            dst = normalize_comfy_output(fileinfo["filename"], fileinfo.get("subfolder", ""), shot_id, frames)
            metadata.write_text(json.dumps({"engine":"fasth3-8step-v2", "fingerprint":fingerprint, "frames":frames}) + "\n")
            output_ok, output_info = valid_video(dst, frames)
            if not output_ok:
                raise RuntimeError(f"invalid FastH3 output for shot_{shot_id:03d}: {output_info}")
            elapsed = time.time() - started
            print(f"[done] shot_{shot_id:03d} elapsed={elapsed:.1f}s media={output_info} -> {dst}", flush=True)
            log({"id": source_id, "runtime_id": shot_id, "ok": True, "duration_requested": duration, "elapsed_sec": round(elapsed, 3), "output": str(dst), "media": output_info, "motion_prompt": motion_prompt})
            return True
        now = time.time()
        if now - last_queue_check >= 10:
            q = request_json(f"{args.comfy_url}/queue", timeout=60)
            last_queue_check = now
            running = len(q.get("queue_running", []))
            pending = len(q.get("queue_pending", []))
            if running or pending:
                empty_since = None
            elif now - started >= 30:
                empty_since = empty_since or now
                if now - empty_since >= 120:
                    raise RuntimeError(
                        f"ComfyUI prompt {pid} disappeared from history and queue "
                        f"for {int(now - empty_since)}s (service likely restarted)"
                    )
            if int(now - started) % 30 < 10:
                print(f"[wait] shot_{shot_id:03d} t={int(now-started)}s running={running} pending={pending}", flush=True)
        time.sleep(5)

print(f"[config] engine=fasth3-8step-v2 worker={args.shard_index + 1}/{args.shard_count} video_jobs={len(video_items)} assigned_seconds={shard_totals[args.shard_index]} all_worker_seconds={shard_totals} comfy_url={args.comfy_url} comfy_root={COMFY} source={SOURCE_WIDTH}x{SOURCE_HEIGHT} native_fps={NATIVE_FPS} normalized_fps={FPS} audio=false final_target=1920x1080", flush=True)
for item in video_items:
    shot_id = item["_runtime_id"]
    source_id = item.get("id", shot_id)
    for attempt in range(1, 4):
        try:
            run_one(item)
            break
        except Exception as e:
            if attempt < 3:
                print(f"[fasth3-retry] shot_{shot_id:03d} source_id={source_id} attempt={attempt}/3 error={e}", flush=True)
                log({"id": source_id, "runtime_id": shot_id, "ok": False, "retry": True, "attempt": attempt, "error": str(e)})
                time.sleep(30)
                continue
            print(f"[fatal] shot_{shot_id:03d} source_id={source_id} attempts=3: {e}", flush=True)
            log({"id": source_id, "runtime_id": shot_id, "ok": False, "attempts": 3, "error": str(e)})
            raise
print("[summary] all FastH3 video shots completed", flush=True)
