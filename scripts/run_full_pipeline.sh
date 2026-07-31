#!/usr/bin/env bash
set -euo pipefail

PROJECT="${PROJECT:-/workspace/japan_project}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SHOT_JSON="${1:?Usage: scripts/run_full_pipeline.sh shot_list.json voice.wav subtitles.srt}"
VOICE="${2:?Usage: scripts/run_full_pipeline.sh shot_list.json voice.wav subtitles.srt}"
SRT="${3:?Usage: scripts/run_full_pipeline.sh shot_list.json voice.wav subtitles.srt}"

mkdir -p "$PROJECT/inputs"
cp "$SHOT_JSON" "$PROJECT/inputs/shot_list.json"
cp "$VOICE" "$PROJECT/inputs/voice$(python3 - <<'PY' "$VOICE"
import sys
from pathlib import Path
print(Path(sys.argv[1]).suffix)
PY
)"
cp "$SRT" "$PROJECT/inputs/subtitles.srt"

VOICE_IN="$PROJECT/inputs/voice$(python3 - <<'PY' "$VOICE"
import sys
from pathlib import Path
print(Path(sys.argv[1]).suffix)
PY
)"
SRT_IN="$PROJECT/inputs/subtitles.srt"

(cd "$REPO_DIR" && node scripts/generate_images_from_shot_json.js --input "$PROJECT/inputs/shot_list.json" --output "$PROJECT/generated_images")

python3 "$REPO_DIR/scripts/run_ltx_videos.py" \
  --project "$PROJECT" \
  --input-json "$PROJECT/inputs/shot_list.json" \
  --images-dir "$PROJECT/generated_images" \
  --output-dir "$PROJECT/ltx_videos" \
  --payload "$PROJECT/comfy_workflows/ltx-2.3-i2v.payload.json"

python3 "$REPO_DIR/scripts/validate_project.py" \
  --project "$PROJECT" \
  --input-json "$PROJECT/inputs/shot_list.json" \
  --voice "$VOICE_IN" \
  --srt "$SRT_IN" \
  --images-dir "$PROJECT/generated_images" \
  --ltx-dir "$PROJECT/ltx_videos"

python3 "$REPO_DIR/scripts/render_final_video.py" \
  --project "$PROJECT" \
  --input-json "$PROJECT/inputs/shot_list.json" \
  --images-dir "$PROJECT/generated_images" \
  --ltx-dir "$PROJECT/ltx_videos" \
  --voice "$VOICE_IN" \
  --srt "$SRT_IN" \
  --output "$PROJECT/final/final_video.mp4"

echo "[done] $PROJECT/final/final_video.mp4"
