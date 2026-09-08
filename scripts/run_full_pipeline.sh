#!/usr/bin/env bash
set -euo pipefail

PROJECT="${PROJECT:-/workspace/japan_project}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SHOT_JSON="${1:?Usage: scripts/run_full_pipeline.sh shot_list.json voice.wav}"
VOICE="${2:?Usage: scripts/run_full_pipeline.sh shot_list.json voice.wav}"

mkdir -p "$PROJECT/inputs" "$PROJECT/comfy_workflows" "$PROJECT/assets"
cp "$REPO_DIR/comfy_workflows/ltx-2.5-nvfp4-i2v.payload.json" "$PROJECT/comfy_workflows/ltx-2.5-nvfp4-i2v.payload.json"
cp "$REPO_DIR/assets/grain.mp4" "$PROJECT/assets/grain.mp4"
cp "$REPO_DIR/assets/keyboard-typing-sound-effect-335503.mp3" "$PROJECT/assets/keyboard-typing-sound-effect-335503.mp3"
cp "$REPO_DIR/assets/YujiBoku-Regular.ttf" "$PROJECT/assets/YujiBoku-Regular.ttf"
cp "$SHOT_JSON" "$PROJECT/inputs/shot_list.json"
cp "$VOICE" "$PROJECT/inputs/voice$(python3 - <<'PY' "$VOICE"
import sys
from pathlib import Path
print(Path(sys.argv[1]).suffix)
PY
)"
VOICE_IN="$PROJECT/inputs/voice$(python3 - <<'PY' "$VOICE"
import sys
from pathlib import Path
print(Path(sys.argv[1]).suffix)
PY
)"
RENDER_MODE="${RENDER_MODE:-optimized}"
RENDER_WORKERS="${RENDER_WORKERS:-6}"
LTX_WORKERS="${LTX_WORKERS:-1}"
IFS=',' read -r -a LTX_COMFY_URL_LIST <<< "${LTX_COMFY_URLS:-http://127.0.0.1:18188}"
IFS=',' read -r -a LTX_COMFY_DIR_LIST <<< "${LTX_COMFY_DIRS:-/workspace/ComfyUI}"

if ! [[ "$LTX_WORKERS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[fatal] LTX_WORKERS must be a positive integer" >&2
  exit 2
fi
if [ "${#LTX_COMFY_URL_LIST[@]}" -lt "$LTX_WORKERS" ] || [ "${#LTX_COMFY_DIR_LIST[@]}" -lt "$LTX_WORKERS" ]; then
  echo "[fatal] LTX_COMFY_URLS and LTX_COMFY_DIRS need at least LTX_WORKERS comma-separated entries" >&2
  exit 2
fi

(cd "$REPO_DIR" && node scripts/generate_images_from_shot_json.js --input "$PROJECT/inputs/shot_list.json" --output "$PROJECT/generated_images")

ltx_pids=()
for ((worker=0; worker<LTX_WORKERS; worker++)); do
  echo "[ltx] starting worker=$((worker + 1))/$LTX_WORKERS url=${LTX_COMFY_URL_LIST[$worker]}"
  python3 "$REPO_DIR/scripts/run_ltx_videos.py" \
    --project "$PROJECT" \
    --comfy "${LTX_COMFY_DIR_LIST[$worker]}" \
    --comfy-url "${LTX_COMFY_URL_LIST[$worker]}" \
    --input-json "$PROJECT/inputs/shot_list.json" \
    --images-dir "$PROJECT/generated_images" \
    --output-dir "$PROJECT/ltx_videos" \
    --payload "$PROJECT/comfy_workflows/ltx-2.5-nvfp4-i2v.payload.json" \
    --megapixels "${LTX_MEGAPIXELS:-0.9}" \
    --aspect-ratio "${LTX_ASPECT_RATIO:-16:9 (Widescreen)}" \
    --multiple "${LTX_MULTIPLE:-32}" \
    --shard-count "$LTX_WORKERS" \
    --shard-index "$worker" \
    > "$PROJECT/ltx_worker_${worker}.log" 2>&1 &
  ltx_pids+=("$!")
done

ltx_failed=0
for ((worker=0; worker<LTX_WORKERS; worker++)); do
  if ! wait "${ltx_pids[$worker]}"; then
    echo "[fatal] LTX worker $worker failed; see $PROJECT/ltx_worker_${worker}.log" >&2
    ltx_failed=1
  fi
done
if [ "$ltx_failed" -ne 0 ]; then
  exit 1
fi
echo "[ltx] all $LTX_WORKERS workers completed"

python3 "$REPO_DIR/scripts/validate_project.py" \
  --project "$PROJECT" \
  --input-json "$PROJECT/inputs/shot_list.json" \
  --voice "$VOICE_IN" \
  --images-dir "$PROJECT/generated_images" \
  --ltx-dir "$PROJECT/ltx_videos"

if [ "$RENDER_MODE" = "legacy" ]; then
  echo "[render] mode=legacy"
  python3 "$REPO_DIR/scripts/render_final_video.py" \
    --project "$PROJECT" \
    --input-json "$PROJECT/inputs/shot_list.json" \
    --images-dir "$PROJECT/generated_images" \
    --ltx-dir "$PROJECT/ltx_videos" \
    --voice "$VOICE_IN" \
    --output "$PROJECT/final/final_video.mp4"
else
  echo "[render] mode=optimized workers=$RENDER_WORKERS"
  python3 "$REPO_DIR/scripts/render_final_video.py" \
    --optimized \
    --workers "$RENDER_WORKERS" \
    --render-root "$PROJECT/render_optimized" \
    --project "$PROJECT" \
    --input-json "$PROJECT/inputs/shot_list.json" \
    --images-dir "$PROJECT/generated_images" \
    --ltx-dir "$PROJECT/ltx_videos" \
    --voice "$VOICE_IN" \
    --output "$PROJECT/final/final_video.mp4"
fi

echo "[done] $PROJECT/final/final_video.mp4"
