#!/usr/bin/env bash
set -euo pipefail

PROJECT="${PROJECT:-/workspace/japan_project}"
COMFY="${COMFY:-/workspace/ComfyUI}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mkdir -p "$PROJECT"/{inputs,assets,comfy_workflows,generated_images,fasth3_videos,final}

echo "[setup] project=$PROJECT"
echo "[setup] repo=$REPO_DIR"

cp "$REPO_DIR"/assets/grain.mp4 "$PROJECT/assets/grain.mp4"
cp "$REPO_DIR"/assets/keyboard-typing-sound-effect-335503.mp3 "$PROJECT/assets/keyboard-typing-sound-effect-335503.mp3"
cp "$REPO_DIR"/assets/YujiBoku-Regular.ttf "$PROJECT/assets/YujiBoku-Regular.ttf"
cp "$REPO_DIR"/comfy_workflows/fasth3-8step-i2v.payload.json "$PROJECT/comfy_workflows/fasth3-8step-i2v.payload.json"

VENV="${VENV:-/venv/main}"
"$VENV/bin/python" -m pip install --upgrade pillow huggingface_hub hf_xet safetensors >/dev/null

"$VENV/bin/python" - <<'PY'
import torch
if not torch.cuda.is_available():
    raise SystemExit("[setup] CUDA GPU is unavailable")
major, minor = torch.cuda.get_device_capability()
name = torch.cuda.get_device_name()
print(f"[setup] gpu={name} compute_capability={major}.{minor}")
if major < 10:
    raise SystemExit("[setup] FastH3 NVFP4 text encoder requires a Blackwell-class GPU (compute capability >= 10.0)")
PY

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "[setup] node/npm missing; installing nodejs npm with apt"
  apt-get update >/dev/null
  DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs npm >/dev/null
fi

if ! fc-match "Noto Sans CJK JP" 2>/dev/null | grep -q "NotoSansCJK"; then
  echo "[setup] Japanese CJK fonts missing; installing fonts-noto-cjk"
  apt-get update >/dev/null
  DEBIAN_FRONTEND=noninteractive apt-get install -y fonts-noto-cjk >/dev/null
  fc-cache -f >/dev/null
fi
(cd "$REPO_DIR" && npm install)

if [ -d "$COMFY" ]; then
  mkdir -p "$COMFY/input/japan_project_i2v"
else
  echo "[setup] missing ComfyUI directory: $COMFY" >&2
  exit 1
fi

COMFY="$COMFY" VENV="$VENV" "$REPO_DIR/scripts/update_comfyui.sh"

if [ "${SKIP_MODEL_DOWNLOAD:-0}" != "1" ]; then
  COMFY="$COMFY" VENV="$VENV" "$REPO_DIR/scripts/download_fasth3_models.sh"
else
  echo "[setup] SKIP_MODEL_DOWNLOAD=1; model download skipped"
fi

COMFY_SERVICE="${COMFY_SERVICE:-comfyui}"
COMFY_URL="${COMFY_URL:-http://127.0.0.1:18188}"
if [ -n "$COMFY_SERVICE" ]; then
  supervisorctl restart "$COMFY_SERVICE"
fi
COMFY_URL="$COMFY_URL" MANIFEST="$REPO_DIR/model_manifest.json" "$VENV/bin/python" - <<'PYREADY'
import json, os, time, urllib.request
from pathlib import Path
manifest = json.loads(Path(os.environ["MANIFEST"]).read_text())
required = [m["filename"] for m in manifest["models_referenced_by_payload"]] + manifest["required_nodes"]
url = os.environ["COMFY_URL"]
for attempt in range(80):
    try:
        with urllib.request.urlopen(f"{url}/object_info", timeout=10) as response:
            payload = response.read().decode()
        missing = [name for name in required if name not in payload]
        if not missing:
            print("[setup] ComfyUI FastH3 nodes and models ready", flush=True)
            break
    except Exception:
        pass
    time.sleep(3)
else:
    raise SystemExit(f"[setup] ComfyUI did not report required FastH3 nodes and models at {url}; missing={missing if 'missing' in locals() else 'API unavailable'}")
PYREADY

echo "[setup] done"
echo "[setup] FastH3 source: 1280x704, 24 fps; normalized clips: 25 fps; final renderer: 1920x1080"
echo "[setup] Next: put shot_list.json and voice.wav into $PROJECT/inputs/"
