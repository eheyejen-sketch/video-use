#!/Users/mikeattreys/Developer/video-use/.venv/bin/python3
"""Build a single icon+label badge overlay (ProRes 4444, real alpha) from a
Tabler icon — the same visual style validated 2026-09-04 (dark pill, colored
icon badge circle, white icon glyph, label text, ease-out-cubic reveal/hold/
fade), generalized to take any Tabler icon by name instead of a hand-drawn
shape.

NOT exec-approved for Jensen/Beast — this is an authoring tool (Claude Code
or Mike runs it to produce a pre-made overlay file), not something the scoped
agents invoke themselves. Once built, the output .mov IS usable by Jensen/
Beast via the EDL "overlays" field (see video-use SKILL.md, "Overlays —
pre-made files only").

Icon source: ~/Developer/tabler-icons/icons/{outline,filled}/<name>.svg
(MIT licensed, github.com/tabler/tabler-icons). Rasterized via rsvg-convert
(librsvg) — do NOT use ImageMagick's built-in SVG delegate, it produces a
1-bit bitmap with no anti-aliasing or alpha (verified 2026-09-04).

Usage:
    build_icon_overlay.py --icon shield-check --label "SECURITY" \
        --color 64,156,255 -o out.mov
    build_icon_overlay.py --icon lock --label "SAFETY" --style filled \
        --color 64,156,255 --duration 3.0 --y 700 -o out.mov

List available icons:
    build_icon_overlay.py --list-icons "shield*"
"""
from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ICON_ROOT = Path("/Users/mikeattreys/Developer/tabler-icons/icons")
FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
W, H = 1080, 1920
FPS = 24


def ease_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def find_icon(name: str, style: str) -> Path:
    p = ICON_ROOT / style / f"{name}.svg"
    if not p.exists():
        sys.exit(f"icon not found: {p} (try --list-icons '{name}*' to search)")
    return p


def list_icons(pattern: str) -> None:
    for style in ("outline", "filled"):
        matches = sorted(f.stem for f in (ICON_ROOT / style).glob("*.svg") if fnmatch.fnmatch(f.stem, pattern))
        if matches:
            print(f"-- {style} ({len(matches)} match{'es' if len(matches) != 1 else ''}) --")
            for m in matches[:60]:
                print(f"  {m}")
            if len(matches) > 60:
                print(f"  ... and {len(matches) - 60} more")


def rasterize_icon(svg_path: Path, out_png: Path, size: int, color_hex: str) -> None:
    """Rasterize an SVG to a colored PNG via rsvg-convert. Tabler icons use
    stroke="currentColor" / fill="currentColor" — substitute the target color
    directly in the SVG text rather than relying on any tool's CSS resolution."""
    svg_text = svg_path.read_text()
    svg_text = svg_text.replace("currentColor", color_hex)
    tmp_svg = out_png.with_suffix(".tmp.svg")
    tmp_svg.write_text(svg_text)
    subprocess.run(
        ["rsvg-convert", "-w", str(size), "-h", str(size), "-o", str(out_png), str(tmp_svg)],
        check=True,
    )
    tmp_svg.unlink()


def build(
    icon_name: str,
    label: str,
    color: tuple[int, int, int],
    style: str,
    duration: float,
    y: int,
    out_path: Path,
) -> None:
    icon_svg = find_icon(icon_name, style)

    work_dir = out_path.parent / f".build_{out_path.stem}"
    work_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = work_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    icon_png = work_dir / "icon.png"
    rasterize_icon(icon_svg, icon_png, size=200, color_hex="#FFFFFF")
    icon_img = Image.open(icon_png).convert("RGBA")

    font = ImageFont.truetype(FONT_PATH, 56)

    pill_w, pill_h = 480, 110
    pill_x = W - pill_w - 40
    badge_r = 38

    n_frames = int(duration * FPS) + 1
    reveal_dur = 0.3
    fade_start = duration - 0.3

    for i in range(n_frames):
        t = i / FPS
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

        alpha = 1.0
        if t < reveal_dur:
            alpha = ease_out_cubic(t / reveal_dur)
        elif t > fade_start:
            alpha = 1.0 - ease_out_cubic((t - fade_start) / (duration - fade_start))
        a = int(max(0, min(1, alpha)) * 255)

        if a > 0:
            y_off = int((1 - ease_out_cubic(min(1.0, t / reveal_dur))) * 20) if t < reveal_dur else 0
            py = y + y_off

            layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            draw = ImageDraw.Draw(layer)
            draw.rounded_rectangle([pill_x, py, pill_x + pill_w, py + pill_h], radius=22,
                                    fill=(15, 15, 20, int(150 * alpha)))
            badge_cx = pill_x + 65
            badge_cy = py + pill_h // 2
            draw.ellipse([badge_cx - badge_r, badge_cy - badge_r, badge_cx + badge_r, badge_cy + badge_r],
                         fill=(*color, a))
            img = Image.alpha_composite(img, layer)

            icon_scaled = icon_img.resize((52, 52), Image.LANCZOS)
            icon_alpha = icon_scaled.split()[3].point(lambda p: int(p * alpha))
            icon_scaled.putalpha(icon_alpha)
            img.alpha_composite(icon_scaled, (badge_cx - 26, badge_cy - 26))

            draw2 = ImageDraw.Draw(img)
            tx, ty = pill_x + 125, py + 28
            draw2.text((tx + 3, ty + 3), label, font=font, fill=(0, 0, 0, int(140 * alpha)))
            draw2.text((tx, ty), label, font=font, fill=(255, 255, 255, a))

        img.save(frames_dir / f"frame_{i:04d}.png")

    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-framerate", str(FPS),
         "-i", str(frames_dir / "frame_%04d.png"),
         "-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuva444p10le",
         str(out_path)],
        check=True,
    )

    import shutil
    shutil.rmtree(work_dir)
    print(f"Built {n_frames} frames -> {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build an icon+label badge overlay from a Tabler icon")
    ap.add_argument("--icon", help="Tabler icon name, e.g. 'shield-check' (no .svg)")
    ap.add_argument("--label", help="Label text, e.g. 'SECURITY'")
    ap.add_argument("--color", default="64,156,255", help="R,G,B for the badge circle (default: 64,156,255)")
    ap.add_argument("--style", choices=["outline", "filled"], default="outline")
    ap.add_argument("--duration", type=float, default=3.0)
    ap.add_argument("--y", type=int, default=700, help="Vertical position in pixels (1080x1920 canvas)")
    ap.add_argument("-o", "--output", type=Path, help="Output .mov path")
    ap.add_argument("--list-icons", metavar="PATTERN", help="List available icons matching a glob pattern and exit")
    args = ap.parse_args()

    if args.list_icons:
        list_icons(args.list_icons)
        return

    if not args.icon or not args.label or not args.output:
        sys.exit("--icon, --label, and -o/--output are required (or use --list-icons)")

    color = tuple(int(c) for c in args.color.split(","))
    build(args.icon, args.label, color, args.style, args.duration, args.y, args.output)


if __name__ == "__main__":
    main()
