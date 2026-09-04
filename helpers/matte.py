#!/Users/mikeattreys/Developer/video-use/helpers/.matte-venv/bin/python3
"""Replace the background behind a person in a video via RVM (RobustVideoMatting).

Runs before `build_final_composite` in the render pipeline — the matted clip
becomes the new "base" that overlays/subtitles composite on top of, same role
`grade.py`'s color correction plays. Not imported by render.py directly (torch
lives in its own venv, .matte-venv, kept out of the main uv-managed deps) —
invoked as a subprocess using this file's own venv interpreter.

No green screen required — RVM is a learned matting model, works on any
background. Requires a real person on camera; not for arbitrary object removal.

Model: RobustVideoMatting (MobileNetV3 backbone), GPL-3.0, github.com/PeterL1n/
RobustVideoMatting. Used here as a local tool (subprocess call, no code
embedded/redistributed) — same usage pattern as GPL ffmpeg elsewhere in this
pipeline. Verified working on this hardware via MPS 2026-09-04: correct
numerically (no NaN/Inf) and visually (tested against RVM's own published
demo result), ~91 FPS steady-state at 480x376 — comfortably real-time.
Full-resolution throughput not yet benchmarked.

Usage:
    python helpers/matte.py <input.mp4> -o <output.mp4> --bg <background.jpg>
    python helpers/matte.py <input.mp4> -o <output.mp4> --bg <background.jpg> --device cpu
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np
import torch
from PIL import Image

# Same HDR handling as render.py — matte.py can run standalone on a raw phone
# clip (no upstream extract_segment tonemap pass), so it needs its own check.
#
# The tonemap chain needs `zscale`, which requires libzimg — the default
# Homebrew `ffmpeg` on this machine is NOT built with it (verified directly:
# "No such filter: 'zscale'"), only `ffmpeg-full` is. render.py's own
# FFMPEG_SUBTITLES_BIN constant routes to ffmpeg-full for libass, but its
# extract_segment() HDR tonemap path calls plain "ffmpeg" — same missing-filter
# bug, not something matte.py introduced. Worth a heads-up, separate from this.
FFMPEG_FULL_BIN = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10) and HLG
TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p"
)


def is_hdr_source(path: Path) -> bool:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() in HDR_TRANSFERS
    except subprocess.CalledProcessError:
        return False


def probe_video(path: Path) -> tuple[int, int, str]:
    """Return (width, height, r_frame_rate) via ffprobe. r_frame_rate is a
    fraction string like '30000/1001' — pass straight to ffmpeg's -r."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    # csv output can carry a trailing comma (extra empty field) depending on
    # container/stream layout — verified on a real .MOV — so parse the first
    # 3 fields rather than assuming an exact 3-way split.
    parts = out.stdout.strip().split(",")
    w, h, fps = parts[0], parts[1], parts[2]
    return int(w), int(h), fps


def downsample_ratio_for(width: int, height: int) -> float:
    """RVM's own guideline table (documentation/inference.md), full-body column
    since talking-head content typically shows torso, not just a tight face crop."""
    long_edge = max(width, height)
    if long_edge <= 512:
        return 1.0
    if long_edge <= 1280:
        return 0.6
    return 0.4


def load_model(device: str):
    print(f"Loading RVM (mobilenetv3) on {device}...")
    model = torch.hub.load("PeterL1n/RobustVideoMatting", "mobilenetv3", pretrained=True, trust_repo=True)
    model.eval().to(device)
    return model


def prepare_background(bg_path: Path, width: int, height: int) -> np.ndarray:
    """Cover-fit the background image to the video's exact dimensions
    (scale to cover, center-crop) — same fit behavior as a CSS background-size:cover."""
    img = Image.open(bg_path).convert("RGB")
    src_w, src_h = img.size
    scale = max(width / src_w, height / src_h)
    new_w, new_h = round(src_w * scale), round(src_h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - width) // 2
    top = (new_h - height) // 2
    img = img.crop((left, top, left + width, top + height))
    return np.array(img).astype(np.float32) / 255.0


def replace_background(
    input_path: Path,
    output_path: Path,
    bg_path: Path,
    device: str = "auto",
    downsample_ratio: float | None = None,
) -> None:
    if device == "auto":
        device = "mps" if torch.backends.mps.is_available() else "cpu"

    width, height, fps = probe_video(input_path)
    ds = downsample_ratio if downsample_ratio is not None else downsample_ratio_for(width, height)
    print(f"Input: {width}x{height} @ {fps}fps, downsample_ratio={ds}, device={device}")

    bg = prepare_background(bg_path, width, height)
    model = load_model(device)

    video_only = output_path.with_suffix(".video_only.mp4")

    hdr = is_hdr_source(input_path)
    decode_bin = FFMPEG_FULL_BIN if hdr else "ffmpeg"
    decode_cmd = [decode_bin, "-v", "error", "-i", str(input_path)]
    if hdr:
        print("HDR source detected (PQ/HLG) — applying tonemap before matting (via ffmpeg-full)")
        decode_cmd += ["-vf", TONEMAP_CHAIN]
    decode_cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-an", "-"]
    decoder = subprocess.Popen(decode_cmd, stdout=subprocess.PIPE)

    encode_cmd = ["ffmpeg", "-y", "-v", "error",
                  "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", fps,
                  "-i", "-",
                  "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
                  str(video_only)]
    encoder = subprocess.Popen(encode_cmd, stdin=subprocess.PIPE)

    frame_bytes = width * height * 3
    rec = [None] * 4
    n = 0

    while True:
        buf = decoder.stdout.read(frame_bytes)
        if len(buf) < frame_bytes:
            break
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 3).astype(np.float32) / 255.0
        src = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).to(device)

        with torch.no_grad():
            fgr, pha, *rec = model(src, *rec, ds)

        fgr_np = fgr[0].permute(1, 2, 0).to("cpu").numpy()
        pha_np = pha[0, 0].to("cpu").numpy()[..., None]
        comp = fgr_np * pha_np + bg * (1 - pha_np)
        encoder.stdin.write((np.clip(comp, 0, 1) * 255).astype(np.uint8).tobytes())

        n += 1
        if n % 60 == 0:
            print(f"  {n} frames matted...")

    decoder.stdout.close()
    decoder.wait()
    encoder.stdin.close()
    encoder.wait()

    if decoder.returncode != 0:
        sys.exit(f"ffmpeg decode failed (exit {decoder.returncode})")
    if encoder.returncode != 0:
        sys.exit(f"ffmpeg encode failed (exit {encoder.returncode})")

    print(f"{n} frames processed. Remuxing audio from source...")
    remux_cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", str(video_only), "-i", str(input_path),
        # a:0 specifically, not a: (bare) — some iPhone .MOV files carry a second,
        # non-decodable "unknown"-codec audio track (verified on a real clip);
        # mapping all audio streams pulls that in and ffmpeg fails outright.
        "-map", "0:v", "-map", "1:a:0?",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(output_path),
    ]
    subprocess.run(remux_cmd, check=True)
    video_only.unlink(missing_ok=True)
    print(f"Done -> {output_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Replace the background behind a person in a video (RVM)")
    ap.add_argument("input", type=Path, help="Input video")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output video path")
    ap.add_argument("--bg", type=Path, required=True, help="Background image (cover-fit to video dimensions)")
    ap.add_argument("--device", default="auto", choices=["auto", "mps", "cpu"], help="Inference device (default: auto)")
    ap.add_argument("--downsample-ratio", type=float, default=None, help="Override RVM's auto-selected downsample ratio")
    args = ap.parse_args()

    if not args.input.exists():
        sys.exit(f"input not found: {args.input}")
    if not args.bg.exists():
        sys.exit(f"background image not found: {args.bg}")

    replace_background(args.input, args.output, args.bg, device=args.device, downsample_ratio=args.downsample_ratio)


if __name__ == "__main__":
    main()
