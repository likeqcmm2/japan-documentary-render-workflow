#!/usr/bin/env bash
set -euo pipefail

# Exact model set for comfy_workflows/ltx-2.5-nvfp4-i2v.payload.json.
# Pass a read-only Hugging Face token through HF_TOKEN; never commit it.
COMFY="${COMFY:-/workspace/ComfyUI}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-/venv/main}"

if [ -f "$REPO_DIR/secrets/.env" ]; then
  # shellcheck disable=SC1091
  source "$REPO_DIR/secrets/.env"
fi

"$VENV/bin/python" -m pip install --upgrade huggingface_hub hf_xet safetensors >/dev/null
export PATH="$VENV/bin:$PATH"
export HF_XET_HIGH_PERFORMANCE="${HF_XET_HIGH_PERFORMANCE:-1}"

HF_ARGS=(--max-workers "${HF_MAX_WORKERS:-16}")
if [ -n "${HF_TOKEN:-}" ]; then
  HF_ARGS+=(--token "$HF_TOKEN")
fi

mkdir -p "$COMFY/models"/{diffusion_models,text_encoders,vae,latent_upscale_models}

download_one() {
  local repo="$1" remote_path="$2" target_dir="$3"
  local filename="${remote_path##*/}"
  local staging
  staging="$(mktemp -d "$COMFY/models/.ltx25-download.XXXXXX")"
  echo "[download] $repo/$remote_path -> $target_dir/$filename"
  hf download "$repo" "$remote_path" --local-dir "$staging" "${HF_ARGS[@]}"
  install -m 0644 "$staging/$remote_path" "$target_dir/$filename"
  rm -rf "$staging"
}

download_one "BennyDaBall/LTX-2.5-22b-distilled-nvfp4-comfy" \
  "ltx-2.5-22b-distilled-transformer-nvfp4-comfy.safetensors" "$COMFY/models/diffusion_models"
download_one "Lightricks/LTX-2.5" \
  "text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors" "$COMFY/models/text_encoders"
download_one "Comfy-Org/gemma-4" \
  "text_encoders/gemma4_e2b_it_bf16.safetensors" "$COMFY/models/text_encoders"
download_one "Lightricks/LTX-2.5" \
  "vae/ltx-2.5-video-vae-conv-bf16.safetensors" "$COMFY/models/vae"
download_one "Lightricks/LTX-2.5" \
  "vae/ltx-2.5-audio-vae-bf16.safetensors" "$COMFY/models/vae"
download_one "Lightricks/LTX-2.5" \
  "latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors" "$COMFY/models/latent_upscale_models"

COMFY="$COMFY" MANIFEST="$REPO_DIR/model_manifest.json" "$VENV/bin/python" - <<'PY'
import json, os
from pathlib import Path
from safetensors import safe_open

comfy = Path(os.environ["COMFY"])
manifest = json.loads(Path(os.environ["MANIFEST"]).read_text())
for model in manifest["models_referenced_by_payload"]:
    path = comfy / model["target_dir_hint"].split("ComfyUI/models/", 1)[1] / model["filename"]
    actual = path.stat().st_size if path.exists() else -1
    expected = model["expected_bytes"]
    if actual != expected:
        raise SystemExit(f"size mismatch: {path} expected={expected} actual={actual}")
    print(f"[verify] {path.name} bytes={actual}")

nvfp4 = comfy / "models/diffusion_models/ltx-2.5-22b-distilled-transformer-nvfp4-comfy.safetensors"
with safe_open(nvfp4, framework="pt", device="cpu") as f:
    markers = sum(key.endswith(".comfy_quant") for key in f.keys())
if markers != 1176:
    raise SystemExit(f"wrong Benny NVFP4 marker count: expected=1176 actual={markers}")
print("[verify] Benny NVFP4 .comfy_quant markers=1176")
PY

echo "[download] all LTX 2.5 models downloaded and verified"
