#!/usr/bin/env bash
set -euo pipefail

COMFY="${COMFY:-/workspace/ComfyUI}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-/venv/main}"

if [ -f "$REPO_DIR/secrets/.env" ]; then
  # shellcheck disable=SC1091
  source "$REPO_DIR/secrets/.env"
fi

if [ -z "${HF_TOKEN:-}" ]; then
  echo "[download] HF_TOKEN is required in secrets/.env or the environment" >&2
  exit 1
fi

"$VENV/bin/python" -m pip install --upgrade huggingface_hub hf_xet >/dev/null
export HF_XET_HIGH_PERFORMANCE="${HF_XET_HIGH_PERFORMANCE:-1}"
export HF_HUB_DISABLE_XET=0

mkdir -p "$COMFY/models"/{diffusion_models,text_encoders,vae}

COMFY="$COMFY" MANIFEST="$REPO_DIR/model_manifest.json" "$VENV/bin/python" - <<'PY'
import json
import os
from pathlib import Path
from huggingface_hub import hf_hub_download

manifest = json.loads(Path(os.environ["MANIFEST"]).read_text())
comfy = Path(os.environ["COMFY"])
token = os.environ.get("HF_TOKEN") or None
for model in manifest["models_referenced_by_payload"]:
    dest = comfy / "models" / model["target_dir_hint"].split("ComfyUI/models/", 1)[1] / model["filename"]
    if dest.is_file() and dest.stat().st_size == model["expected_bytes"]:
        print(f"[verify] {dest.name} bytes={dest.stat().st_size} (cached)", flush=True)
        continue
    print(f"[download] {model['hf_repo']}/{model['hf_path']}", flush=True)
    path = hf_hub_download(
        repo_id=model["hf_repo"], filename=model["hf_path"],
        local_dir=comfy / "models", token=token,
    )
    if Path(path) != dest:
        raise SystemExit(f"download path mismatch: {path} expected={dest}")
    actual = dest.stat().st_size
    if actual != model["expected_bytes"]:
        raise SystemExit(f"model size mismatch: {dest.name} expected={model['expected_bytes']} actual={actual}")
    print(f"[verify] {dest.name} bytes={actual}", flush=True)
print("[download] FastH3 model set is ready", flush=True)
PY
