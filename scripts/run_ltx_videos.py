import argparse
import json, math, shutil, socket, subprocess, time, uuid, urllib.error, urllib.request
from pathlib import Path

FPS = 25
MEGAPIXELS = 0.9
ASPECT_RATIO = "16:9 (Widescreen)"
MULTIPLE = 32

def parse_args():
    parser = argparse.ArgumentParser(description="Render video shots with LTX 2.5 Benny NVFP4 + Conv VAE through ComfyUI.")
    parser.add_argument("--project", default="/workspace/japan_project", help="Working project directory on Vast.")
    parser.add_argument("--comfy", default="/workspace/ComfyUI", help="ComfyUI directory.")
    parser.add_argument("--input-json", default=None, help="Shot list JSON. Defaults to <project>/inputs/shot_list.json.")
    parser.add_argument("--images-dir", default=None, help="Generated image directory. Defaults to <project>/generated_images.")
    parser.add_argument("--output-dir", default=None, help="LTX video output directory. Defaults to <project>/ltx_videos.")
    parser.add_argument("--payload", default=None, help="Defaults to <project>/comfy_workflows/ltx-2.5-nvfp4-i2v.payload.json.")
    parser.add_argument("--comfy-url", default="http://127.0.0.1:18188", help="ComfyUI API URL.")
    parser.add_argument("--megapixels", type=float, default=MEGAPIXELS, help="LTX source resolution. Production default is 0.9 MP; final renderer upscales to 1920x1080.")
    parser.add_argument("--aspect-ratio", default=ASPECT_RATIO)
    parser.add_argument("--multiple", type=int, default=MULTIPLE)
    parser.add_argument("--fps", type=int, default=FPS)
    parser.add_argument("--shard-count", type=int, default=1, help="Number of parallel LTX workers.")
    parser.add_argument("--shard-index", type=int, default=0, help="Zero-based worker index.")
    return parser.parse_args()

args = parse_args()
PROJECT = Path(args.project)
COMFY = Path(args.comfy)
INPUT_JSON = Path(args.input_json) if args.input_json else PROJECT / "inputs" / "shot_list.json"
IMAGES_DIR = Path(args.images_dir) if args.images_dir else PROJECT / "generated_images"
OUT_DIR = Path(args.output_dir) if args.output_dir else PROJECT / "ltx_videos"
PAYLOAD = Path(args.payload) if args.payload else PROJECT / "comfy_workflows" / "ltx-2.5-nvfp4-i2v.payload.json"
if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
    raise SystemExit("shard-count must be >= 1 and shard-index must be in [0, shard-count)")
LOG_PATH = OUT_DIR.with_name(OUT_DIR.name + (f"_worker_{args.shard_index}.jsonl" if args.shard_count > 1 else "_log.jsonl"))
INPUT_SUBDIR = "japan_project_i2v"
INPUT_DIR = COMFY / "input" / INPUT_SUBDIR
FPS = args.fps

INPUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

items = json.loads(INPUT_JSON.read_text())
for runtime_id, item in enumerate(items, 1):
    item["_runtime_id"] = runtime_id
video_items = [x for x in items if (x.get("media_type") or "").lower() == "video"]

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
    if not src.exists():
        raise FileNotFoundError(src)
    if not dst.exists() or dst.stat().st_size != src.stat().st_size:
        shutil.copy2(src, dst)
    return f"{INPUT_SUBDIR}/{name}"

def expected_output(shot_id):
    return OUT_DIR / f"shot_{shot_id:03d}.mp4"

def probe_video(path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
            check=True, capture_output=True, text=True,
        )
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        return {
            "width": int(stream["width"]), "height": int(stream["height"]),
            "fps": stream["r_frame_rate"], "duration": float(data["format"]["duration"]),
        }
    except (subprocess.SubprocessError, KeyError, ValueError, json.JSONDecodeError, IndexError):
        return None

def valid_video(path, requested_duration):
    info = probe_video(path) if path.exists() and path.stat().st_size > 100000 else None
    if not info:
        return False, info
    valid = (
        info["width"] >= 1200 and info["height"] >= 672
        and info["fps"] == f"{FPS}/1"
        and abs(info["duration"] - requested_duration) <= max(1.0, 2.0 / FPS)
    )
    return valid, info

def copy_comfy_output(filename, subfolder, shot_id):
    src = COMFY / "output" / subfolder / filename
    dst = expected_output(shot_id)
    if not src.exists():
        raise FileNotFoundError(src)
    shutil.copy2(src, dst)
    return dst

def make_prompt(item):
    w = json.loads(json.dumps(base_payload))
    shot_id = item["_runtime_id"]
    duration = shot_duration(item)
    motion_prompt = (item.get("edit") or {}).get("motion_prompt") or item.get("shot") or "subtle documentary motion"
    image_name = prepare_image(shot_id)

    w["395"]["inputs"]["image"] = image_name
    w["398:376"]["inputs"]["value"] = motion_prompt
    w["398:383"]["inputs"]["value"] = False
    w["398:361"]["inputs"]["value"] = FPS
    w["398:362"]["inputs"]["value"] = duration
    w["398:366"]["inputs"]["batch_size"] = 1
    w["403"]["inputs"].update({"aspect_ratio": args.aspect_ratio, "megapixels": args.megapixels, "multiple": args.multiple})
    w["75"]["inputs"]["filename_prefix"] = f"japan_project_ltx/shot_{shot_id:03d}"
    seed = 1000000 + shot_id
    w["398:339"]["inputs"]["noise_seed"] = seed
    w["398:338"]["inputs"]["noise_seed"] = seed
    for node in w.values():
        for k, v in list((node.get("inputs") or {}).items()):
            if v == "__RANDOM_INT__":
                node["inputs"][k] = seed
    return w, duration, motion_prompt

def run_one(item):
    shot_id = item["_runtime_id"]
    source_id = item.get("id", shot_id)
    dst = expected_output(shot_id)
    requested_duration = shot_duration(item)
    cached_ok, cached_info = valid_video(dst, requested_duration)
    if cached_ok:
        print(f"[skip] shot_{shot_id:03d} source_id={source_id} valid={cached_info} {dst}", flush=True)
        return True
    prompt, duration, motion_prompt = make_prompt(item)
    print(f"[queue] shot_{shot_id:03d} source_id={source_id} duration={duration}s", flush=True)
    started = time.time()
    resp = request_json(f"{args.comfy_url}/prompt", {
        "prompt": prompt,
        "client_id": "japan-ltx-" + str(uuid.uuid4()),
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
            save = outputs.get("75") or {}
            images = save.get("images") or []
            if not images:
                raise RuntimeError(f"no video output for shot_{shot_id:03d}: {json.dumps(outputs)[:1000]}")
            fileinfo = images[0]
            dst = copy_comfy_output(fileinfo["filename"], fileinfo.get("subfolder", ""), shot_id)
            output_ok, output_info = valid_video(dst, duration)
            if not output_ok:
                raise RuntimeError(f"invalid LTX output for shot_{shot_id:03d}: {output_info}")
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

print(f"[config] engine=ltx-2.5-benny-nvfp4-conv-vae worker={args.shard_index + 1}/{args.shard_count} video_jobs={len(video_items)} assigned_seconds={shard_totals[args.shard_index]} all_worker_seconds={shard_totals} comfy_url={args.comfy_url} comfy_root={COMFY} megapixels={args.megapixels} aspect={args.aspect_ratio!r} multiple={args.multiple} fps={FPS} prompt_enhancer=false final_target=1920x1080", flush=True)
for item in video_items:
    shot_id = item["_runtime_id"]
    source_id = item.get("id", shot_id)
    for attempt in range(1, 4):
        try:
            run_one(item)
            break
        except Exception as e:
            if attempt < 3:
                print(f"[ltx-retry] shot_{shot_id:03d} source_id={source_id} attempt={attempt}/3 error={e}", flush=True)
                log({"id": source_id, "runtime_id": shot_id, "ok": False, "retry": True, "attempt": attempt, "error": str(e)})
                time.sleep(30)
                continue
            print(f"[fatal] shot_{shot_id:03d} source_id={source_id} attempts=3: {e}", flush=True)
            log({"id": source_id, "runtime_id": shot_id, "ok": False, "attempts": 3, "error": str(e)})
            raise
print("[summary] all LTX video shots completed", flush=True)
