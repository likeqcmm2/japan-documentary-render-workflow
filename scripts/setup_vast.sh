#!/usr/bin/env bash
set -euo pipefail

PROJECT="${PROJECT:-/workspace/japan_project}"
COMFY="${COMFY:-/workspace/ComfyUI}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mkdir -p "$PROJECT"/{inputs,assets,comfy_workflows,generated_images,ltx_videos,final}

echo "[setup] project=$PROJECT"
echo "[setup] repo=$REPO_DIR"

cp "$REPO_DIR"/assets/grain.mp4 "$PROJECT/assets/grain.mp4"
cp "$REPO_DIR"/assets/keyboard-typing-sound-effect-335503.mp3 "$PROJECT/assets/keyboard-typing-sound-effect-335503.mp3"
cp "$REPO_DIR"/assets/YujiBoku-Regular.ttf "$PROJECT/assets/YujiBoku-Regular.ttf"
cp "$REPO_DIR"/comfy_workflows/ltx-2.5-nvfp4-i2v.payload.json "$PROJECT/comfy_workflows/ltx-2.5-nvfp4-i2v.payload.json"

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
    raise SystemExit("[setup] Benny NVFP4 requires a Blackwell-class GPU (compute capability >= 10.0)")
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

COMFY="$COMFY" "$VENV/bin/python" - <<'PY'
import os, re
from pathlib import Path
version_file = Path(os.environ["COMFY"]) / "comfyui_version.py"
text = version_file.read_text() if version_file.exists() else ""
match = re.search(r'__version__\s*=\s*["\']([^"\']+)', text)
if match:
    version = tuple(int(x) for x in re.findall(r"\d+", match.group(1))[:3])
    print(f"[setup] comfyui_version={match.group(1)}")
    if version < (0, 32, 0):
        raise SystemExit("[setup] ComfyUI >= 0.32.0 is required for Benny NVFP4")
else:
    print("[setup] warning: could not read ComfyUI version; API/model validation remains required")
PY

if [ "${SKIP_MODEL_DOWNLOAD:-0}" != "1" ]; then
  COMFY="$COMFY" VENV="$VENV" "$REPO_DIR/scripts/download_ltx_models.sh"
else
  echo "[setup] SKIP_MODEL_DOWNLOAD=1; model download skipped"
fi

echo "[setup] done"
echo "[setup] LTX production source: 0.9 MP, 16:9, 25 fps; final renderer: 1920x1080"
echo "[setup] Next: put shot_list.json, voice.wav, subtitles.srt into $PROJECT/inputs/"
