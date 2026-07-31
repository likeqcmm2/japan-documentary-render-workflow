#!/usr/bin/env bash
set -euo pipefail

# This helper records the model filenames required by comfy_workflows/ltx-2.3-i2v.payload.json.
# On the Vast template used in production, these files were already present.
# If a fresh ComfyUI template is missing them, use HF_TOKEN locally in secrets/.env or export it before running.

COMFY="${COMFY:-/workspace/ComfyUI}"
if [ -f "secrets/.env" ]; then
  # shellcheck disable=SC1091
  source "secrets/.env"
fi
if [ -n "${HF_TOKEN:-}" ]; then
  export HF_TOKEN
fi

python3 -m pip install --upgrade huggingface_hub hf_xet >/dev/null 2>&1 || \
  python3 -m pip install --break-system-packages --upgrade huggingface_hub hf_xet >/dev/null

mkdir -p "$COMFY/models/checkpoints" "$COMFY/models/latent_upscale_models" "$COMFY/models/loras" "$COMFY/models/text_encoders"

echo "[download] downloading the exact LTX 2.3 files referenced by model_manifest.json"

HF_ARGS=()
if [ -n "${HF_TOKEN:-}" ]; then
  HF_ARGS+=(--token "$HF_TOKEN")
fi
HF_ARGS+=(--max-workers 16)

hf download "Lightricks/LTX-2.3-fp8" \
  "ltx-2.3-22b-dev-fp8.safetensors" \
  --local-dir "$COMFY/models/checkpoints" "${HF_ARGS[@]}" || true

hf download "Lightricks/LTX-2.3" \
  "ltx-2.3-spatial-upscaler-x2-1.1.safetensors" \
  --local-dir "$COMFY/models/latent_upscale_models" "${HF_ARGS[@]}" || true

hf download "Comfy-Org/ltx-2.3" \
  "split_files/loras/ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors" \
  --local-dir "$COMFY/models/loras" "${HF_ARGS[@]}" || true
mv -f "$COMFY/models/loras/split_files/loras/ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors" "$COMFY/models/loras/" 2>/dev/null || true

hf download "Comfy-Org/ltx-2" \
  "split_files/loras/gemma-3-12b-it-abliterated_lora_rank64_bf16.safetensors" \
  --local-dir "$COMFY/models/loras" "${HF_ARGS[@]}" || true
mv -f "$COMFY/models/loras/split_files/loras/gemma-3-12b-it-abliterated_lora_rank64_bf16.safetensors" "$COMFY/models/loras/" 2>/dev/null || true

hf download "Comfy-Org/ltx-2" \
  "split_files/text_encoders/gemma_3_12B_it_fp4_mixed.safetensors" \
  --local-dir "$COMFY/models/text_encoders" "${HF_ARGS[@]}" || true
mv -f "$COMFY/models/text_encoders/split_files/text_encoders/gemma_3_12B_it_fp4_mixed.safetensors" "$COMFY/models/text_encoders/" 2>/dev/null || true

find "$COMFY/models/loras/split_files" "$COMFY/models/text_encoders/split_files" -type d -empty -delete 2>/dev/null || true

echo "[download] done; verify missing files with model_manifest.json and ComfyUI startup logs."
