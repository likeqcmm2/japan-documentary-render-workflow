#!/usr/bin/env bash
set -euo pipefail

FILE="${1:?Usage: scripts/upload_drive.sh /path/to/final.mp4 [drive_folder]}"
DRIVE_FOLDER="${2:-${DRIVE_OUTPUT_DIR:-Japan_Project_Render_Workflow/final}}"
REMOTE="${RCLONE_REMOTE:-gdrive}"

if [ -f "secrets/.env" ]; then
  # shellcheck disable=SC1091
  source "secrets/.env"
fi

echo "[upload] $FILE -> $REMOTE:$DRIVE_FOLDER"
rclone copy "$FILE" "$REMOTE:$DRIVE_FOLDER" --progress
rclone link "$REMOTE:$DRIVE_FOLDER/$(basename "$FILE")"
