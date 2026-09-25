#!/usr/bin/env bash
set -euo pipefail

COMFY="${COMFY:-/workspace/ComfyUI}"
VENV="${VENV:-/venv/main}"

if [ ! -d "$COMFY/.git" ]; then
  echo "[comfy] expected a Git checkout at $COMFY" >&2
  exit 1
fi
if [ -n "$(git -C "$COMFY" status --porcelain --untracked-files=no)" ]; then
  echo "[comfy] tracked files contain local changes; refusing to overwrite them: $COMFY" >&2
  exit 1
fi

git -C "$COMFY" fetch origin master
git -C "$COMFY" merge --ff-only origin/master
"$VENV/bin/python" -m pip install -r "$COMFY/requirements.txt"
printf '[comfy] version_commit=%s\n' "$(git -C "$COMFY" rev-parse HEAD)"
