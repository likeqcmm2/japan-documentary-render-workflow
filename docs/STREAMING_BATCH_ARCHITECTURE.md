# Streaming Batch Architecture

## Objective

Keep expensive FastH3 GPUs supplied while independent image API, CPU rendering,
quality control, and Drive upload work proceeds concurrently. Preserve the
existing per-production artifact layout so every stage remains independently
resumable.

## Resource lanes

The orchestrator uses four bounded lanes:

1. **Image lane (one dispatcher):** runs the existing 30-concurrent, two-second
   paced image generator. It completes video/FastH3 source images for every
   production before static images.
2. **FastH3 lane (one worker per GPU):** consumes an image as soon as its atomic PNG
   appears. Work advances in manifest order, but a completed production releases
   the GPUs immediately; it does not wait for final rendering or upload.
3. **Static CPU lane:** renders all non-video shots after a production's image
   pass. This lane is independent from FastH3.
4. **Final CPU lane:** validates assets, renders remaining video clips, concatenates,
   muxes voice audio, performs QC, and uploads. It is serialized so multiple
   full-video encodes do not exhaust CPU, memory, or disk bandwidth.

The static and final lanes may overlap. Each uses the renderer's bounded worker
count, so operators should benchmark `RENDER_WORKERS` for the server's CPU and
storage.

## Dependency graph

```text
video image -> FastH3 clip -----------+
                                      -> frame-locked clip -> concat -> mux -> QC -> Drive
static image -> static CPU clip ---+
```

An atomic rename publishes each generated PNG. FastH3 never observes a partially
written API response. A failure sentinel wakes waiting FastH3 workers immediately
when upstream image generation cannot complete.

## Memory control

ComfyUI/FastH3 can retain large pinned host allocations between prompts. The prior LTX pipeline reached the OOM killer after roughly 75-85 clips on a dual-5090 server with a 242 GiB cgroup limit. FastH3 has not yet been benchmarked for long batch runs. Streaming mode retains a configurable default of 30 clips per GPU; lower `FASTH3_WAVE_PER_WORKER` if host RAM rises:

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
- FastH3 clips are skipped only when the source fingerprint matches and ffprobe validates resolution, FPS, frame count, and absence of audio;
- rendered clips are skipped only when their source/edit fingerprint, frame count, FPS, and absence of audio are correct;
- final delivery is accepted only after mandatory QC and Drive size verification.

It is safe to rerun the same manifest. The original single-production scripts
remain a recovery interface for any individual project directory.

## Scheduling policy

- Manifest order controls delivery priority.
- All video source images are dispatched before static assets across the batch.
- Each FastH3 wave uses one shared FIFO task queue. Each GPU atomically takes the
  next runtime ID when it becomes free, so a faster GPU never waits for a fixed
  pre-assigned shard to finish.
- A production enters finalization only after its images, static clips, and FastH3
  clips are complete.
- The next production may enter FastH3 while the previous production is rendering,
  undergoing QC, or uploading.

## Completion contract

A production is complete only when:

- every expected image and FastH3 source validates;
- every final clip matches its absolute frame interval;
- final output is H.264 1920x1080 at 25 fps with zero frame drift;
- audio is AAC stereo 48 kHz and fully decodes;
- beginning, middle, and ending video samples decode;
- volume measurements for final and source voice are recorded;
- Drive reports the same byte size and returns a usable link.
