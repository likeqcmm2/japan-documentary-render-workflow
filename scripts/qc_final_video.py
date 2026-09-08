#!/usr/bin/env python3
import argparse
import json
import subprocess
from pathlib import Path


def run(command, *, capture=False):
    return subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
    )


def probe(path):
    result = run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration,size:stream=index,codec_name,codec_type,width,height,r_frame_rate,nb_frames,sample_rate,channels",
        "-of", "json", str(path),
    ], capture=True)
    return json.loads(result.stdout)


def volume(path):
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"],
        text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True,
    )
    values = {}
    for line in result.stderr.splitlines():
        for key in ("mean_volume", "max_volume"):
            marker = f"{key}:"
            if marker in line:
                values[key] = line.split(marker, 1)[1].strip()
    return values


def main():
    parser = argparse.ArgumentParser(description="Mandatory final video/audio QC.")
    parser.add_argument("--video", required=True)
    parser.add_argument("--voice", required=True)
    parser.add_argument("--json", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--fps", type=int, default=25)
    args = parser.parse_args()

    video = Path(args.video)
    voice = Path(args.voice)
    items = json.loads(Path(args.json).read_text())
    info = probe(video)
    voice_info = probe(voice)
    streams = info["streams"]
    video_stream = next(stream for stream in streams if stream["codec_type"] == "video")
    audio_stream = next(stream for stream in streams if stream["codec_type"] == "audio")
    expected_frames = round(float(items[-1]["end"]) * args.fps)
    actual_frames = int(video_stream["nb_frames"])

    errors = []
    if video_stream.get("codec_name") != "h264": errors.append("video codec is not H.264")
    if (video_stream.get("width"), video_stream.get("height")) != (1920, 1080): errors.append("video is not 1920x1080")
    if video_stream.get("r_frame_rate") != f"{args.fps}/1": errors.append(f"video is not {args.fps}fps")
    if actual_frames != expected_frames: errors.append(f"frame drift: expected={expected_frames} actual={actual_frames}")
    if audio_stream.get("codec_name") != "aac": errors.append("audio codec is not AAC")
    if audio_stream.get("sample_rate") != "48000": errors.append("audio is not 48kHz")
    if int(audio_stream.get("channels", 0)) != 2: errors.append("audio is not stereo")

    duration = float(info["format"]["duration"])
    sample_points = (0.0, max(0.0, duration / 2 - 1), max(0.0, duration - 2))
    for point in sample_points:
        run(["ffmpeg", "-v", "error", "-ss", f"{point:.3f}", "-i", str(video), "-t", "2", "-map", "0:v:0", "-f", "null", "-"])
    run(["ffmpeg", "-v", "error", "-i", str(video), "-map", "0:a:0", "-f", "null", "-"])

    report = {
        "ok": not errors,
        "errors": errors,
        "video": info,
        "voice": voice_info,
        "expected_frames": expected_frames,
        "actual_frames": actual_frames,
        "drift_frames": actual_frames - expected_frames,
        "video_volume": volume(video),
        "voice_volume": volume(voice),
        "decode": {"video_samples": list(sample_points), "full_audio": True},
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
