import json
import argparse
from concurrent.futures import ThreadPoolExecutor
import math
import re
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

def parse_args():
    parser = argparse.ArgumentParser(description="Render final documentary video from shot JSON, generated images, LTX videos, WAV, and SRT.")
    parser.add_argument("--project", default="/workspace/japan_project")
    parser.add_argument("--input-json", default=None, help="Shot list JSON. Defaults to <project>/inputs/shot_list.json.")
    parser.add_argument("--images-dir", default=None, help="GPT Image output directory. Defaults to <project>/generated_images.")
    parser.add_argument("--ltx-dir", default=None, help="LTX video directory. Defaults to <project>/ltx_videos.")
    parser.add_argument("--voice", required=True, help="Voice-over WAV/MP3 path.")
    parser.add_argument("--srt", required=True, help="Subtitle SRT path.")
    parser.add_argument("--output", default=None, help="Final MP4 path. Defaults to <project>/final/final_video.mp4.")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--optimized", action="store_true", help="Use one-pass subtitles, static-hold fast path, and bounded parallel rendering.")
    parser.add_argument("--workers", type=int, default=6, help="Concurrent optimized clip renders (default: 6).")
    parser.add_argument("--render-root", default=None, help="Optional directory for optimized render caches and intermediate files.")
    return parser.parse_args()

args = parse_args()
PROJECT = Path(args.project)
INPUT_JSON = Path(args.input_json) if args.input_json else PROJECT / "inputs" / "shot_list.json"
IMAGES_DIR = Path(args.images_dir) if args.images_dir else PROJECT / "generated_images"
LTX_DIR = Path(args.ltx_dir) if args.ltx_dir else PROJECT / "ltx_videos"
ITEMS = json.loads(INPUT_JSON.read_text())
for runtime_id, item in enumerate(ITEMS, 1):
    item["_runtime_id"] = runtime_id
RENDER_ROOT = Path(args.render_root) if args.render_root else PROJECT
CLIP_DIR = RENDER_ROOT / "clips_final_hardsub_frame_locked"
BASE_CLIP_DIR = RENDER_ROOT / "clips_final_base"
FINAL_DIR = PROJECT / "final"
OVERLAY_DIR = RENDER_ROOT / "typing_overlays_final_archival"
TMP_DIR = RENDER_ROOT / "tmp_final_hardsub"
for folder in (CLIP_DIR, BASE_CLIP_DIR, FINAL_DIR, OVERLAY_DIR, TMP_DIR):
    folder.mkdir(parents=True, exist_ok=True)

W, H, FPS = args.width, args.height, args.fps
FONT_FILE = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
BOLD_FONT_FILE = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
YUJI_BOKU_FONT_FILE = str(PROJECT / "assets" / "YujiBoku-Regular.ttf")
GRAIN = PROJECT / "assets" / "grain.mp4"
TYPE_SFX = PROJECT / "assets" / "keyboard-typing-sound-effect-335503.mp3"
VOICE = Path(args.voice)
SRT = Path(args.srt)
GRAIN_OPACITY = {"light": 0.055, "medium": 0.095, "heavy": 0.14}
ARCHIVAL_OVERLAY_SCALE = 0.70


def build_frame_schedule():
    """Quantize absolute JSON boundaries once so per-shot rounding cannot drift."""
    previous_end = None
    for item in ITEMS:
        start_frame = round(float(item["start"]) * FPS)
        end_frame = round(float(item["end"]) * FPS)
        if previous_end is None and start_frame != 0:
            raise ValueError(f"The first shot must start at frame 0, got {start_frame}")
        if previous_end is not None and start_frame != previous_end:
            raise ValueError(
                f"Non-contiguous frame boundary before runtime shot {item['_runtime_id']}: "
                f"start_frame={start_frame}, previous_end_frame={previous_end}"
            )
        if end_frame <= start_frame:
            raise ValueError(f"Shot {item['_runtime_id']} has no frames after quantization")
        item["_start_frame"] = start_frame
        item["_end_frame"] = end_frame
        item["_frames"] = end_frame - start_frame
        previous_end = end_frame


build_frame_schedule()


def x264_fallback(cmd):
    out = []
    skip_next = False
    for i, token in enumerate(cmd):
        if skip_next:
            skip_next = False
            continue
        if token == "h264_nvenc":
            out.append("libx264")
        elif token == "-cq":
            skip_next = True
        elif token == "-preset" and i + 1 < len(cmd) and cmd[i + 1] == "p4":
            out.extend(["-preset", "medium", "-crf", "18"])
            skip_next = True
        else:
            out.append(token)
    return out


def run_ffmpeg(cmd, label):
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode == 0:
        return result
    if "h264_nvenc" in cmd and (
        "No capable devices found" in result.stdout
        or "OpenEncodeSessionEx failed" in result.stdout
        or "unsupported device" in result.stdout
        or "Unknown encoder 'h264_nvenc'" in result.stdout
        or "Unrecognized option 'cq'" in result.stdout
    ):
        print(f"[fallback] {label}: h264_nvenc unavailable; retrying with libx264", flush=True)
        fallback = x264_fallback(cmd)
        result = subprocess.run(fallback, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return result


def probe_video(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=nb_frames,r_frame_rate",
            "-of", "json", str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    return int(stream["nb_frames"]), stream["r_frame_rate"]


def valid_cached_clip(path, expected_frames):
    if not path.exists() or path.stat().st_size <= 100000:
        return False
    try:
        frames, rate = probe_video(path)
        audio = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
        ).stdout.strip()
        return frames == expected_frames and rate == f"{FPS}/1" and not audio
    except (subprocess.SubprocessError, KeyError, ValueError, json.JSONDecodeError):
        return False


def duration(item):
    return item["_frames"] / FPS


def frame_count(item):
    return item["_frames"]


def runtime_id(item):
    return item["_runtime_id"]


def shot_label(item):
    return f"shot_{runtime_id(item):03d}"


def clip_label(item):
    return f"clip_{runtime_id(item):03d}"


def clip_path(item):
    return CLIP_DIR / f"{clip_label(item)}.mp4"


def source_path(item):
    sid = runtime_id(item)
    if (item.get("media_type") or "").lower() == "video":
        return LTX_DIR / f"shot_{sid:03d}.mp4"
    return IMAGES_DIR / f"shot_{sid:03d}.png"


def parse_scale_range(value):
    if not value:
        return 1.0, 1.08
    nums = re.findall(r"[0-9]+(?:\.[0-9]+)?", value)
    if len(nums) >= 2:
        return float(nums[0]), float(nums[1])
    if len(nums) == 1:
        return float(nums[0]), float(nums[0])
    return 1.0, 1.08


def photo_filter(item, frames):
    edit = item.get("edit") or {}
    kb = edit.get("kenburns_type") or "static_hold"
    z0, z1 = parse_scale_range(edit.get("kenburns_scale_range"))
    den = max(frames - 1, 1)
    if kb in ("none", "static_hold"):
        z0 = z1 = max(z0, z1, 1.0)

    z = f"{z0}+({z1 - z0})*on/{den}"
    center_x = "iw/2-(iw/zoom/2)"
    center_y = "ih/2-(ih/zoom/2)"
    max_x = "iw-iw/zoom"
    max_y = "ih-ih/zoom"
    if kb == "pan_left_to_right":
        x, y = f"({max_x})*on/{den}", center_y
    elif kb == "pan_right_to_left":
        x, y = f"({max_x})*(1-on/{den})", center_y
    elif kb == "pan_top_to_bottom":
        x, y = center_x, f"({max_y})*on/{den}"
    elif kb == "pan_bottom_to_top":
        x, y = center_x, f"({max_y})*(1-on/{den})"
    elif kb == "pan_diagonal_tl_br":
        x, y = f"({max_x})*on/{den}", f"({max_y})*on/{den}"
    elif kb == "pan_diagonal_br_tl":
        x, y = f"({max_x})*(1-on/{den})", f"({max_y})*(1-on/{den})"
    else:
        x, y = center_x, center_y

    return (
        "[0:v]scale=8000:-2,crop=8000:4500,setsar=1,"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},"
        "format=yuv420p,setpts=PTS-STARTPTS[v0]"
    )


def video_filter():
    return (
        f"[0:v]fps={FPS},scale={W}:{H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,"
        "setsar=1,format=yuv420p,setpts=PTS-STARTPTS[v0]"
    )


def static_photo_filter():
    # Match the old centered 16:9 crop without paying for 8000px zoompan frames.
    return (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},setsar=1,format=yuv420p,"
        "tpad=stop_mode=clone:stop_duration=__DURATION__,"
        "trim=duration=__DURATION__,setpts=PTS-STARTPTS[v0]"
    )


def esc_path(path):
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def write_textfile(item, text, kind):
    path = TMP_DIR / f"text_{runtime_id(item):03d}_{kind}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def wrap_text(text, font, max_width):
    lines = []
    for raw_line in text.splitlines():
        current = ""
        for ch in raw_line:
            trial = current + ch
            if current and font.getlength(trial) > max_width:
                lines.append(current)
                current = ch
            else:
                current = trial
        lines.append(current)
    return "\n".join(lines)


def paper_texture(w, h, seed):
    rng = (seed * 1103515245 + 12345) & 0x7fffffff
    img = Image.new("RGBA", (w, h), (222, 199, 151, 246))
    px = img.load()
    for y in range(h):
        for x in range(w):
            rng = (rng * 1664525 + 1013904223) & 0xffffffff
            n = ((rng >> 24) & 255) - 128
            tone = int(n * 0.10)
            base = (222 + tone, 199 + tone, 151 + tone, 246)
            px[x, y] = base
    return img


def create_typing_overlay(item, clip_duration):
    text = (item.get("edit") or {}).get("text_overlay_ja")
    if not text:
        return None, 0.0

    sid = runtime_id(item)
    scale_tag = int(round(ARCHIVAL_OVERLAY_SCALE * 100))
    total_frames = frame_count(item)
    output = OVERLAY_DIR / f"typing_{sid:03d}_s{scale_tag}_f{total_frames}.mov"
    typing_duration = min(3.5, max(0.8, len(text.replace("\n", "")) * 0.08))
    if output.exists() and output.stat().st_size > 1000:
        return output, typing_duration

    # Keep the archival plate compact enough to support the imagery. Scale the
    # whole treatment together so text, padding, border, and shadow stay balanced.
    s = ARCHIVAL_OVERLAY_SCALE
    ow, oh = round(1160 * s), round(320 * s)
    font_path = YUJI_BOKU_FONT_FILE if Path(YUJI_BOKU_FONT_FILE).exists() else BOLD_FONT_FILE
    font = ImageFont.truetype(font_path, round(46 * s))
    line_spacing = round(12 * s)
    visible_chars = list(text)

    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{ow}x{oh}", "-r", str(FPS), "-i", "-",
        "-an", "-c:v", "qtrle", str(output),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for frame in range(total_frames):
            t = frame / FPS
            count = len(visible_chars)
            if t < typing_duration:
                count = max(0, min(len(visible_chars), int(math.ceil(len(visible_chars) * t / typing_duration))))
            shown = "".join(visible_chars[:count])
            shown = wrap_text(shown, font, ow - 150)

            img = Image.new("RGBA", (ow, oh), (0, 0, 0, 0))
            if shown:
                draw = ImageDraw.Draw(img)
                bbox = draw.multiline_textbbox((0, 0), shown, font=font, spacing=line_spacing)
                text_w = bbox[2] - bbox[0]
                text_h = bbox[3] - bbox[1]
                pad_x, pad_y = round(54 * s), round(34 * s)
                outer_margin = round(70 * s)
                plate_w = min(ow - outer_margin, max(round(260 * s), text_w + pad_x * 2))
                plate_h = min(oh - outer_margin, max(round(100 * s), text_h + pad_y * 2))
                px0, py0 = round(28 * s), round(28 * s)
                shadow = Image.new("RGBA", (ow, oh), (0, 0, 0, 0))
                sd = ImageDraw.Draw(shadow)
                shadow_offset = round(8 * s)
                sd.rounded_rectangle((px0 + shadow_offset, py0 + shadow_offset, px0 + plate_w + shadow_offset, py0 + plate_h + shadow_offset), radius=round(5 * s), fill=(0, 0, 0, 95))
                img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(round(9 * s))))
                plate = paper_texture(int(plate_w), int(plate_h), sid + frame)
                pd = ImageDraw.Draw(plate)
                pd.rounded_rectangle((0, 0, plate_w - 1, plate_h - 1), radius=round(4 * s), outline=(91, 63, 35, 235), width=max(1, round(3 * s)))
                inner = round(10 * s)
                pd.rounded_rectangle((inner, inner, plate_w - inner - 1, plate_h - inner - 1), radius=round(2 * s), outline=(122, 84, 46, 170), width=1)
                img.alpha_composite(plate, (px0, py0))
                draw = ImageDraw.Draw(img)
                tx = px0 + (plate_w - text_w) / 2 - bbox[0]
                ty = py0 + (plate_h - text_h) / 2 - bbox[1]
                draw.multiline_text((tx, ty), shown, font=font, fill=(42, 32, 24, 255), spacing=line_spacing)
            proc.stdin.write(img.tobytes())
    finally:
        proc.stdin.close()
    stderr = proc.stderr.read() if proc.stderr else b""
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", "ignore")[-3000:])
    return output, typing_duration


def parse_srt_time(value):
    h, m, rest = value.strip().replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def fmt_srt_time(sec):
    sec = max(0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def wrap_japanese_sub(text, max_chars=23):
    text = "".join(line.strip() for line in text.splitlines())
    lines, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) >= max_chars and ch in "、。！？｣」）)":
            lines.append(cur)
            cur = ""
        elif len(cur) >= max_chars + 6:
            lines.append(cur)
            cur = ""
    if cur:
        lines.append(cur)
    return "\n".join(lines[:2]) if len(lines) <= 2 else "\n".join([lines[0], "".join(lines[1:])])


def load_srt_entries():
    raw = SRT.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\n\s*\n", raw.strip())
    entries = []
    for block in blocks:
        lines = [x.rstrip() for x in block.splitlines() if x.strip()]
        if len(lines) < 2:
            continue
        time_line = next((x for x in lines if "-->" in x), None)
        if not time_line:
            continue
        idx = lines.index(time_line)
        a, b = [x.strip() for x in time_line.split("-->", 1)]
        text = "\n".join(lines[idx + 1:])
        entries.append((parse_srt_time(a), parse_srt_time(b), text))
    return entries


SRT_ENTRIES = load_srt_entries() if SRT.exists() else []
SUBTITLE_STYLE = (
    r"FontName=Noto Sans CJK JP\,FontSize=16\,PrimaryColour=&H00FFFFFF\,"
    r"BackColour=&H99000000\,BorderStyle=4\,Outline=0\,Shadow=0\,"
    r"Alignment=2\,MarginL=35\,MarginR=35\,MarginV=32"
)


def write_local_srt(item):
    if (item.get("edit") or {}).get("hardsub") != "normal":
        return None
    sid = runtime_id(item)
    start, end = float(item["start"]), float(item["end"])
    rows = []
    # A shot may group several source/SRT IDs, so subtitle membership is based
    # only on timeline overlap with the shot boundaries.
    for s, e, text in SRT_ENTRIES:
        ls = max(s, start) - start
        le = min(e, end) - start
        if le - ls > 0.02:
            rows.append((ls, le, wrap_japanese_sub(text)))
    if not rows:
        return None
    path = TMP_DIR / f"shot_{sid:03d}_subs.srt"
    body = []
    for i, (s, e, text) in enumerate(rows, 1):
        body.append(f"{i}\n{fmt_srt_time(s)} --> {fmt_srt_time(e)}\n{text}\n")
    path.write_text("\n".join(body), encoding="utf-8")
    return path

def render_clip(item):
    output = clip_path(item)
    base_output = BASE_CLIP_DIR / f"{clip_label(item)}_base.mp4"
    if valid_cached_clip(output, frame_count(item)):
        print(f"[skip] {output.name}", flush=True)
        return

    if args.optimized:
        return render_clip_optimized(item)

    d = duration(item)
    frames = frame_count(item)
    src = source_path(item)
    if not src.exists():
        raise FileNotFoundError(src)

    media = (item.get("media_type") or "").lower()
    edit = item.get("edit") or {}
    grain = edit.get("film_grain") or "none"
    typing_overlay, typing_duration = create_typing_overlay(item, d)
    typing = typing_overlay is not None

    cmd = ["ffmpeg", "-hide_banner", "-y"]
    if media == "video":
        cmd += ["-stream_loop", "-1", "-i", str(src)]
    else:
        cmd += ["-loop", "1", "-i", str(src)]

    input_count = 1
    grain_idx = None
    if grain != "none" and GRAIN.exists():
        grain_idx = input_count
        input_count += 1
        cmd += ["-stream_loop", "-1", "-i", str(GRAIN)]

    overlay_idx = None
    if typing_overlay:
        overlay_idx = input_count
        input_count += 1
        cmd += ["-i", str(typing_overlay)]

    filters = [video_filter() if media == "video" else photo_filter(item, frames)]
    current = "v0"

    if grain_idx is not None:
        opacity = GRAIN_OPACITY.get(grain, 0.095)
        filters.append(
            f"[{grain_idx}:v]fps={FPS},scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},trim=end_frame={frames},setpts=PTS-STARTPTS[g]"
        )
        filters.append(f"[{current}][g]blend=all_mode=screen:all_opacity={opacity}[vgrain]")
        current = "vgrain"

    if edit.get("vignette"):
        filters.append(f"[{current}]vignette=PI/5[vvig]")
        current = "vvig"

    if overlay_idx is not None:
        filters.append(f"[{overlay_idx}:v]fps={FPS},format=rgba,setpts=PTS-STARTPTS[tov]")
        filters.append(f"[{current}][tov]overlay=80:90:eof_action=pass[vtyped]")
        current = "vtyped"

    filters.append(f"[{current}]trim=end_frame={frames},setpts=PTS-STARTPTS[vout]")

    cmd += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[vout]",
        "-frames:v",
        str(frames),
        "-c:v",
        "h264_nvenc",
        "-preset",
        "p4",
        "-cq",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(FPS),
        "-an",
        "-movflags",
        "+faststart",
        str(base_output),
    ]
    print(
        f"[render] {clip_label(item)} source_id={item.get('id')} {media} {d:.3f}s grain={grain} typing={typing} hardsub={(item.get('edit') or {}).get('hardsub')}",
        flush=True,
    )
    result = run_ffmpeg(cmd, clip_label(item))
    if result.returncode != 0:
        print(result.stdout[-4000:], flush=True)
        raise RuntimeError(f"ffmpeg failed for {clip_label(item)}")

    local_srt = write_local_srt(item)
    if local_srt:
        sub_cmd = [
            "ffmpeg", "-hide_banner", "-y", "-i", str(base_output),
            "-vf", f"subtitles='{esc_path(local_srt)}':fontsdir='/usr/share/fonts/opentype/noto':force_style={SUBTITLE_STYLE}",
            "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20", "-pix_fmt", "yuv420p",
            "-r", str(FPS), "-frames:v", str(frames), "-an", "-movflags", "+faststart", str(output),
        ]
        result = run_ffmpeg(sub_cmd, f"{clip_label(item)}_subtitles")
        if result.returncode != 0:
            print(result.stdout[-4000:], flush=True)
            raise RuntimeError(f"subtitle burn failed for {clip_label(item)}")
    else:
        shutil.copy2(base_output, output)


def render_clip_optimized(item):
    output = clip_path(item)
    if valid_cached_clip(output, frame_count(item)):
        print(f"[skip] {output.name}", flush=True)
        return

    d = duration(item)
    frames = frame_count(item)
    src = source_path(item)
    if not src.exists():
        raise FileNotFoundError(src)

    media = (item.get("media_type") or "").lower()
    edit = item.get("edit") or {}
    grain = edit.get("film_grain") or "none"
    typing_overlay, typing_duration = create_typing_overlay(item, d)
    typing = typing_overlay is not None
    local_srt = write_local_srt(item)

    cmd = ["ffmpeg", "-hide_banner", "-y"]
    if media == "video":
        cmd += ["-stream_loop", "-1", "-i", str(src)]
    elif (edit.get("kenburns_type") or "static_hold") in ("none", "static_hold"):
        # A single decoded frame is padded after scaling by tpad in the filter graph.
        cmd += ["-i", str(src)]
    else:
        cmd += ["-loop", "1", "-i", str(src)]

    input_count = 1
    grain_idx = None
    if grain != "none" and GRAIN.exists():
        grain_idx = input_count
        input_count += 1
        cmd += ["-stream_loop", "-1", "-i", str(GRAIN)]

    overlay_idx = None
    if typing_overlay:
        overlay_idx = input_count
        input_count += 1
        cmd += ["-i", str(typing_overlay)]

    kb = (edit.get("kenburns_type") or "static_hold")
    if media == "photo" and kb in ("none", "static_hold"):
        filters = [static_photo_filter().replace("__DURATION__", f"{d:.3f}")]
    else:
        filters = [video_filter() if media == "video" else photo_filter(item, frames)]
    current = "v0"

    if grain_idx is not None:
        opacity = GRAIN_OPACITY.get(grain, 0.095)
        filters.append(
            f"[{grain_idx}:v]fps={FPS},scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},trim=end_frame={frames},setpts=PTS-STARTPTS[g]"
        )
        filters.append(f"[{current}][g]blend=all_mode=screen:all_opacity={opacity}[vgrain]")
        current = "vgrain"

    if edit.get("vignette"):
        filters.append(f"[{current}]vignette=PI/5[vvig]")
        current = "vvig"

    if overlay_idx is not None:
        filters.append(f"[{overlay_idx}:v]fps={FPS},format=rgba,setpts=PTS-STARTPTS[tov]")
        filters.append(f"[{current}][tov]overlay=80:90:eof_action=pass[vtyped]")
        current = "vtyped"

    # Burn subtitles in the same encode as the visual treatment.
    if local_srt:
        filters.append(
            f"[{current}]subtitles='{esc_path(local_srt)}':fontsdir='/usr/share/fonts/opentype/noto':"
            f"force_style={SUBTITLE_STYLE}[vsub]"
        )
        current = "vsub"

    filters.append(f"[{current}]trim=end_frame={frames},setpts=PTS-STARTPTS[vout]")

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-frames:v", str(frames),
        "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20",
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-an",
        str(output),
    ]
    print(
        f"[render-optimized] {clip_label(item)} source_id={item.get('id')} {media} {d:.3f}s grain={grain} typing={typing} hardsub={(item.get('edit') or {}).get('hardsub')}",
        flush=True,
    )
    result = run_ffmpeg(cmd, f"optimized_{clip_label(item)}")
    if result.returncode != 0:
        print(result.stdout[-4000:], flush=True)
        raise RuntimeError(f"optimized ffmpeg failed for {clip_label(item)}")


if args.optimized:
    workers = max(1, min(int(args.workers), 6))
    print(f"[optimized] workers={workers} render_root={RENDER_ROOT}", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(render_clip, item) for item in ITEMS]
        for future in futures:
            future.result()
else:
    for item in ITEMS:
        render_clip(item)

expected_total_frames = ITEMS[-1]["_end_frame"] - ITEMS[0]["_start_frame"]
actual_clip_frames = 0
for item in ITEMS:
    frames, rate = probe_video(clip_path(item))
    if frames != frame_count(item) or rate != f"{FPS}/1":
        raise RuntimeError(
            f"Frame QC failed for {clip_label(item)}: expected={frame_count(item)} "
            f"actual={frames} rate={rate}"
        )
    actual_clip_frames += frames
if actual_clip_frames != expected_total_frames:
    raise RuntimeError(
        f"Clip frame total mismatch: expected={expected_total_frames} actual={actual_clip_frames}"
    )
print(f"[frame-qc] clips={len(ITEMS)} total_frames={actual_clip_frames} fps={FPS}", flush=True)

concat = TMP_DIR / "concat_list_final.txt"
concat.write_text("".join(f"file '{clip_path(item)}'\n" for item in ITEMS), encoding="utf-8")

visual = RENDER_ROOT / "japan_project_visual_timeline_final_hardsub.mp4"
cmd = [
    "ffmpeg",
    "-hide_banner",
    "-y",
    "-f",
    "concat",
    "-safe",
    "0",
    "-i",
    str(concat),
    "-c",
    "copy",
    "-movflags",
    "+faststart",
    str(visual),
]
print("[concat]", visual, flush=True)
result = run_ffmpeg(cmd, "concat")
if result.returncode != 0:
    print(result.stdout[-4000:], flush=True)
    raise RuntimeError("concat failed")

visual_frames, visual_rate = probe_video(visual)
if visual_frames != expected_total_frames or visual_rate != f"{FPS}/1":
    raise RuntimeError(
        f"Concat frame QC failed: expected={expected_total_frames} actual={visual_frames} rate={visual_rate}"
    )
print(f"[frame-qc] concat_frames={visual_frames} duration={visual_frames / FPS:.3f}s", flush=True)

final = Path(args.output) if args.output else FINAL_DIR / "final_video.mp4"
final.parent.mkdir(parents=True, exist_ok=True)
final_duration = expected_total_frames / FPS
typing_events = []
for item in ITEMS:
    text = (item.get("edit") or {}).get("text_overlay_ja")
    if text:
        event_duration = min(3.5, max(0.8, len(text.replace("\n", "")) * 0.08))
        event_duration = min(event_duration, duration(item))
        typing_events.append((item["_start_frame"] / FPS, event_duration))

cmd = ["ffmpeg", "-hide_banner", "-y", "-i", str(visual), "-i", str(VOICE)]
if typing_events and TYPE_SFX.exists():
    cmd += ["-stream_loop", "-1", "-i", str(TYPE_SFX)]
    split_labels = "".join(f"[typein{i}]" for i in range(len(typing_events)))
    filters = [f"[2:a]asplit={len(typing_events)}{split_labels}", "[1:a]volume=1.0[vo]"]
    mixed_labels = []
    for i, (start, event_duration) in enumerate(typing_events):
        delay_ms = round(start * 1000)
        filters.append(
            f"[typein{i}]atrim=0:{event_duration:.3f},asetpts=PTS-STARTPTS,"
            f"volume=0.35,adelay={delay_ms}|{delay_ms}[type{i}]"
        )
        mixed_labels.append(f"[type{i}]")
    filters.append(
        f"[vo]{''.join(mixed_labels)}amix=inputs={len(typing_events) + 1}:"
        "duration=first:dropout_transition=0[a]"
    )
    cmd += ["-filter_complex", ";".join(filters), "-map", "0:v:0", "-map", "[a]"]
else:
    cmd += ["-map", "0:v:0", "-map", "1:a:0"]

cmd += [
    "-t", f"{final_duration:.3f}",
    "-c:v", "copy",
    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
    "-movflags", "+faststart",
    str(final),
]
print("[final]", final, flush=True)
result = run_ffmpeg(cmd, "final_mux")
if result.returncode != 0:
    print(result.stdout[-6000:], flush=True)
    raise RuntimeError("final mux failed")

final_frames, final_rate = probe_video(final)
if final_frames != expected_total_frames or final_rate != f"{FPS}/1":
    raise RuntimeError(
        f"Final frame QC failed: expected={expected_total_frames} actual={final_frames} rate={final_rate}"
    )
print(f"[frame-qc] final_frames={final_frames} drift_frames=0", flush=True)

print("[done]", final, flush=True)
