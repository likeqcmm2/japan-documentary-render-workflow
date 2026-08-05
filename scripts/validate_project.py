#!/usr/bin/env python3
import argparse
import json
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


def main():
    parser = argparse.ArgumentParser(description="Validate inputs and generated assets for the render workflow.")
    parser.add_argument("--project", default="/workspace/japan_project")
    parser.add_argument("--input-json", default=None)
    parser.add_argument("--voice", default=None)
    parser.add_argument("--srt", default=None)
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
    print("edit.hardsub", dict(Counter(str((x.get("edit") or {}).get("hardsub")) for x in items)))
    print("text_overlay_count", sum(1 for x in items if (x.get("edit") or {}).get("text_overlay_ja")))
    if len({str(x) for x in source_ids}) != len(source_ids):
        raise SystemExit("Duplicate source IDs in shot list")

    missing_images = []
    missing_ltx = []
    for sid, item in runtime_items:
        image_path = images_dir / f"shot_{sid:03d}.png"
        if not image_path.exists():
            missing_images.append({"runtime_id": sid, "source_id": item.get("id", sid)})
        if (item.get("media_type") or "").lower() == "video":
            video_path = ltx_dir / f"shot_{sid:03d}.mp4"
            if not video_path.exists() or video_path.stat().st_size < 100000:
                missing_ltx.append({"runtime_id": sid, "source_id": item.get("id", sid)})

    print(f"missing_images={len(missing_images)} {missing_images[:40]}")
    print(f"missing_ltx_videos={len(missing_ltx)} {missing_ltx[:40]}")

    if args.voice:
        voice = Path(args.voice)
        print("voice_exists", voice.exists(), voice)
        duration = ffprobe_duration(voice)
        print("voice_duration", duration)
        if duration is not None:
            json_end = float(items[-1]["end"])
            print("json_last_end", json_end, "delta_sec", round(duration - json_end, 3))
    if args.srt:
        srt = Path(args.srt)
        print("srt_exists", srt.exists(), srt, "size", srt.stat().st_size if srt.exists() else None)

    if missing_images or missing_ltx:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
