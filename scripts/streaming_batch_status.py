#!/usr/bin/env python3
import argparse
import json
import sqlite3
from pathlib import Path


def count(folder, pattern):
    return sum(1 for _ in folder.glob(pattern)) if folder.exists() else 0


def main():
    parser = argparse.ArgumentParser(description="Report streaming batch state and artifact progress.")
    parser.add_argument("manifest")
    parser.add_argument("--project-root", default="/workspace/projects")
    args = parser.parse_args()
    manifest = Path(args.manifest).resolve()
    data = json.loads(manifest.read_text())
    state_path = manifest.with_suffix(".state.sqlite3")
    phases = {}
    if state_path.exists():
        with sqlite3.connect(state_path) as db:
            for production, phase, status, updated_at, detail in db.execute(
                "SELECT production,phase,status,updated_at,detail FROM phases ORDER BY production,phase"
            ):
                phases.setdefault(production, {})[phase] = {
                    "status": status,
                    "updated_at": updated_at,
                    "detail": json.loads(detail) if detail else None,
                }
    output = []
    for raw in data.get("productions", []):
        shot_json = Path(raw["shot_json"])
        if not shot_json.is_absolute(): shot_json = manifest.parent / shot_json
        name = raw.get("name") or shot_json.stem.removesuffix("_final")
        project = Path(raw.get("project") or Path(args.project_root) / name)
        items = json.loads(shot_json.read_text())
        video_total = sum(1 for item in items if (item.get("media_type") or "").lower() == "video")
        final_dir = project / "final"
        output.append({
            "name": name,
            "shots": len(items),
            "images": count(project / "generated_images", "shot_*.png"),
            "ltx": count(project / "ltx_videos", "shot_*.mp4"),
            "ltx_total": video_total,
            "clips": count(project / "render_optimized" / "clips_final_frame_locked", "clip_*.mp4"),
            "finals": [str(path) for path in final_dir.glob("*_final_video.mp4")],
            "delivery": json.loads((final_dir / "drive_delivery.json").read_text()) if (final_dir / "drive_delivery.json").exists() else None,
            "phases": phases.get(name, {}),
        })
    print(json.dumps({"manifest": str(manifest), "productions": output}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
