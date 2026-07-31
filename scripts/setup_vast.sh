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
cp "$REPO_DIR"/comfy_workflows/ltx-2.3-i2v.payload.json "$PROJECT/comfy_workflows/ltx-2.3-i2v.payload.json"

python3 -m pip install pillow huggingface_hub hf_xet >/dev/null 2>&1 || \
  python3 -m pip install --break-system-packages pillow huggingface_hub hf_xet >/dev/null

if command -v npm >/dev/null 2>&1; then
  (cd "$REPO_DIR" && npm install)
fi

if [ -d "$COMFY" ]; then
  mkdir -p "$COMFY/input/japan_project_i2v"
fi

echo "[setup] done"
echo "[setup] Next: put shot_list.json, voice.wav, subtitles.srt into $PROJECT/inputs/"
