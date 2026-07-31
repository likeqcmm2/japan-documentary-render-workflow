import argparse
import json, math, os, shutil, socket, time, uuid, urllib.error, urllib.request
from pathlib import Path

WIDTH = 1920
HEIGHT = 1080
FPS = 25

def parse_args():
    parser = argparse.ArgumentParser(description="Render media_type=video shots through the proven LTX 2.3 I2V ComfyUI workflow.")
    parser.add_argument("--project", default="/workspace/japan_project", help="Working project directory on Vast.")
    parser.add_argument("--comfy", default="/workspace/ComfyUI", help="ComfyUI directory.")
    parser.add_argument("--input-json", default=None, help="Shot list JSON. Defaults to <project>/inputs/shot_list.json.")
    parser.add_argument("--images-dir", default=None, help="Generated image directory. Defaults to <project>/generated_images.")
    parser.add_argument("--output-dir", default=None, help="LTX video output directory. Defaults to <project>/ltx_videos.")
    parser.add_argument("--payload", default=None, help="ComfyUI workflow payload. Defaults to <project>/comfy_workflows/ltx-2.3-i2v.payload.json.")
    parser.add_argument("--comfy-url", default="http://127.0.0.1:18188", help="ComfyUI API URL.")
    parser.add_argument("--width", type=int, default=WIDTH)
    parser.add_argument("--height", type=int, default=HEIGHT)
    parser.add_argument("--fps", type=int, default=FPS)
    return parser.parse_args()

args = parse_args()
PROJECT = Path(args.project)
COMFY = Path(args.comfy)
INPUT_JSON = Path(args.input_json) if args.input_json else PROJECT / "inputs" / "shot_list.json"
IMAGES_DIR = Path(args.images_dir) if args.images_dir else PROJECT / "generated_images"
OUT_DIR = Path(args.output_dir) if args.output_dir else PROJECT / "ltx_videos"
PAYLOAD = Path(args.payload) if args.payload else PROJECT / "comfy_workflows" / "ltx-2.3-i2v.payload.json"
LOG_PATH = OUT_DIR.with_name(OUT_DIR.name + "_log.jsonl")
INPUT_SUBDIR = "japan_project_i2v"
INPUT_DIR = COMFY / "input" / INPUT_SUBDIR
WIDTH, HEIGHT, FPS = args.width, args.height, args.fps

INPUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

items = json.loads(INPUT_JSON.read_text())
video_items = [x for x in items if (x.get("media_type") or "").lower() == "video"]
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

def copy_comfy_output(filename, subfolder, shot_id):
    src = COMFY / "output" / subfolder / filename
    dst = expected_output(shot_id)
    if not src.exists():
        raise FileNotFoundError(src)
    shutil.copy2(src, dst)
    return dst

def make_prompt(item):
    w = json.loads(json.dumps(base_payload))
    shot_id = int(item["id"])
    duration = int(math.ceil(float(item["end"]) - float(item["start"])))
    duration = max(1, duration)
    motion_prompt = (item.get("edit") or {}).get("motion_prompt") or item.get("shot") or "subtle documentary motion"
    image_name = prepare_image(shot_id)

    w["269"]["inputs"]["image"] = image_name
    w["320:319"]["inputs"]["value"] = motion_prompt
    w["320:328"]["inputs"]["value"] = False
    w["320:312"]["inputs"]["value"] = WIDTH
    w["320:299"]["inputs"]["value"] = HEIGHT
    w["320:300"]["inputs"]["value"] = FPS
    w["320:301"]["inputs"]["value"] = duration
    w["75"]["inputs"]["filename_prefix"] = f"japan_project_ltx/shot_{shot_id:03d}"
    seed = 1000000 + shot_id
    for node in w.values():
        for k, v in list((node.get("inputs") or {}).items()):
            if v == "__RANDOM_INT__":
                node["inputs"][k] = seed
    return w, duration, motion_prompt

def run_one(item):
    shot_id = int(item["id"])
    dst = expected_output(shot_id)
    if dst.exists() and dst.stat().st_size > 100000:
        print(f"[skip] shot_{shot_id:03d} exists {dst}", flush=True)
        return True
    prompt, duration, motion_prompt = make_prompt(item)
    print(f"[queue] shot_{shot_id:03d} duration={duration}s", flush=True)
    started = time.time()
    resp = request_json(f"{args.comfy_url}/prompt", {
        "prompt": prompt,
        "client_id": "japan-ltx-" + str(uuid.uuid4()),
    })
    if resp.get("node_errors"):
        raise RuntimeError(json.dumps(resp["node_errors"], ensure_ascii=False))
    pid = resp["prompt_id"]
    while True:
        hist = request_json(f"{args.comfy_url}/history/{pid}", timeout=60)
        if pid in hist:
            outputs = hist[pid].get("outputs", {})
            save = outputs.get("75") or {}
            images = save.get("images") or []
            if not images:
                raise RuntimeError(f"no video output for shot_{shot_id:03d}: {json.dumps(outputs)[:1000]}")
            fileinfo = images[0]
            dst = copy_comfy_output(fileinfo["filename"], fileinfo.get("subfolder", ""), shot_id)
            elapsed = time.time() - started
            print(f"[done] shot_{shot_id:03d} elapsed={elapsed:.1f}s -> {dst}", flush=True)
            log({"id": shot_id, "ok": True, "duration_requested": duration, "elapsed_sec": round(elapsed, 3), "output": str(dst), "motion_prompt": motion_prompt})
            return True
        if int(time.time() - started) % 30 < 5:
            q = request_json(f"{args.comfy_url}/queue", timeout=60)
            print(f"[wait] shot_{shot_id:03d} t={int(time.time()-started)}s running={len(q.get('queue_running', []))} pending={len(q.get('queue_pending', []))}", flush=True)
        time.sleep(5)

print(f"[config] video_jobs={len(video_items)} size={WIDTH}x{HEIGHT} fps={FPS}", flush=True)
for item in video_items:
    try:
        run_one(item)
    except Exception as e:
        shot_id = int(item.get("id", -1))
        print(f"[fatal] shot_{shot_id:03d}: {e}", flush=True)
        log({"id": shot_id, "ok": False, "error": str(e)})
        raise
print("[summary] all LTX video shots completed", flush=True)
