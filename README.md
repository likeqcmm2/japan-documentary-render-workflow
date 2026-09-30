# Japan Documentary Render Workflow

Produce a documentary-style YouTube video from a shot-list JSON and voice-over audio. For each `media_type: "video"` shot, this version animates its generated still with **FastVideo FastH3 8-Step V2** in ComfyUI. Static shots, timeline assembly, overlays, voice-over, QC, and Drive delivery retain the original pipeline.

## Pipeline

1. Generate a 1536×864 PNG for each shot with OpenAI `gpt-image-2.5-sunburst`. Successful images are cached; failed requests are retried for three rounds.
2. Feed each video shot's PNG and `edit.motion_prompt` to the video-only FastH3 graph. Its three models are listed in `model_manifest.json`.
3. Normalize each FastH3 source from native 24 fps to **1280×704, 25 fps, no audio**, with exactly `round(end * 25) - round(start * 25)` frames. The source PNG is center-cropped by a small amount before scaling, avoiding aspect distortion.
4. Render static and video shots into the absolute 25 fps timeline. Retain Ken Burns, film grain, vignette, Japanese archival text plates, and typing effects.
5. Concatenate the frame-locked clips, mix typing sounds with voice-over, verify zero frame drift, and upload the final 1920×1080 H.264/AAC video with rclone.

The 1280×704 FastH3 source is fitted without stretching into 1920×1080. This preserves the original roughly 12-pixel black bars above and below video shots. Photo handling is unchanged. Final intermediates contain no audio; ComfyUI does not use an audio branch or audio VAE.

The previous LTX graph and models are no longer part of this workflow. Old `ltx_videos` caches remain untouched but are not accepted as FastH3 output. New source clips use `fasth3_videos`, and final clip caches use `render_fasth3_optimized` so stale LTX renders cannot be reused. Generated PNGs remain reusable.

## Input contract

Each production needs a shot-list JSON file and voice-over audio (`.wav` or `.mp3`). JSON array order is the timeline; a one-based runtime ID determines reusable filenames even when a source `id` is a comma-separated group.

```json
{
  "id": 1,
  "start": 0,
  "end": 6.529,
  "media_type": "video",
  "shot": "Image-generation prompt",
  "on_screen_text_ja": null,
  "edit": {
    "motion_prompt": "Describe movement for FastH3",
    "film_grain": "light",
    "vignette": true,
    "text_overlay_ja": "Optional Japanese plate"
  }
}
```

- `media_type: "photo"`: use the generated PNG in the existing static renderer.
- `media_type: "video"`: animate the PNG with `edit.motion_prompt`. If it is absent, the script falls back to `shot`.
- `on_screen_text_ja`: appended to the image prompt, not rendered as an FFmpeg overlay.
- `edit.text_overlay_ja`: archival-paper plate and typing sound on the final timeline.
- Legacy `edit.hardsub` is ignored; SRT burning is unsupported.

Absolute shot boundaries are rounded once to 25 fps. The final video's frame count is `round(last_shot.end * 25)`; per-shot rounding is never accumulated.

## New Vast server setup

Use a ComfyUI Vast image with an RTX 5090 or another Blackwell GPU with enough VRAM. The FastH3 model files total about **40 GB** on disk. The tested 1344×768 configuration used nearly the full 32 GB GPU memory; the production setting here is 1280×704 and must receive a one-shot smoke test on the new server.

```bash
cd /workspace
git clone https://github.com/likeqcmm2/japan-documentary-render-workflow.git
cd japan-documentary-render-workflow
# Copy secrets/.env from your Mac first; see below.
scripts/setup_vast.sh
```

`setup_vast.sh`:

- updates the ComfyUI Git checkout to the **latest `origin/master` commit** with a fast-forward merge, then installs its latest `requirements.txt` into `/venv/main`;
- stops with a clear error if tracked ComfyUI files are locally modified or the checkout cannot fast-forward, instead of overwriting them;
- installs Python, Node, npm, and Japanese-font dependencies as needed;
- downloads only the three FastH3 model files from `FastVideo/FastVideo-FastH3-Comfy` using `hf_xet` high-performance mode and checks exact byte sizes;
- restarts the configured ComfyUI supervisor service and waits for all required FastH3 nodes and models to appear in `/object_info`.

Set `COMFY`, `VENV`, `COMFY_SERVICE`, and `COMFY_URL` if the ComfyUI installation differs from `/workspace/ComfyUI`, `/venv/main`, `comfyui`, and `http://127.0.0.1:18188`. For a second independent GPU/ComfyUI root, run setup again with those four variables pointing to that root and service. The two instances need separate API ports, input/output/user/temp folders; model files may be shared read-only.

The Hugging Face token belongs in `secrets/.env` or the process environment. On the owner's Mac, the local secret files are under `/Users/truongdonghai/Desktop/Japan_Documentary_Render_Secrets`. Copy `.env` over SSH to the cloned repo's `secrets/.env` **before running setup** and set mode `600`. Never commit tokens, OpenAI keys, rclone config, or Vast credentials. Configure rclone separately when Drive upload is needed. `SKIP_MODEL_DOWNLOAD=1` is available only when all three exact models have already been installed.

## Single production

```bash
PROJECT=/workspace/japan_project \
  bash scripts/run_full_pipeline.sh /path/to/shot_list.json /path/to/voice.wav
```

This generates images, runs FastH3 only for video shots, validates source assets, renders all clips, combines audio, and writes `/workspace/japan_project/final/final_video.mp4`. It does **not** upload automatically; upload manually with:

```bash
scripts/upload_drive.sh /workspace/japan_project/final/final_video.mp4
```

Two independent ComfyUI instances can split video shots:

```bash
FASTH3_WORKERS=2 \
FASTH3_COMFY_URLS=http://127.0.0.1:18188,http://127.0.0.1:18189 \
FASTH3_COMFY_DIRS=/workspace/ComfyUI,/workspace/ComfyUI-gpu1 \
RENDER_WORKERS=6 \
  bash scripts/run_full_pipeline.sh /path/to/shot_list.json /path/to/voice.wav
```

Workers keep stable runtime IDs and write disjoint clip names. `RENDER_MODE=legacy` remains available. The optimized renderer uses up to six concurrent clip jobs and the existing NVENC settings (`h264_nvenc`, CQ 20), with a `libx264` fallback when NVENC is unavailable.

## Streaming batch

Create a manifest like `examples/streaming_batch.example.json`, then inspect the plan:

```bash
python3 scripts/run_streaming_batch.py /workspace/inputs/batch.json --plan
```

Run the batch:

```bash
FASTH3_WORKERS=2 \
FASTH3_COMFY_URLS=http://127.0.0.1:18188,http://127.0.0.1:18189 \
FASTH3_COMFY_DIRS=/workspace/ComfyUI,/workspace/ComfyUI-gpu1 \
FASTH3_COMFY_SERVICES=comfyui,comfyui-gpu1 \
RENDER_WORKERS=6 \
  python3 scripts/run_streaming_batch.py /workspace/inputs/batch.json
```

For a rented server, use `nohup` and inspect durable status with `python3 scripts/streaming_batch_status.py /workspace/inputs/batch.json`. Use `--no-upload` to render and QC locally without Drive. The SQLite ledger beside the manifest records phases; validated PNGs, FastH3 clips, and final clips are reused on resume.

The batch keeps four resource lanes: image generation, one FastH3 worker per GPU, static CPU clip rendering, and serialized final rendering/QC/upload. Each GPU takes the next available shot from a shared queue. ComfyUI is recycled between bounded waves and productions; `FASTH3_WAVE_PER_WORKER` (default 30) can be lowered after observing host RAM usage. See `docs/STREAMING_BATCH_ARCHITECTURE.md`.

## Smoke test before a full production

On a new Vast server, generate or reuse the PNG for one video shot and run:

```bash
python3 scripts/run_fasth3_videos.py \
  --project /workspace/japan_project \
  --input-json /workspace/japan_project/inputs/shot_list.json \
  --images-dir /workspace/japan_project/generated_images \
  --runtime-ids 1
```

Replace `1` with a runtime ID whose `media_type` is `video`. The output must be **1280×704, 25 fps, no audio**, with the exact number of timeline frames. Then run `scripts/validate_project.py` when all required images and clips are available, and render a short production subset to verify 1920×1080 video, voice-over, and zero frame drift. The earlier FastH3 tests were successful at 1344×768; 1280×704 needs this confirmation on the new server. The example shot list includes video shots up to 19 seconds, beyond the roughly 15-second trained range noted by the ComfyUI node; smoke-test a longest shot before starting a large batch.

## Resume and QC

- Image generation skips existing valid PNGs and retries failed jobs in three rounds. A persistent failure writes `generated_images/failed-images.json` and stops the production before rendering.
- FastH3 caches include a fingerprint of the input PNG, prompt, payload, duration, and seed. Only a matching 1280×704, 25 fps, silent clip with the exact frame count is reused. Rendered clips also track their source and edit settings, so changed FastH3 output cannot reuse an older final clip.
- The final renderer validates each frame-locked clip, concatenation, and final file. The batch additionally checks H.264 1920×1080 at 25 fps, AAC stereo 48 kHz, video samples, full audio decode, and Drive byte size/link when uploading.
- `scripts/run_fasth3_videos.py` retries transient ComfyUI API failures and can be rerun after a service restart. Successful clips are skipped.
- Secrets remain outside Git. See `secrets/README.md` and `.env.example`.

A 15-second FastH3 render at **1344×768** took about **4 minutes 15–19 seconds** on the tested RTX 5090. This is a measured reference, not a promise for 1280×704 or a full batch. The previous LTX speed benchmarks do not apply to FastH3.
