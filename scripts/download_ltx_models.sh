#!/usr/bin/env bash
set -euo pipefail

# This helper records the model filenames required by comfy_workflows/ltx-2.3-i2v.payload.json.
# On the Vast template used in production, these files were already present.
# If a fresh ComfyUI template is missing them, use HF_TOKEN locally in secrets/.env or export it before running.

COMFY="${COMFY:-/workspace/ComfyUI}"
REPO_ID="${LTX_HF_REPO:-Lightricks/LTX-Video}"

if [ -f "secrets/.env" ]; then
  # shellcheck disable=SC1091
  source "secrets/.env"
fi
if [ -n "${HF_TOKEN:-}" ]; then
  export HF_TOKEN
fi

python3 -m pip install --upgrade huggingface_hub hf_xet >/dev/null

mkdir -p "$COMFY/models/checkpoints" "$COMFY/models/upscale_models" "$COMFY/models/loras" "$COMFY/models/text_encoders"

echo "[download] repo=$REPO_ID"
echo "[download] If a file is not found in this repo, read model_manifest.json and place it manually in the target dir."

huggingface-cli download "$REPO_ID" \
  --include "ltx-2.3-22b-dev-fp8.safetensors" \
  --local-dir "$COMFY/models/checkpoints" || true

huggingface-cli download "$REPO_ID" \
  --include "ltx-2.3-spatial-upscaler-x2-1.1.safetensors" \
  --local-dir "$COMFY/models/upscale_models" || true

huggingface-cli download "$REPO_ID" \
  --include "ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors" \
  --local-dir "$COMFY/models/loras" || true

huggingface-cli download "$REPO_ID" \
  --include "gemma_3_12B_it_fp4_mixed.safetensors" \
  --local-dir "$COMFY/models/text_encoders" || true

echo "[download] done; verify missing files with model_manifest.json and ComfyUI startup logs."
