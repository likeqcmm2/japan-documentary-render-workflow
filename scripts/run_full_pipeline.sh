#!/usr/bin/env bash
set -euo pipefail

PROJECT="${PROJECT:-/workspace/japan_project}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SHOT_JSON="${1:?Usage: scripts/run_full_pipeline.sh shot_list.json voice.wav}"
VOICE="${2:?Usage: scripts/run_full_pipeline.sh shot_list.json voice.wav}"

mkdir -p "$PROJECT/inputs" "$PROJECT/comfy_workflows" "$PROJECT/assets"
cp "$REPO_DIR/comfy_workflows/fasth3-8step-i2v.payload.json" "$PROJECT/comfy_workflows/fasth3-8step-i2v.payload.json"
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
FASTH3_WORKERS="${FASTH3_WORKERS:-1}"
IFS=',' read -r -a FASTH3_COMFY_URL_LIST <<< "${FASTH3_COMFY_URLS:-http://127.0.0.1:18188}"
IFS=',' read -r -a FASTH3_COMFY_DIR_LIST <<< "${FASTH3_COMFY_DIRS:-/workspace/ComfyUI}"

if ! [[ "$FASTH3_WORKERS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[fatal] FASTH3_WORKERS must be a positive integer" >&2
  exit 2
fi
if [ "${#FASTH3_COMFY_URL_LIST[@]}" -lt "$FASTH3_WORKERS" ] || [ "${#FASTH3_COMFY_DIR_LIST[@]}" -lt "$FASTH3_WORKERS" ]; then
  echo "[fatal] FASTH3_COMFY_URLS and FASTH3_COMFY_DIRS need at least FASTH3_WORKERS comma-separated entries" >&2
  exit 2
fi

(cd "$REPO_DIR" && node scripts/generate_images_from_shot_json.js --input "$PROJECT/inputs/shot_list.json" --output "$PROJECT/generated_images")

fasth3_pids=()
for ((worker=0; worker<FASTH3_WORKERS; worker++)); do
  echo "[fasth3] starting worker=$((worker + 1))/$FASTH3_WORKERS url=${FASTH3_COMFY_URL_LIST[$worker]}"
  python3 "$REPO_DIR/scripts/run_fasth3_videos.py" \
    --project "$PROJECT" \
    --comfy "${FASTH3_COMFY_DIR_LIST[$worker]}" \
    --comfy-url "${FASTH3_COMFY_URL_LIST[$worker]}" \
    --input-json "$PROJECT/inputs/shot_list.json" \
    --images-dir "$PROJECT/generated_images" \
    --output-dir "$PROJECT/fasth3_videos" \
    --payload "$PROJECT/comfy_workflows/fasth3-8step-i2v.payload.json" \
    --shard-count "$FASTH3_WORKERS" \
    --shard-index "$worker" \
    > "$PROJECT/fasth3_worker_${worker}.log" 2>&1 &
  fasth3_pids+=("$!")
done

fasth3_failed=0
for ((worker=0; worker<FASTH3_WORKERS; worker++)); do
  if ! wait "${fasth3_pids[$worker]}"; then
    echo "[fatal] FastH3 worker $worker failed; see $PROJECT/fasth3_worker_${worker}.log" >&2
    fasth3_failed=1
  fi
done
if [ "$fasth3_failed" -ne 0 ]; then
  exit 1
fi
echo "[fasth3] all $FASTH3_WORKERS workers completed"

python3 "$REPO_DIR/scripts/validate_project.py" \
  --project "$PROJECT" \
  --input-json "$PROJECT/inputs/shot_list.json" \
  --voice "$VOICE_IN" \
  --images-dir "$PROJECT/generated_images" \
  --fasth3-dir "$PROJECT/fasth3_videos"

if [ "$RENDER_MODE" = "legacy" ]; then
  echo "[render] mode=legacy"
  python3 "$REPO_DIR/scripts/render_final_video.py" \
    --project "$PROJECT" \
    --input-json "$PROJECT/inputs/shot_list.json" \
    --images-dir "$PROJECT/generated_images" \
    --fasth3-dir "$PROJECT/fasth3_videos" \
    --voice "$VOICE_IN" \
    --output "$PROJECT/final/final_video.mp4"
else
  echo "[render] mode=optimized workers=$RENDER_WORKERS"
  python3 "$REPO_DIR/scripts/render_final_video.py" \
    --optimized \
    --workers "$RENDER_WORKERS" \
    --render-root "$PROJECT/render_fasth3_optimized" \
    --project "$PROJECT" \
    --input-json "$PROJECT/inputs/shot_list.json" \
    --images-dir "$PROJECT/generated_images" \
    --fasth3-dir "$PROJECT/fasth3_videos" \
    --voice "$VOICE_IN" \
    --output "$PROJECT/final/final_video.mp4"
fi

echo "[done] $PROJECT/final/final_video.mp4"
