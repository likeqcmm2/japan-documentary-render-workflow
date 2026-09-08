# Streaming Batch Architecture

## Objective

Keep expensive LTX GPUs supplied while independent image API, CPU rendering,
quality control, and Drive upload work proceeds concurrently. Preserve the
existing per-production artifact layout so every stage remains independently
resumable.

## Resource lanes

The orchestrator uses four bounded lanes:

1. **Image lane (one dispatcher):** runs the existing 30-concurrent, two-second
   paced image generator. It completes video/LTX source images for every
   production before static images.
2. **LTX lane (one worker per GPU):** consumes an image as soon as its atomic PNG
   appears. Work advances in manifest order, but a completed production releases
   the GPUs immediately; it does not wait for final rendering or upload.
3. **Static CPU lane:** renders all non-video shots after a production's image
   pass. This lane is independent from LTX.
4. **Final CPU lane:** validates assets, renders remaining video clips, concatenates,
   muxes voice audio, performs QC, and uploads. It is serialized so multiple
   full-video encodes do not exhaust CPU, memory, or disk bandwidth.

The static and final lanes may overlap. Each uses the renderer's bounded worker
count, so operators should benchmark `RENDER_WORKERS` for the server's CPU and
storage.

## Dependency graph

```text
video image -> LTX clip -----------+
                                      -> frame-locked clip -> concat -> mux -> QC -> Drive
static image -> static CPU clip ---+
```

An atomic rename publishes each generated PNG. LTX never observes a partially
written API response. A failure sentinel wakes waiting LTX workers immediately
when upstream image generation cannot complete.

## Memory control

ComfyUI/LTX can retain large pinned host allocations between prompts. On a
dual-5090 server with a roughly 242 GiB cgroup limit, unbounded runs have reached
the OOM killer after approximately 75-85 total clips. Streaming mode therefore
defaults to waves of 30 clips per GPU:

1. Complete and validate the current wave.
2. Restart configured ComfyUI supervisor services.
3. Wait for both `/object_info` APIs and required model registry entries.
4. Start the next wave from the existing clip cache.

The same controlled recycle occurs between productions. Tune the wave downward
when the cgroup memory limit is smaller; increase it only after observing stable
`memory.current` and zero new `oom_kill` events.

## Durability and recovery

The orchestrator writes `<manifest>.state.sqlite3` with phase status and an event
history. The database is observability metadata, not a substitute for artifact
validation. On restart:

- existing PNGs are skipped;
- LTX clips are skipped only when ffprobe validates resolution, FPS, and duration;
- rendered clips are skipped only when frame count, FPS, and absence of audio are
  correct;
- final delivery is accepted only after mandatory QC and Drive size verification.

It is safe to rerun the same manifest. The original single-production scripts
remain a recovery interface for any individual project directory.

## Scheduling policy

- Manifest order controls delivery priority.
- All video source images are dispatched before static assets across the batch.
- Each LTX wave uses online duration balancing while retaining ascending runtime
  order inside each GPU queue. This avoids assigning all long clips to one GPU
  without making a worker wait for a late source image.
- A production enters finalization only after its images, static clips, and LTX
  clips are complete.
- The next production may enter LTX while the previous production is rendering,
  undergoing QC, or uploading.

## Completion contract

A production is complete only when:

- every expected image and LTX source validates;
- every final clip matches its absolute frame interval;
- final output is H.264 1920x1080 at 25 fps with zero frame drift;
- audio is AAC stereo 48 kHz and fully decodes;
- beginning, middle, and ending video samples decode;
- volume measurements for final and source voice are recorded;
- Drive reports the same byte size and returns a usable link.
