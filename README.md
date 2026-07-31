# Japan Documentary Render Workflow

Production workflow to render a documentary-style YouTube video from a shot-list JSON, voice-over audio, and SRT subtitles.

This repo packages the exact pipeline used for the production render:

1. Generate one image per JSON shot with OpenAI `gpt-image-2`.
2. For shots where `media_type` is `video`, run LTX 2.3 Image-to-Video in ComfyUI.
3. Render every shot into a fixed-duration clip with FFmpeg.
4. Apply per-shot Ken Burns, grain, vignette, typing overlays, typing sound, and selective hard subtitles.
5. Concatenate all clips, mux voice-over, verify duration, upload to Google Drive with rclone.

The repo is intended for a future Codex session: clone it on a new Vast ComfyUI server, provide the input JSON/audio/SRT and local secrets, then run the workflow to produce a Drive link.

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
- `edit.text_overlay_ja`: rendered by FFmpeg/Pillow as an archival-paper plate in upper-left with Yuji Boku font and typing effect.
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
- LTX skips existing `ltx_videos/shot_###.mp4`.
- Final render skips existing `clips_final_hardsub/clip_###.mp4`.

If ComfyUI resets or a network connection drops, restart the same command. Already completed files should be skipped.

## Known Production Issue and Fix

During production, ComfyUI reset the local HTTP connection while polling `shot_113`:

```text
Connection reset by peer
```

This was not a prompt/model error. The script now retries Comfy API calls. If the command exits anyway, rerun it; completed video shots are skipped.

## Quality Notes

The final production style includes:

- GPT Image: `gpt-image-2`, `1536x864`, `quality=low`, `n=1`.
- Request pacing: max `15` concurrent, new request every `4s`.
- LTX I2V: request `1920x1080`, `25fps`, duration `ceil(end-start)`.
- FFmpeg render output: `1920x1080`, `25fps`, `h264_nvenc`.
- If a new Vast template reports `h264_nvenc` / `OpenEncodeSessionEx failed` / `unsupported device`, `render_final_video.py` automatically retries that FFmpeg command with `libx264`.
- Photo Ken Burns uses high-resolution intermediate scaling (`scale=8000`) before `zoompan` to avoid jerky motion.
- Real grain asset from `assets/grain.mp4`, not synthetic FFmpeg noise.
- `text_overlay_ja`: upper-left archival-paper plate, Yuji Boku font, typing animation, typing sound trimmed to the typing duration.
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
