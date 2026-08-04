# Japan Documentary Render Workflow

Production workflow to render a documentary-style YouTube video from a shot-list JSON, voice-over audio, and SRT subtitles.

This repo packages the exact pipeline used for the production render:

1. Generate one image per JSON shot with OpenAI `gpt-image-2`.
2. For shots where `media_type` is `video`, run LTX 2.3 Image-to-Video in ComfyUI.
3. Render every shot into a fixed-duration clip with FFmpeg.
4. Apply per-shot Ken Burns, grain, vignette, typing overlays, typing sound, and selective hard subtitles.
5. Concatenate all clips, mux voice-over, verify duration, upload to Google Drive with rclone.

The render stage defaults to the optimized renderer: hardsubs are burned during the
main clip encode, static-hold photos use a direct centered crop, and six clips render
concurrently. The encoder settings remain `h264_nvenc`, `CQ 20`, `1920x1080`, and
`25 fps`. The optimized renderer keeps separate resumable caches under
`<project>/render_optimized`.

The repo is intended for a future Codex session: clone it on a new Vast ComfyUI server, provide the input JSON/audio/SRT and local secrets, then run the workflow to produce a Drive link.

## Render Speed And Fallback

`scripts/run_full_pipeline.sh` uses optimized rendering by default:

```bash
RENDER_MODE=optimized RENDER_WORKERS=6 bash scripts/run_full_pipeline.sh shot.json voice.wav subtitles.srt
```

`RENDER_WORKERS=6` is the tested default for the 64-core/128-thread RTX 5090 setup. The optimized renderer
caps the value at 6. This setting completed the 206-shot Edo render in 7m 54.6s with no FFmpeg errors,
which was 44.66% faster than the same optimized render with 3 workers and 76.46% faster than the legacy
render. More workers are intentionally not enabled by default because NVENC sessions, CPU filters, and disk
I/O can compete and reduce reliability.
For a legacy comparison or emergency fallback, run:

```bash
RENDER_MODE=legacy bash scripts/run_full_pipeline.sh shot.json voice.wav subtitles.srt
```

On the Edo benchmark with 206 shots, the legacy render took 33m 36.6s and the
optimized render took 14m 17.6s: 57.47% less wall time (2.35x faster). Both outputs
were 1920x1080, 25 fps, and 1519.041s long. Six sampled frame comparisons had SSIM
between 0.9887 and 0.9974. The typing sound remains mixed per clip intentionally;
moving it to a global timeline is not enabled because it could change shot sync.

A 30-shot concurrency benchmark on the same instance took 98.977s with 2 workers
and 77.044s with 3 workers, a 22.16% improvement, with no FFmpeg errors. The full
6-worker production render was then validated separately before making 6 workers the
default.

## Important Security Rule

Do **not** commit real API keys, Hugging Face tokens, rclone config, Google credentials, or Vast SSH credentials.

Secrets live locally:

- On Mac: `/Users/truongdonghai/Desktop/Japan_Documentary_Render_Secrets`.
- On Vast: `/workspace/japan_project/secrets/.env` or shell environment variables.
- rclone config is configured on the machine, not committed.

See `secrets/README.md`.

For this production setup, the Macbook-local secrets/config folder is:

```text
/Users/truongdonghai/Desktop/Japan_Documentary_Render_Secrets
```

Expected local files:

```text
/Users/truongdonghai/Desktop/Japan_Documentary_Render_Secrets/.env
/Users/truongdonghai/Desktop/Japan_Documentary_Render_Secrets/rclone.conf
```

If `.env` does not exist yet, create it from:

```text
/Users/truongdonghai/Desktop/Japan_Documentary_Render_Secrets/.env.template
```

## Expected Input

Each production run needs:

- A shot-list JSON file. The filename can be anything.
- A voice-over file, usually `.wav`.
- An SRT subtitle file. The filename can be anything.

The scripts normalize these into:

```text
/workspace/japan_project/inputs/shot_list.json
/workspace/japan_project/inputs/voice.wav
/workspace/japan_project/inputs/subtitles.srt
```

## JSON Contract

Each shot should look like this shape:

```json
{
  "id": 1,
  "start": 0,
  "end": 6.529,
  "text_ja": "...",
  "media_type": "photo",
  "shot": "Prompt for GPT Image.",
  "on_screen_text_ja": "Text that should be included inside generated image prompt, or null.",
  "edit": {
    "kenburns_type": "static_hold",
    "kenburns_scale_range": "1.0 -> 1.08",
    "film_grain": "light",
    "vignette": true,
    "text_overlay_ja": "Text shown by renderer with typing effect, or null.",
    "motion_prompt": "Prompt for LTX if media_type is video.",
    "hardsub": "normal"
  }
}
```

Field behavior:

- `media_type: "photo"`: use generated image directly in FFmpeg.
- `media_type: "video"`: first generate image, then run LTX I2V using `edit.motion_prompt`.
- `shot`: base prompt for GPT Image.
- `on_screen_text_ja`: appended to `shot` when generating the still image. It is **not** overlaid by FFmpeg.
- `edit.text_overlay_ja`: rendered by FFmpeg/Pillow as a compact archival-paper plate in upper-left with Yuji Boku font and typing effect. The complete plate treatment (canvas, text, padding, border, and shadow) is scaled to 70% of the original production size; change `ARCHIVAL_OVERLAY_SCALE` in `scripts/render_final_video.py` only if a different global size is needed.
- `edit.hardsub: "normal"`: burn SRT subtitles only inside this shot.
- `edit.hardsub: null`: do not burn SRT subtitles inside this shot.
- `edit.kenburns_type: "none"` or `"static_hold"`: no animated Ken Burns movement.
- Video outputs from LTX are padded to `1920x1080` with black edges if the LTX workflow emits a shorter frame such as `1920x1024`.

## Packaged Reusable Assets

Committed assets:

- `assets/grain.mp4`
- `assets/keyboard-typing-sound-effect-335503.mp3`
- `assets/YujiBoku-Regular.ttf`
- `comfy_workflows/ltx-2.3-i2v.payload.json`

The Comfy payload is the proven production payload copied from:

```text
/opt/comfyui-api-wrapper/payloads/ltx-2.3-i2v.json
```

## Model Requirements

Read `model_manifest.json`.

The proven LTX payload references:

- `Lightricks/LTX-2.3-fp8` -> `ComfyUI/models/checkpoints/ltx-2.3-22b-dev-fp8.safetensors`
- `Comfy-Org/ltx-2` -> `ComfyUI/models/text_encoders/gemma_3_12B_it_fp4_mixed.safetensors`
- `Lightricks/LTX-2.3` -> `ComfyUI/models/latent_upscale_models/ltx-2.3-spatial-upscaler-x2-1.1.safetensors`
- `Comfy-Org/ltx-2.3` -> `ComfyUI/models/loras/ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors`
- `Comfy-Org/ltx-2` -> `ComfyUI/models/loras/gemma-3-12b-it-abliterated_lora_rank64_bf16.safetensors`

On the Vast ComfyUI template used in production, these were already available. On a new server, first check ComfyUI startup logs. If missing, use:

```bash
scripts/download_ltx_models.sh
```

The helper installs `huggingface_hub` and `hf_xet`. If a model is not found in the default repo, place it manually according to `model_manifest.json`.

After download, verify exact model placement:

```bash
python3 - <<'PY'
from pathlib import Path
checks = [
  ("checkpoints", "ltx-2.3-22b-dev-fp8.safetensors"),
  ("text_encoders", "gemma_3_12B_it_fp4_mixed.safetensors"),
  ("latent_upscale_models", "ltx-2.3-spatial-upscaler-x2-1.1.safetensors"),
  ("loras", "ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors"),
  ("loras", "gemma-3-12b-it-abliterated_lora_rank64_bf16.safetensors"),
]
for folder, name in checks:
    path = Path("/workspace/ComfyUI/models") / folder / name
    print(path, path.exists(), path.stat().st_size if path.exists() else None)
PY
```

Expected production-tested sizes:

```text
ltx-2.3-22b-dev-fp8.safetensors                                      29145431166
gemma_3_12B_it_fp4_mixed.safetensors                                  9447702218
ltx-2.3-spatial-upscaler-x2-1.1.safetensors                            995743560
ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors 2741024390
gemma-3-12b-it-abliterated_lora_rank64_bf16.safetensors                628203616
```

After downloading new models, restart ComfyUI so the model registry reloads:

```bash
pgrep -af "ComfyUI|main.py"
kill <COMFY_MAIN_PID> || true
sleep 5
curl -fsS http://127.0.0.1:18188/system_stats >/dev/null && echo comfy_api_ready
```

On the tested Vast templates, killing the ComfyUI `main.py` process lets the supervisor restart it automatically.

## New Vast Server Setup

Assumption: the Vast server uses the same ComfyUI template as the production server:

```text
/workspace/ComfyUI
ComfyUI API: http://127.0.0.1:18188
Project dir: /workspace/japan_project
```

On the Vast server:

```bash
cd /workspace
git clone <PRIVATE_REPO_URL> japan-documentary-render-workflow
cd japan-documentary-render-workflow
scripts/setup_vast.sh
```

`setup_vast.sh` installs required Python packages, Node/npm if the Vast template is missing them, and the repo's npm dependency for OpenAI image generation.

It also installs `fonts-noto-cjk` when Japanese subtitle fonts are missing. Without this package, SRT hardsubs may render as square boxes.

Then create local secrets on Vast:

```bash
cp .env.example secrets/.env
nano secrets/.env
```

Minimum:

```bash
OPENAI_API_KEY=sk-proj-...
HF_TOKEN=hf_...
RCLONE_REMOTE=gdrive
DRIVE_OUTPUT_DIR=Japan_Project_Render_Workflow/final
```

Configure rclone if it is not already configured:

```bash
rclone config
rclone lsd gdrive:
```

If running from the owner's Macbook, prefer copying the existing local secrets/config to the Vast server:

```bash
ssh -p <PORT> root@<HOST> 'mkdir -p /workspace/japan-documentary-render-workflow/secrets /root/.config/rclone && chmod 700 /root/.config/rclone'
scp -P <PORT> /Users/truongdonghai/Desktop/Japan_Documentary_Render_Secrets/.env root@<HOST>:/workspace/japan-documentary-render-workflow/secrets/.env
scp -P <PORT> /Users/truongdonghai/Desktop/Japan_Documentary_Render_Secrets/rclone.conf root@<HOST>:/root/.config/rclone/rclone.conf
ssh -p <PORT> root@<HOST> 'chmod 600 /workspace/japan-documentary-render-workflow/secrets/.env /root/.config/rclone/rclone.conf'
```

If `.env` is missing on the Macbook, ask the user to fill it before running paid API steps.

Important: if you use `rsync --delete` to update the repo folder on Vast, it can remove `secrets/.env` because real secrets are not in git. Always copy `.env` again after syncing/cloning the repo:

```bash
scp -P <PORT> /Users/truongdonghai/Desktop/Japan_Documentary_Render_Secrets/.env root@<HOST>:/workspace/japan-documentary-render-workflow/secrets/.env
ssh -p <PORT> root@<HOST> 'chmod 600 /workspace/japan-documentary-render-workflow/secrets/.env'
```

## Full Run

Put input files anywhere on the Vast server, then run:

```bash
PROJECT=/workspace/japan_project \
scripts/run_full_pipeline.sh /path/to/shot_list.json /path/to/voice.wav /path/to/subtitles.srt
```

This does:

1. Copies input files into `/workspace/japan_project/inputs/`.
2. Runs OpenAI image generation:

```bash
node scripts/generate_images_from_shot_json.js \
  --input /workspace/japan_project/inputs/shot_list.json \
  --output /workspace/japan_project/generated_images
```

Image generation is deliberately fault-tolerant:

- The first pass launches every selected image job. A failed prompt is written to `generated_images/api-run-log.jsonl`, and the batch continues instead of stopping at the first error.
- Each request still has its normal transient-error retries. After the first pass, only the failed images are retried in **3 complete retry rounds**. Images that succeed are removed from the retry list.
- If any image is still failing after all 3 rounds, the command exits non-zero and writes `generated_images/failed-images.json`. The full pipeline stops before LTX, validation, rendering, or upload so the prompts can be inspected safely.
- Fix the prompt or API issue and rerun the same command. Existing successful `shot_###.png` files are skipped, so only missing images are attempted again.

3. Runs LTX for `media_type: video` only:

```bash
python3 scripts/run_ltx_videos.py \
  --project /workspace/japan_project \
  --input-json /workspace/japan_project/inputs/shot_list.json \
  --images-dir /workspace/japan_project/generated_images \
  --output-dir /workspace/japan_project/ltx_videos
```

4. Validates generated assets and audio duration:

```bash
python3 scripts/validate_project.py \
  --project /workspace/japan_project \
  --input-json /workspace/japan_project/inputs/shot_list.json \
  --voice /workspace/japan_project/inputs/voice.wav \
  --srt /workspace/japan_project/inputs/subtitles.srt
```

5. Renders final:

```bash
python3 scripts/render_final_video.py \
  --project /workspace/japan_project \
  --input-json /workspace/japan_project/inputs/shot_list.json \
  --images-dir /workspace/japan_project/generated_images \
  --ltx-dir /workspace/japan_project/ltx_videos \
  --voice /workspace/japan_project/inputs/voice.wav \
  --srt /workspace/japan_project/inputs/subtitles.srt \
  --output /workspace/japan_project/final/final_video.mp4
```

6. Uploads to Drive manually:

```bash
scripts/upload_drive.sh /workspace/japan_project/final/final_video.mp4
```

## Smoke Test on a New Vast Server

Before running full production on a new server, test a tiny subset first. This avoids spending hours before discovering setup issues.

Create a 4-shot subset locally or on Vast:

```bash
python3 - <<'PY'
import json
from pathlib import Path
items = json.loads(Path("/workspace/japan_project/inputs/shot_list.json").read_text())
Path("/workspace/japan_project/inputs/smoke_first4.json").write_text(
    json.dumps(items[:4], ensure_ascii=False, indent=2),
    encoding="utf-8",
)
PY
```

Generate only those images:

```bash
node scripts/generate_images_from_shot_json.js \
  --input /workspace/japan_project/inputs/smoke_first4.json \
  --output /workspace/japan_project/generated_images_smoke
```

Render only the first video shot through LTX:

```bash
python3 - <<'PY'
import json
from pathlib import Path
items = json.loads(Path("/workspace/japan_project/inputs/smoke_first4.json").read_text())
video = [x for x in items if (x.get("media_type") or "").lower() == "video"][:1]
Path("/workspace/japan_project/inputs/smoke_ltx_one.json").write_text(
    json.dumps(video, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
PY

python3 scripts/run_ltx_videos.py \
  --project /workspace/japan_project \
  --input-json /workspace/japan_project/inputs/smoke_ltx_one.json \
  --images-dir /workspace/japan_project/generated_images_smoke \
  --output-dir /workspace/japan_project/ltx_videos_smoke \
  --payload /workspace/japan_project/comfy_workflows/ltx-2.3-i2v.payload.json
```

Render the short smoke video:

```bash
python3 scripts/render_final_video.py \
  --project /workspace/japan_project \
  --input-json /workspace/japan_project/inputs/smoke_first4.json \
  --images-dir /workspace/japan_project/generated_images_smoke \
  --ltx-dir /workspace/japan_project/ltx_videos_smoke \
  --voice /workspace/japan_project/inputs/voice.wav \
  --srt /workspace/japan_project/inputs/subtitles.srt \
  --output /workspace/japan_project/final/smoke_first4.mp4
```

Upload smoke output to verify rclone:

```bash
scripts/upload_drive.sh /workspace/japan_project/final/smoke_first4.mp4 Japan_Project_Render_Workflow/smoke_tests
```

## Timing Expectations

Do not terminate a long run just because it appears slow.

Observed production timings:

- GPT Image generation: many requests run concurrently; usually minutes for ~177 shots.
- LTX I2V: slowest step. A 6-19 second shot can take roughly 40-285 seconds each. A project with ~63 video shots can take hours.
- FFmpeg render: faster than LTX, but still can take several minutes for ~177 clips.
- Upload: depends on file size; the production file was ~1.1GB.

Codex should keep polling and reporting progress. Do not kill the process unless there is a real error.

## Resume Behavior

The scripts are intentionally resumable:

- Image generation skips existing `generated_images/shot_###.png` unless `--force` is passed.
- Image generation records failures, completes the first pass, retries only failed images for 3 rounds, and pauses the workflow with `failed-images.json` if any remain unsuccessful.
- LTX skips existing `ltx_videos/shot_###.mp4`.
- Final render skips existing `clips_final_hardsub/clip_###.mp4`.

If ComfyUI resets or a network connection drops, restart the same command. Already completed files should be skipped.

## Known Production Issue and Fix

During production, ComfyUI reset the local HTTP connection while polling `shot_113`:

```text
Connection reset by peer
```

This was not a prompt/model error. The script now retries Comfy API calls. If the command exits anyway, rerun it; completed video shots are skipped.

## New Vast Troubleshooting Notes

These issues were found and fixed while smoke-testing a brand-new Vast server on July 31, 2026.

### `setup_vast.sh` fails on pip

Symptom:

```text
ERROR: Cannot uninstall pip 24.0, RECORD file not found. The package was installed by debian.
```

Fix:

`setup_vast.sh` no longer upgrades Debian's system pip directly. It retries package installation with `--break-system-packages`.

### `node: command not found`

Symptom:

```text
bash: node: command not found
```

Fix:

`setup_vast.sh` now installs `nodejs npm` with apt if the Vast template is missing Node. OpenAI image generation requires Node.

### Japanese subtitles render as square boxes

Symptom:

```text
□□□□□□
```

Cause: the new Vast template does not have `fonts-noto-cjk`; FFmpeg falls back to DejaVu Sans, which lacks Japanese glyphs.

Fix:

Run `scripts/setup_vast.sh` again. It now installs `fonts-noto-cjk` and refreshes fontconfig. Verify:

```bash
fc-match "Noto Sans CJK JP"
```

Expected:

```text
NotoSansCJK-Regular.ttc: "Noto Sans CJK JP" "Regular"
```

Manual fallback if the setup script cannot install apt packages:

```bash
apt-get update
apt-get install -y fonts-noto-cjk fontconfig
fc-cache -f
fc-match "Noto Sans CJK JP"
```

Official font source if manual download is needed instead of apt:
[Google Noto CJK on GitHub](https://github.com/notofonts/noto-cjk).

### GPT Image says `Missing OPENAI_API_KEY`

Most likely cause: `secrets/.env` was not copied to Vast, or it was removed by repo sync.

Fix:

```bash
scp -P <PORT> /Users/truongdonghai/Desktop/Japan_Documentary_Render_Secrets/.env root@<HOST>:/workspace/japan-documentary-render-workflow/secrets/.env
ssh -p <PORT> root@<HOST> 'chmod 600 /workspace/japan-documentary-render-workflow/secrets/.env'
```

Then rerun image generation.

### `hf download` downloads zero files

Cause: wrong Hugging Face repo/path. The LTX files are spread across multiple repos, not a single `Lightricks/LTX-Video` repo.

Fix:

Use the current `scripts/download_ltx_models.sh`, which downloads these exact files:

```text
Lightricks/LTX-2.3-fp8/ltx-2.3-22b-dev-fp8.safetensors
Lightricks/LTX-2.3/ltx-2.3-spatial-upscaler-x2-1.1.safetensors
Comfy-Org/ltx-2.3/split_files/loras/ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors
Comfy-Org/ltx-2/split_files/loras/gemma-3-12b-it-abliterated_lora_rank64_bf16.safetensors
Comfy-Org/ltx-2/split_files/text_encoders/gemma_3_12B_it_fp4_mixed.safetensors
```

The script moves files downloaded under `split_files/...` into the flat Comfy folders required by the payload.

### LTX models downloaded but Comfy still cannot find them

Cause: ComfyUI was already running before the models were downloaded.

Fix:

Restart ComfyUI and then test `curl http://127.0.0.1:18188/system_stats`.

### FFmpeg/NVENC fails on RTX 5090 template

Symptom:

```text
OpenEncodeSessionEx failed: unsupported device
No capable devices found
```

Fix:

`render_final_video.py` automatically retries failed `h264_nvenc` commands with `libx264`. The output should still be correct, just slower to encode.

## Quality Notes

The final production style includes:

- GPT Image: `gpt-image-2`, `1536x864`, `quality=low`, `n=1`.
- Request pacing: max `15` concurrent, new request every `4s`.
- LTX I2V: request `1920x1080`, `25fps`, duration `ceil(end-start)`.
- FFmpeg render output: `1920x1080`, `25fps`, `h264_nvenc`.
- If a new Vast template reports `h264_nvenc` / `OpenEncodeSessionEx failed` / `unsupported device`, `render_final_video.py` automatically retries that FFmpeg command with `libx264`.
- Photo Ken Burns uses high-resolution intermediate scaling (`scale=8000`) before `zoompan` to avoid jerky motion.
- Real grain asset from `assets/grain.mp4`, not synthetic FFmpeg noise.
- `text_overlay_ja`: upper-left archival-paper plate at 70% scale, Yuji Boku font, typing animation, typing sound trimmed to the typing duration. This setting does not affect YouTube-style SRT subtitles.
- SRT hardsub: YouTube-style small subtitle at bottom, only for shots with `edit.hardsub: "normal"`.

## Final QA Checklist

After render:

```bash
ffprobe -v error -show_entries format=duration,size -of default=nw=1:nk=1 /workspace/japan_project/final/final_video.mp4
```

Compare duration with `end` of the last JSON item. Small drift under ~0.5s is acceptable.

Extract QC frames:

```bash
mkdir -p /workspace/japan_project/final/qc_frames
ffmpeg -y -ss 3 -i /workspace/japan_project/final/final_video.mp4 -frames:v 1 /workspace/japan_project/final/qc_frames/frame_003s.jpg
ffmpeg -y -ss 8 -i /workspace/japan_project/final/final_video.mp4 -frames:v 1 /workspace/japan_project/final/qc_frames/frame_008s.jpg
```

Check:

- A shot with `hardsub: null` has no bottom subtitle.
- A shot with `hardsub: normal` has bottom subtitle.
- `text_overlay_ja` appears as archival-paper plate, not black box.
- Voice-over is present.

## GitHub Usage

The repo should be private.

First push from Mac:

```bash
git init
git add .
git commit -m "Package production documentary render workflow"
gh repo create japan-documentary-render-workflow --private --source=. --remote=origin --push
```

For future updates:

```bash
git add .
git commit -m "Update workflow"
git push
```
