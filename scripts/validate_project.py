#!/usr/bin/env python3
import argparse
import json
import math
import subprocess
from collections import Counter
from pathlib import Path


def ffprobe_duration(path: Path):
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            text=True,
        ).strip()
        return float(out)
    except Exception:
        return None


def ffprobe_video(path: Path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
            check=True, capture_output=True, text=True,
        )
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        return int(stream["width"]), int(stream["height"]), stream["r_frame_rate"], float(data["format"]["duration"])
    except (subprocess.SubprocessError, KeyError, ValueError, json.JSONDecodeError, IndexError):
        return None


def main():
    parser = argparse.ArgumentParser(description="Validate inputs and generated assets for the render workflow.")
    parser.add_argument("--project", default="/workspace/japan_project")
    parser.add_argument("--input-json", default=None)
    parser.add_argument("--voice", default=None)
    parser.add_argument("--images-dir", default=None)
    parser.add_argument("--ltx-dir", default=None)
    args = parser.parse_args()

    project = Path(args.project)
    input_json = Path(args.input_json) if args.input_json else project / "inputs" / "shot_list.json"
    images_dir = Path(args.images_dir) if args.images_dir else project / "generated_images"
    ltx_dir = Path(args.ltx_dir) if args.ltx_dir else project / "ltx_videos"

    items = json.loads(input_json.read_text())
    runtime_items = list(enumerate(items, 1))
    source_ids = [x.get("id", runtime_id) for runtime_id, x in runtime_items]
    print(f"items={len(items)} first_source_id={source_ids[0]} last_source_id={source_ids[-1]}")
    print("media", dict(Counter((x.get("media_type") or "").lower() for x in items)))
    print("text_overlay_count", sum(1 for x in items if (x.get("edit") or {}).get("text_overlay_ja")))
    if len({str(x) for x in source_ids}) != len(source_ids):
        raise SystemExit("Duplicate source IDs in shot list")

    missing_images = []
    missing_ltx = []
    invalid_ltx = []
    for sid, item in runtime_items:
        image_path = images_dir / f"shot_{sid:03d}.png"
        if not image_path.exists():
            missing_images.append({"runtime_id": sid, "source_id": item.get("id", sid)})
        if (item.get("media_type") or "").lower() == "video":
            video_path = ltx_dir / f"shot_{sid:03d}.mp4"
            if not video_path.exists() or video_path.stat().st_size < 100000:
                missing_ltx.append({"runtime_id": sid, "source_id": item.get("id", sid)})
            else:
                info = ffprobe_video(video_path)
                expected_duration = max(1, int(math.ceil(float(item["end"]) - float(item["start"]))))
                if not info or info[0] < 1200 or info[1] < 672 or info[2] != "25/1" or abs(info[3] - expected_duration) > 1.0:
                    invalid_ltx.append({"runtime_id": sid, "source_id": item.get("id", sid), "probe": info, "expected_duration": expected_duration})

    print(f"missing_images={len(missing_images)} {missing_images[:40]}")
    print(f"missing_ltx_videos={len(missing_ltx)} {missing_ltx[:40]}")
    print(f"invalid_ltx_videos={len(invalid_ltx)} {invalid_ltx[:40]}")

    if args.voice:
        voice = Path(args.voice)
        print("voice_exists", voice.exists(), voice)
        duration = ffprobe_duration(voice)
        print("voice_duration", duration)
        if duration is not None:
            json_end = float(items[-1]["end"])
            print("json_last_end", json_end, "delta_sec", round(duration - json_end, 3))
    if missing_images or missing_ltx or invalid_ltx:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
