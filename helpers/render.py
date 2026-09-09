#!/Users/mikeattreys/Developer/video-use/.venv/bin/python3
"""Render a video from an EDL.

Implements the HEURISTICS render pipeline in the correct order:

  1. Per-segment extract with color grade + 30ms audio fades baked in
  2. Lossless -c copy concat into base.mp4
  2.5. If EDL has "background": replace the background behind the person
     (RVM matting, no green screen needed) — becomes the new base
  3. If overlays or subtitles: single filter graph that overlays animations
     (with PTS shift so frame 0 lands at the overlay window start)
     and applies `subtitles` filter LAST → final.mp4

Optionally builds a master SRT from the per-source transcripts + EDL
output-timeline offsets, applies the proven force_style (2-word
UPPERCASE chunks, Helvetica 18 Bold, MarginV=35).

Usage:
    python helpers/render.py <edl.json> -o final.mp4
    python helpers/render.py <edl.json> -o preview.mp4 --preview
    python helpers/render.py <edl.json> -o final.mp4 --build-subtitles
    python helpers/render.py <edl.json> -o final.mp4 --no-subtitles
    python helpers/render.py <edl.json> -o final.mp4 --no-matte

EDL background field (optional):
    {"background": "path/to/bg.png", ...}
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _job_lock  # noqa: E402

try:
    from grade import get_preset, auto_grade_for_clip  # same directory
except Exception:
    def get_preset(name: str) -> str:
        return ""

    def auto_grade_for_clip(video, start=0.0, duration=None, verbose=False):  # type: ignore
        return "eq=contrast=1.03:saturation=0.98", {}

try:
    from check_fillers import FILLER_WORDS, _normalize as _norm_word  # same directory
except Exception:  # keep validation working even if check_fillers moves
    FILLER_WORDS = {"um", "umm", "uhm", "uh", "uhh", "er", "erm", "ah", "hmm", "mm"}
    _re_word = re.compile(r"[a-z']+")

    def _norm_word(text: str) -> str:
        m = _re_word.search(text.lower())
        return m.group(0) if m else ""


# The default Homebrew ffmpeg formula is built without libass or libzimg, so
# neither the `subtitles` filter nor `zscale` (used by TONEMAP_CHAIN for HDR
# sources) are available on it. ffmpeg-full (keg-only, installed side-by-side,
# does not affect the system ffmpeg other tools depend on) has both. Used by
# the subtitle-burning composite below AND by extract_segment() whenever the
# source is HDR — verified directly: default ffmpeg fails with "No such
# filter: 'zscale'" on a real HDR .MOV.
FFMPEG_SUBTITLES_BIN = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"

# -------- Subtitle style (bold-overlay, proven at 1920×1080 and 1080×1920) --
#
# MarginV is NOT taste — it is a platform safe-zone rule.
# TikTok / IG Reels / Shorts UI (caption, username, music, right-rail actions)
# covers roughly the bottom ~25–30% of a 1080×1920 frame. Captions placed near
# the bottom edge get clipped or obscured by the UI. libass auto-scales the
# render canvas relative to PlayResY=288, so MarginV=90 lands the caption
# baseline roughly 30% up from the bottom on any aspect — clear of the UI on
# every major vertical-video platform. Default floor is ~75 for that reason.
#
# Lowered to 50 on 2026-09-04 per Mike's explicit request (close talking-head
# framing put the caption near his chin/collarbone) — a real specific reason,
# not casual drift. This trades some platform-UI safe-zone margin for staying
# clear of the face; re-check against the actual target platform's UI before
# a real publish, don't assume 50 is still clear once export destination is
# decided.
SUB_FORCE_STYLE = (
    "FontName=Helvetica,FontSize=18,Bold=1,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
    "BorderStyle=1,Outline=2,Shadow=0,"
    "Alignment=2,MarginV=50"
)

# -------- Helpers ------------------------------------------------------------


def run(cmd: list[str], quiet: bool = False) -> None:
    if not quiet:
        print(f"  $ {' '.join(str(c) for c in cmd[:6])}{' …' if len(cmd) > 6 else ''}")
    subprocess.run(cmd, check=True)


def resolve_grade_filter(grade_field: str | None) -> str:
    """The EDL's 'grade' field can be a preset name, a raw ffmpeg filter, or 'auto'.

    Returns the filter string to embed into the per-segment -vf chain.
    For 'auto', returns the sentinel "__AUTO__" which is resolved per-segment.
    """
    if not grade_field:
        return ""
    if grade_field == "auto":
        return "__AUTO__"
    # Preset names are short identifiers, filter strings contain '=' or ','.
    if re.fullmatch(r"[a-zA-Z0-9_\-]+", grade_field):
        try:
            return get_preset(grade_field)
        except KeyError:
            print(f"warning: unknown preset '{grade_field}', using as raw filter")
            return grade_field
    return grade_field


def resolve_path(maybe_path: str, base: Path) -> Path:
    """Resolve a path that may be absolute or relative to `base`."""
    p = Path(maybe_path)
    if p.is_absolute():
        return p
    return (base / p).resolve()


def resolve_asset_path(ref: str, edit_dir: Path, edl: dict | None = None) -> Path | None:
    """Resolve an EDL asset reference (e.g. `background`) that an agent may have
    written as a bare filename. Tries, in order: as-given / absolute; under the
    edit dir; under the edit dir's parent (where the source video lives — agents
    routinely drop the bg image beside the video and reference it by name); the
    directory of the first source in the EDL; then a shallow search for the
    basename under the edit-dir parent. Returns the first hit, or None.

    The old behaviour (resolve only against edit_dir) silently no-op'd the
    background swap when the file sat beside the source video — 2026-09-08."""
    if not ref:
        return None
    cand: list[Path] = []
    p = Path(ref)
    cand.append(p if p.is_absolute() else (edit_dir / p))
    cand.append(edit_dir.parent / p.name)
    if edl:
        for sp in (edl.get("sources") or {}).values():
            cand.append(Path(sp).parent / p.name)
    for c in cand:
        try:
            if c.exists():
                return c.resolve()
        except OSError:
            continue
    try:
        for hit in sorted(edit_dir.parent.glob(f"**/{p.name}")):
            if hit.is_file():
                return hit.resolve()
    except OSError:
        pass
    return None


# -------- HDR → SDR tone mapping (HLG / PQ sources) --------------------------
#
# iPhone defaults to HLG HDR in Rec.2020 (and many mirrorless cameras ship PQ).
# If the source is HDR and we only downconvert bit depth (yuv420p10le → yuv420p)
# without tone-mapping, the output is 8-bit but still carries HLG/PQ transfer
# metadata. Players that honor the metadata (screen recorders, most social
# upload re-encodes) interpret 8-bit values in an HDR container and the result
# looks oversaturated / blown out. QuickTime on macOS can hide this locally —
# screen recording and uploaded renders cannot.
#
# Fix: detect HDR via color_transfer and prepend a zscale+tonemap chain to the
# vf graph so the output is clean Rec.709 SDR.

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10) and HLG

TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p"
)


def is_hdr_source(video: Path) -> bool:
    """Return True if the source uses a PQ or HLG transfer function."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() in HDR_TRANSFERS
    except subprocess.CalledProcessError:
        return False


def is_portrait_source(video: Path) -> bool:
    """Return True if the video's height > width (portrait / vertical)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, check=True,
        )
        # csv output can carry a trailing comma (extra empty field) depending
        # on container/stream layout — verified on a real .MOV — which broke
        # this 2-way unpack and was silently swallowed by this except clause,
        # defaulting to False (landscape) on an actual portrait source.
        parts = out.stdout.strip().split(",")
        w, h = int(parts[0]), int(parts[1])
        return h > w
    except Exception:
        return False


# -------- Per-segment extraction (Rule 2 + Rule 3) --------------------------


def extract_segment(
    source: Path,
    seg_start: float,
    duration: float,
    grade_filter: str,
    out_path: Path,
    preview: bool = False,
    draft: bool = False,
) -> None:
    """Extract a cut range as its own MP4 with grade + 30ms audio fades baked in.

    `-ss` before `-i` for fast accurate seeking. Scale to 1080p from 4K.
    Portrait sources (height > width) are scaled by height to preserve orientation.

    Quality ladder:
      - final (default): 1080p libx264 fast CRF 20
      - preview:         1080p libx264 medium CRF 22 (evaluable for QC)
      - draft:           720p libx264 ultrafast CRF 28 (cut-point check only)
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    portrait = is_portrait_source(source)
    if draft:
        scale = "scale=-2:1280" if portrait else "scale=1280:-2"
    else:
        scale = "scale=-2:1920" if portrait else "scale=1920:-2"

    vf_parts: list[str] = []
    hdr = is_hdr_source(source)
    if hdr:
        vf_parts.append(TONEMAP_CHAIN)
    vf_parts.append(scale)
    if grade_filter:
        vf_parts.append(grade_filter)
    vf = ",".join(vf_parts)

    # 30ms audio fades at both edges (Rule 3) — prevent pops
    fade_out_start = max(0.0, duration - 0.03)
    af = f"afade=t=in:st=0:d=0.03,afade=t=out:st={fade_out_start:.3f}:d=0.03"

    if draft:
        preset, crf = "ultrafast", "28"
    elif preview:
        preset, crf = "medium", "22"
    else:
        preset, crf = "fast", "20"

    cmd = [
        FFMPEG_SUBTITLES_BIN if hdr else "ffmpeg", "-y",
        "-ss", f"{seg_start:.3f}",
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-af", af,
        "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-pix_fmt", "yuv420p", "-r", "24",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# -------- Cut validation against the real transcript ------------------------
#
# Added 2026-09-07 after a real incident: a proposed EDL's ranges were derived
# by eyeballing a coarse summary of the transcript instead of computing gaps
# programmatically, and two of the "silence gaps" it identified actually
# contained real spoken words -- rendering as proposed would have silently
# deleted sentences, not just dead air. This check makes that class of mistake
# structurally impossible to render: it cross-checks every kept range and every
# gap between kept ranges against the source's own word-level transcript, and
# refuses to proceed if a cut would clip mid-word or skip over real speech.
#
# Padding tolerance matches Hard Rule 7 (30-200ms working window) -- a small
# amount of a word's duration inside a "cut" gap is normal padding drift, not
# a mistake. WORD_CLIP_TOLERANCE_S is deliberately generous (0.25s) so this
# doesn't false-positive on intentional tight cuts.
WORD_CLIP_TOLERANCE_S = 0.25


def _resolve_transcript_path(edit_dir: Path, source_name: str,
                             edl: dict | None = None) -> Path | None:
    """`transcripts/<name>.json` — resolved by the SOURCE FILE's stem, not
    whatever key the EDL used for it (agents copy "C0103" from the format
    example while the file/transcript is IMG_4328). Tries the key, then
    Path(edl["sources"][key]).stem. Every transcript lookup in this module
    must go through here."""
    candidates = [source_name]
    if edl:
        p = (edl.get("sources") or {}).get(source_name)
        if p:
            candidates.append(Path(p).stem)
    for name in candidates:
        path = edit_dir / "transcripts" / f"{name}.json"
        if path.exists():
            return path
    return None


def _load_transcript_words(edit_dir: Path, source_name: str,
                            edl: dict | None = None) -> list[dict] | None:
    path = _resolve_transcript_path(edit_dir, source_name, edl)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    return [w for w in data.get("words", []) if w.get("type", "word") == "word"]


def _classify_cut_warning(msg: str) -> dict:
    """Turn one validate_cuts_against_transcripts() string into a structured
    record for `--validate-only --json`. Kinds: 'clip' (a range boundary lands
    mid-word -- never acceptable), 'speech_gap' (a between-ranges / head / tail
    span that removes real speech -- acceptable only if the EDL declares it in
    `omissions`), 'no_transcript' (a source wasn't transcribed), 'other'."""
    src_m = re.match(r"^\[([^\]]+)\]\s*", msg)
    source = src_m.group(1) if src_m else None
    if "falls mid-word" in msg:
        t_m = re.search(r"at ([\d.]+)s falls mid-word", msg)
        t = float(t_m.group(1)) if t_m else None
        return {"kind": "clip", "source": source, "start": t, "end": t, "message": msg}
    if "is NOT silence" in msg:
        g_m = re.search(r"([\d.]+)-([\d.]+)s is NOT silence", msg)
        a = float(g_m.group(1)) if g_m else None
        b = float(g_m.group(2)) if g_m else None
        return {"kind": "speech_gap", "source": source, "start": a, "end": b, "message": msg}
    if "no cached transcript found" in msg:
        n_m = re.search(r"source '([^']+)'", msg)
        return {"kind": "no_transcript", "source": n_m.group(1) if n_m else source,
                "start": None, "end": None, "message": msg}
    return {"kind": "other", "source": source, "start": None, "end": None, "message": msg}


def _declared_omission(edl: dict, source: str, start: float, end: float) -> bool:
    """True if an `omissions` entry for this source, with a non-empty reason,
    OVERLAPS the removed span [start,end].

    The check is "acknowledged", not "exactly bounded". A between-ranges gap
    IS the cut -- its exact endpoints come from where the editor put the two
    range boundaries. Requiring the omission's start/end to match those to
    the decimal is bureaucratic friction that a token-limited model keeps
    failing (it declares the *content* span -- "the 'Uh, so,' bridge" -- not
    the arithmetic gap between its ranges). An omission that names any part
    of the gap proves the editor saw it; that's what defeats the failure
    this guards against (2026-09-07: a cut made believing a span was silent).
    Mid-word clips are never covered by this -- those still always fail."""
    for o in edl.get("omissions", []) or []:
        try:
            if (o.get("source") == source
                    and str(o.get("reason", "")).strip()
                    and float(o["start"]) < end - WORD_CLIP_TOLERANCE_S
                    and float(o["end"]) > start + WORD_CLIP_TOLERANCE_S):
                return True
        except (KeyError, TypeError, ValueError):
            continue
    return False


def validate_cuts_against_transcripts(edl: dict, edit_dir: Path) -> list[str]:
    """Returns a list of human-readable warnings. Empty list = clean.

    A removed-speech span listed in the EDL's `omissions` (with a reason) is
    treated as intentional and not warned about. Mid-word clips are always
    warned regardless of `omissions`."""
    warnings: list[str] = []
    ranges = edl.get("ranges", [])
    by_source: dict[str, list[dict]] = {}
    for r in ranges:
        by_source.setdefault(r["source"], []).append(r)

    for source_name, source_ranges in by_source.items():
        words = _load_transcript_words(edit_dir, source_name, edl)
        if words is None:
            warnings.append(
                f"no cached transcript found for source '{source_name}' -- cuts for "
                f"this source were NOT validated against real speech. If this source "
                f"has spoken audio, transcribe it first so cuts can be checked."
            )
            continue

        sorted_ranges = sorted(source_ranges, key=lambda r: r["start"])

        # Check 1: does any kept range's start/end land mid-word?
        for r in sorted_ranges:
            for edge_name, edge_t in (("start", r["start"]), ("end", r["end"])):
                for w in words:
                    ws, we = w["start"], w["end"]
                    if ws + WORD_CLIP_TOLERANCE_S < edge_t < we - WORD_CLIP_TOLERANCE_S:
                        warnings.append(
                            f"[{source_name}] range {edge_name} at {edge_t:.2f}s falls "
                            f"mid-word inside \"{w['text']}\" ({ws:.2f}-{we:.2f}s) -- "
                            f"this cut would clip a word, not land in silence."
                        )

        def _check_gap(gap_start: float, gap_end: float, label: str) -> None:
            if gap_end <= gap_start:
                return
            skipped = [
                w for w in words
                if w["start"] >= gap_start - WORD_CLIP_TOLERANCE_S
                and w["end"] <= gap_end + WORD_CLIP_TOLERANCE_S
                and (w["end"] - w["start"]) > 0  # ignore zero-duration ASR artifacts
                and min(w["end"], gap_end) - max(w["start"], gap_start) > WORD_CLIP_TOLERANCE_S
            ]
            if not skipped:
                return
            # Removing a gap that contains ONLY filler words (um/uh/...) is the
            # normal business of a filler-removal edit -- don't force an
            # `omissions` entry for each of 15+ of them. Any real word in the
            # gap still warns (that's genuine speech being deleted).
            if all(_norm_word(w.get("text", "")) in FILLER_WORDS for w in skipped):
                return
            if not _declared_omission(edl, source_name, gap_start, gap_end):
                quote = " ".join(w["text"] for w in skipped)
                warnings.append(
                    f"[{source_name}] {label} {gap_start:.2f}-{gap_end:.2f}s "
                    f"is NOT silence -- it contains real speech that would be "
                    f"deleted: \"{quote}\""
                )

        # Check 2: does the gap between consecutive kept ranges skip real words?
        for a, b in zip(sorted_ranges, sorted_ranges[1:]):
            _check_gap(a["end"], b["start"], "the gap between kept ranges")

        # Check 3: does content get silently dropped before the first kept
        # range, or after the last one? (the mistake that slipped past Check 2
        # on 2026-09-07 -- a trailing "outro" cut that actually contained a
        # full extra sentence, not just wind-down, with no second kept range
        # after it for the between-ranges check to compare against)
        if words:
            transcript_start = min(w["start"] for w in words)
            transcript_end = max(w["end"] for w in words)
            _check_gap(transcript_start, sorted_ranges[0]["start"], "before the first kept range,")
            _check_gap(sorted_ranges[-1]["end"], transcript_end, "after the last kept range,")

    return warnings


def extract_all_segments(
    edl: dict,
    edit_dir: Path,
    preview: bool,
    draft: bool = False,
) -> list[Path]:
    """Extract every EDL range into edit_dir/clips_graded/seg_NN.mp4.
    Returns the ordered list of segment paths.

    If the EDL `grade` is "auto", analyze each segment range with
    `auto_grade_for_clip` and apply a per-segment subtle correction.
    Otherwise, apply the same preset/raw filter to every segment.
    """
    resolved = resolve_grade_filter(edl.get("grade"))
    is_auto = resolved == "__AUTO__"
    clips_dir = edit_dir / (
        "clips_draft" if draft else ("clips_preview" if preview else "clips_graded")
    )
    clips_dir.mkdir(parents=True, exist_ok=True)

    ranges = edl["ranges"]
    sources = edl["sources"]

    seg_paths: list[Path] = []
    print(f"extracting {len(ranges)} segment(s) → {clips_dir.name}/")
    if is_auto:
        print("  (auto-grade per segment: analyzing each range)")
    for i, r in enumerate(ranges):
        src_name = r["source"]
        src_path = resolve_path(sources[src_name], edit_dir)
        start = float(r["start"])
        end = float(r["end"])
        duration = end - start
        out_path = clips_dir / f"seg_{i:02d}_{src_name}.mp4"

        if is_auto:
            seg_filter, _stats = auto_grade_for_clip(src_path, start=start, duration=duration, verbose=False)
        else:
            seg_filter = resolved

        note = r.get("beat") or r.get("note") or ""
        print(f"  [{i:02d}] {src_name}  {start:7.2f}-{end:7.2f}  ({duration:5.2f}s)  {note}")
        if is_auto:
            print(f"        grade: {seg_filter or '(none)'}")
        extract_segment(src_path, start, duration, seg_filter, out_path, preview=preview, draft=draft)
        seg_paths.append(out_path)

    return seg_paths


# -------- Lossless concat ----------------------------------------------------


def apply_background_matte(base_path: Path, bg_ref: str, edit_dir: Path) -> Path:
    """Replace the background behind the person in `base_path` via RVM matting.
    Runs matte.py as a subprocess in its own venv (torch lives there, not in
    video-use's main deps — see helpers/matte.py's module docstring). Returns
    the new base path to use downstream; on failure, prints a warning and
    returns the original `base_path` unchanged rather than aborting the render.
    """
    bg_path = resolve_asset_path(bg_ref, edit_dir)
    if bg_path is None:
        print(f"warning: background {bg_ref!r} not found (looked beside the edit dir, "
              f"the source video, and the sources) — skipping matte, background NOT "
              f"swapped. This should have been caught at the EDL gate.")
        return base_path

    matte_script = Path(__file__).parent / "matte.py"
    matted_path = base_path.with_name(base_path.stem + "_matted.mp4")
    print(f"background matte ({bg_path.name}) → {matted_path.name}")
    try:
        subprocess.run(
            [str(matte_script), str(base_path), "-o", str(matted_path), "--bg", str(bg_path)],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"warning: background matte failed ({e}), continuing with unmatted base")
        return base_path
    return matted_path


def concat_segments(segment_paths: list[Path], out_path: Path, edit_dir: Path) -> None:
    """Lossless concat via the concat demuxer. No re-encode."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list = edit_dir / "_concat.txt"
    concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in segment_paths))

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"concat → {out_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    concat_list.unlink(missing_ok=True)


# -------- Master SRT (Rule 5) ------------------------------------------------


PUNCT_BREAK = set(".,!?;:")


def _srt_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _words_in_range(transcript: dict, t_start: float, t_end: float) -> list[dict]:
    out: list[dict] = []
    for w in transcript.get("words", []):
        if w.get("type") != "word":
            continue
        ws = w.get("start")
        we = w.get("end")
        if ws is None or we is None:
            continue
        if we <= t_start or ws >= t_end:
            continue
        out.append(w)
    return out


def build_master_srt(edl: dict, edit_dir: Path, out_path: Path) -> None:
    """Build an output-timeline SRT from per-source transcripts.

    - 2-word chunks (break on any punctuation in between)
    - UPPERCASE text
    - Output times computed as word.start - segment_start + segment_offset
    """
    entries: list[tuple[float, float, str]] = []
    seg_offset = 0.0

    for r in edl["ranges"]:
        src_name = r["source"]
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        seg_duration = seg_end - seg_start

        tr_path = _resolve_transcript_path(edit_dir, src_name, edl)
        if tr_path is None:
            print(f"  no transcript for {src_name}, skipping captions for this segment")
            seg_offset += seg_duration
            continue

        transcript = json.loads(tr_path.read_text())
        words_in_seg = _words_in_range(transcript, seg_start, seg_end)

        # strip_fillers removes filler words from the AUDIO; the captions must
        # match. Drop them here too, or the burned-in SRT still flashes "UH" /
        # "UM" cues over the cleaned audio (2026-09-08: a strip_fillers render
        # ended on a burst of standalone filler captions).
        if edl.get("strip_fillers"):
            words_in_seg = [w for w in words_in_seg
                            if _norm_word(w.get("text") or "") not in FILLER_WORDS]

        # Group into 2-word chunks, break on punctuation
        chunks: list[list[dict]] = []
        current: list[dict] = []
        for w in words_in_seg:
            text = (w.get("text") or "").strip()
            if not text:
                continue
            current.append(w)
            # Break if the current text ends in punctuation or we hit 2 words
            ends_in_punct = bool(text) and text[-1] in PUNCT_BREAK
            if len(current) >= 2 or ends_in_punct:
                chunks.append(current)
                current = []
        if current:
            chunks.append(current)

        for chunk in chunks:
            local_start = max(seg_start, chunk[0].get("start", seg_start))
            local_end = min(seg_end, chunk[-1].get("end", seg_end))
            out_start = max(0.0, local_start - seg_start) + seg_offset
            out_end = max(0.0, local_end - seg_start) + seg_offset
            if out_end <= out_start:
                out_end = out_start + 0.4
            text = " ".join((w.get("text") or "").strip() for w in chunk)
            text = re.sub(r"\s+", " ", text).strip()
            # Strip trailing punctuation for cleaner uppercase look
            text = text.rstrip(",;:")
            text = text.upper()
            # A caption that is nothing but filler words ("UH", "UM UH") is
            # never wanted — happens with zero-duration ASR filler tokens that
            # strip_fillers can't cut. Drop the cue.
            toks = [t for t in re.split(r"\s+", text) if t]
            if toks and all(_norm_word(t) in FILLER_WORDS for t in toks):
                continue
            entries.append((out_start, out_end, text))

        seg_offset += seg_duration

    # Sort and write as SRT
    entries.sort(key=lambda e: e[0])
    lines: list[str] = []
    for i, (a, b, t) in enumerate(entries, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(a)} --> {_srt_timestamp(b)}")
        lines.append(t)
        lines.append("")
    out_path.write_text("\n".join(lines))
    print(f"master SRT → {out_path.name} ({len(entries)} cues)")


# -------- Loudness normalization (social-ready audio) -----------------------


# Social-media standard: -14 LUFS integrated, -1 dBTP peak, LRA 11 LU.
# Matches YouTube / Instagram / TikTok / X / LinkedIn normalization targets.
LOUDNORM_I = -14.0
LOUDNORM_TP = -1.0
LOUDNORM_LRA = 11.0


def measure_loudness(video_path: Path) -> dict[str, str] | None:
    """Run ffmpeg loudnorm first pass and parse the JSON measurement.

    Returns a dict with measured_i, measured_tp, measured_lra, measured_thresh,
    target_offset, or None if measurement failed.
    """
    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:print_format=json"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-af", filter_str,
        "-vn", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # loudnorm prints the JSON to stderr at the end of the run
    stderr = proc.stderr

    # Find the JSON block — loudnorm output contains a `{ ... }` block
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError:
        return None
    needed = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    if not needed.issubset(data.keys()):
        return None
    return data


def apply_loudnorm_two_pass(
    input_path: Path,
    output_path: Path,
    preview: bool = False,
) -> bool:
    """Run two-pass loudnorm on input_path, write normalized copy to output_path.

    Returns True on success, False if measurement failed (caller should fall
    back to copying the input unchanged).

    In preview mode, skips the measurement pass and uses a one-pass approximation
    for speed. Final mode always does the proper two-pass.
    """
    if preview:
        # One-pass approximation — faster, slightly less accurate.
        filter_str = f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats",
            "-i", str(input_path),
            "-c:v", "copy",
            "-af", filter_str,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart",
            str(output_path),
        ]
        print(f"  loudnorm (1-pass preview) → {output_path.name}")
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return True

    # Full two-pass
    print(f"  loudnorm pass 1: measuring {input_path.name}")
    measurement = measure_loudness(input_path)
    if measurement is None:
        print("  loudnorm measurement failed — falling back to 1-pass")
        return apply_loudnorm_two_pass(input_path, output_path, preview=True)

    print(f"    measured: I={measurement['input_i']} LUFS  "
          f"TP={measurement['input_tp']}  LRA={measurement['input_lra']}")

    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        f":measured_I={measurement['input_i']}"
        f":measured_TP={measurement['input_tp']}"
        f":measured_LRA={measurement['input_lra']}"
        f":measured_thresh={measurement['input_thresh']}"
        f":offset={measurement['target_offset']}"
        f":linear=true"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(input_path),
        "-c:v", "copy",
        "-af", filter_str,
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output_path),
    ]
    print(f"  loudnorm pass 2: normalizing → {output_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return True


# -------- Final compositing (Rule 1 + Rule 4) -------------------------------


def build_final_composite(
    base_path: Path,
    overlays: list[dict],
    subtitles_path: Path | None,
    out_path: Path,
    edit_dir: Path,
) -> None:
    """Final pass: base → overlays (PTS-shifted) → subtitles LAST → out.

    If there are no overlays and no subtitles, just copy base to out.
    """
    has_overlays = bool(overlays)
    has_subs = subtitles_path is not None and subtitles_path.exists()

    if not has_overlays and not has_subs:
        # Nothing to do — just rename/copy base to final name
        run(["ffmpeg", "-y", "-i", str(base_path), "-c", "copy", str(out_path)], quiet=True)
        return

    inputs: list[str] = ["-i", str(base_path)]
    for ov in overlays:
        ov_path = resolve_path(ov["file"], edit_dir)
        inputs += ["-i", str(ov_path)]

    filter_parts: list[str] = []
    # PTS-shift every overlay so its frame 0 lands at start_in_output
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        filter_parts.append(f"[{idx}:v]setpts=PTS-STARTPTS+{t}/TB[a{idx}]")

    # Chain overlays on top of base
    current = "[0:v]"
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        dur = float(ov["duration"])
        end = t + dur
        next_label = f"[v{idx}]"
        filter_parts.append(
            f"{current}[a{idx}]overlay=enable='between(t,{t:.3f},{end:.3f})'{next_label}"
        )
        current = next_label

    # Subtitles LAST — Rule 1
    if has_subs:
        subs_abs = str(subtitles_path.resolve()).replace(":", r"\:").replace("'", r"\'")
        filter_parts.append(
            f"{current}subtitles='{subs_abs}':force_style='{SUB_FORCE_STYLE}'[outv]"
        )
        out_label = "[outv]"
    else:
        # Rename the last overlay output to [outv] for consistency
        if has_overlays:
            filter_parts.append(f"{current}null[outv]")
            out_label = "[outv]"
        else:
            out_label = "[0:v]"

    filter_complex = ";".join(filter_parts)

    cmd = [
        FFMPEG_SUBTITLES_BIN if has_subs else "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", out_label,
        "-map", "0:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"compositing → {out_path.name}")
    print(f"  overlays: {len(overlays)}, subtitles: {'yes' if has_subs else 'no'}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# -------- Main ---------------------------------------------------------------


def _job_key(out_path: Path) -> str:
    return f"render_{out_path.stem}"


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Render a video from an EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="Output video path (required unless --validate-only)")
    ap.add_argument(
        "--validate-only",
        action="store_true",
        help="Run only the cut-vs-transcript validation and exit: prints the same "
             "CUT VALIDATION FAILED report render would print, exit 0 if clean / 1 if "
             "any range clips a word or a gap contains real speech. Renders nothing, "
             "needs no -o. This is what pipeline.py's EDL gate calls.",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="With --validate-only: emit the warnings as a JSON array of "
             "{kind: clip|speech_gap|no_transcript, source, start, end, message} "
             "instead of the text report. pipeline.py's EDL gate consumes this.",
    )
    ap.add_argument(
        "--preview",
        action="store_true",
        help="Preview mode: 1080p, medium, CRF 22 — evaluable for QC, faster than final.",
    )
    ap.add_argument(
        "--draft",
        action="store_true",
        help="Draft mode: 720p, ultrafast, CRF 28 — cut-point verification only.",
    )
    ap.add_argument(
        "--build-subtitles",
        action="store_true",
        help="Build master.srt from transcripts + EDL offsets before compositing",
    )
    ap.add_argument(
        "--no-subtitles",
        action="store_true",
        help="Skip subtitles even if the EDL references one",
    )
    ap.add_argument(
        "--no-matte",
        action="store_true",
        help="Skip background matting even if the EDL references a background image",
    )
    ap.add_argument(
        "--no-loudnorm",
        action="store_true",
        help="Skip audio loudness normalization. Default is on (-14 LUFS, -1 dBTP, LRA 11).",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Render even if cut validation finds a range that clips a word or "
             "skips real speech. Use only after reviewing the warnings yourself "
             "and confirming the cut is intentional.",
    )
    ap.add_argument(
        "--status",
        action="store_true",
        help="Report status of a background render job for this output path instead of "
             "starting one: RUNNING: / DONE: / FAILED: / NOT_FOUND:. Exit 0 unless failed/not_found.",
    )
    ap.add_argument(
        "--notify-session",
        type=str,
        default=None,
        help="OpenClaw session key to wake (via `openclaw system event`) when this background render "
             "finishes, instead of leaving the agent with no signal to check back. Pass your own "
             "session key. Omit if not running under OpenClaw (e.g. Claude Code).",
    )
    ap.add_argument(
        "--notify-profile",
        type=str,
        default=None,
        help="OpenClaw --profile to use for the completion notification (e.g. 'unleashed' for Beast; "
             "omit for Jensen's default profile). Only used with --notify-session.",
    )
    ap.add_argument(
        "--_worker",
        action="store_true",
        help=argparse.SUPPRESS,  # internal: runs the actual render synchronously
    )
    return ap


def _run_render(args: argparse.Namespace, edl_path: Path, edit_dir: Path, out_path: Path) -> None:
    edl = json.loads(edl_path.read_text())

    # 0. Validate cuts against the real transcript before doing any work.
    # See validate_cuts_against_transcripts()'s docstring/comment for why this
    # exists -- it catches cuts that would clip a word or delete real speech
    # mistaken for silence, before anything renders.
    cut_warnings = validate_cuts_against_transcripts(edl, edit_dir)
    if cut_warnings:
        print("CUT VALIDATION FAILED:")
        for w in cut_warnings:
            print(f"  - {w}")
        if not args.force:
            sys.exit(
                "\nRefusing to render. Fix the EDL ranges above, or re-run with "
                "--force if you have reviewed these and the cut is intentional."
            )
        print("\n--force set, rendering anyway despite the warnings above.\n")

    # 1. Extract per-segment (auto-grade per range if EDL grade is "auto")
    segment_paths = extract_all_segments(
        edl, edit_dir, preview=args.preview, draft=args.draft
    )

    # 2. Concat → base
    if args.draft:
        base_name = "base_draft.mp4"
    elif args.preview:
        base_name = "base_preview.mp4"
    else:
        base_name = "base.mp4"
    base_path = edit_dir / base_name
    concat_segments(segment_paths, base_path, edit_dir)

    # 2.5. Background matte (optional) — if the EDL names a background image,
    # replace the background behind the person and use that as the new base.
    if edl.get("background") and not args.no_matte:
        base_path = apply_background_matte(base_path, edl["background"], edit_dir)

    # 3. Subtitles: build if requested, resolve final path
    subs_path: Path | None = None
    if not args.no_subtitles:
        if args.build_subtitles:
            subs_path = edit_dir / "master.srt"
            build_master_srt(edl, edit_dir, subs_path)
        elif edl.get("subtitles"):
            subs_path = resolve_path(edl["subtitles"], edit_dir)
            if not subs_path.exists():
                print(f"warning: subtitles path in EDL does not exist: {subs_path}")
                subs_path = None
    # An empty SRT makes ffmpeg's subtitles filter exit non-zero (183) and
    # kills the whole render. If there are no cues, drop subtitles with a
    # loud warning instead -- a captionless render beats no render.
    if subs_path is not None:
        srt_text = subs_path.read_text() if subs_path.exists() else ""
        if not srt_text.strip() or "-->" not in srt_text:
            print(f"WARNING: {subs_path.name} has no cues — rendering WITHOUT subtitles. "
                  f"(Check that the source transcript resolved; see 'no transcript for' above.)")
            subs_path = None

    # 4. Composite (overlays + subtitles LAST) → intermediate (pre-loudnorm) path
    overlays = edl.get("overlays") or []
    if args.no_loudnorm:
        # Composite directly to final output
        build_final_composite(base_path, overlays, subs_path, out_path, edit_dir)
    else:
        # Composite to a temp file, then run loudnorm → final output
        tmp_composite = out_path.with_suffix(".prenorm.mp4")
        build_final_composite(base_path, overlays, subs_path, tmp_composite, edit_dir)
        print("loudness normalization → social-ready (-14 LUFS / -1 dBTP / LRA 11)")
        apply_loudnorm_two_pass(tmp_composite, out_path, preview=args.draft)
        tmp_composite.unlink(missing_ok=True)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\ndone: {out_path} ({size_mb:.1f} MB)")


def main() -> None:
    args = _build_arg_parser().parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    edit_dir = edl_path.parent

    if args.validate_only:
        # Cut-vs-transcript check only. Renders nothing, needs no -o. Same report
        # body as the pre-render gate in _run_render(), but as a standalone exit
        # code so pipeline.py's EDL gate can block on it. With --json, classify
        # each warning so the gate can treat mid-word clips (never allowed) apart
        # from removed-speech gaps (allowed if the EDL declares them).
        try:
            edl = json.loads(edl_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            if args.json:
                print(json.dumps([{"kind": "parse_error", "source": None,
                                   "start": None, "end": None, "message": str(e)}]))
            else:
                print(f"CUT VALIDATION FAILED:\n  - could not read EDL: {e}")
            sys.exit(1)
        cut_warnings = validate_cuts_against_transcripts(edl, edit_dir)
        cut_records = [_classify_cut_warning(w) for w in cut_warnings]

        # Asset references (background / subtitles file) must resolve, or the
        # render silently skips them -- 2026-09-08 a bare-filename `background`
        # produced an un-swapped output with only a buried render-time warning.
        asset_records: list[dict] = []
        bg = edl.get("background")
        if bg and resolve_asset_path(bg, edit_dir, edl) is None:
            asset_records.append({
                "kind": "background", "source": None, "start": None, "end": None,
                "message": f"background {bg!r} does not resolve to a file (looked "
                           f"beside the edit dir, the source video, and the sources). "
                           f"The swap would be silently skipped. Use an absolute path "
                           f"or place the file beside the source video.",
            })
        subs = edl.get("subtitles")
        if isinstance(subs, str) and subs and not resolve_path(subs, edit_dir).exists():
            asset_records.append({
                "kind": "subtitles", "source": None, "start": None, "end": None,
                "message": f"subtitles file {subs!r} does not exist; captions would "
                           f"be skipped. Omit the field to auto-build from transcripts.",
            })

        records = cut_records + asset_records
        if args.json:
            print(json.dumps(records))
            sys.exit(1 if records else 0)
        if records:
            print("VALIDATION FAILED:")
            for r in records:
                print(f"  - {r['message']}")
            sys.exit(1)
        print("VALIDATION OK: cuts land in silence, all removed speech declared, "
              "assets resolve.")
        sys.exit(0)

    if args.output is None:
        sys.exit("render.py: -o/--output is required unless --validate-only is set")
    out_path = args.output.resolve()
    job_key = _job_key(out_path)

    if args.status:
        sys.exit(_job_lock.print_status_line(edit_dir, job_key, cached_output=out_path))

    if args._worker:
        # Runs detached, spawned by the block below. Does the real render and
        # reports the outcome into the lock file for --status to read.
        try:
            _run_render(args, edl_path, edit_dir, out_path)
            _job_lock.mark_done(edit_dir, job_key, str(out_path),
                                 session_key=args.notify_session, profile=args.notify_profile,
                                 script_name="render.py")
        except SystemExit as e:
            _job_lock.mark_failed(edit_dir, job_key, str(e.code),
                                   session_key=args.notify_session, profile=args.notify_profile,
                                   script_name="render.py")
            raise
        except Exception as e:  # noqa: BLE001 -- must record failure, not crash silently
            _job_lock.mark_failed(edit_dir, job_key, str(e),
                                   session_key=args.notify_session, profile=args.notify_profile,
                                   script_name="render.py")
            raise
        return

    # Foreground entry point: never blocks on the actual render (which can
    # run minutes on a full clip with matte/overlays) -- either a job for
    # this output is already running (reports status, does not launch a
    # duplicate ffmpeg/matte pipeline), or a new background worker is
    # spawned and this call returns right away. Same self-backgrounding
    # rationale as transcribe.py -- see _job_lock.py.
    running = _job_lock.check_running_job(edit_dir, job_key)
    if running:
        elapsed = time.time() - running.get("started_at", time.time())
        print(
            f"ALREADY_RUNNING: elapsed={elapsed:.0f}s pid={running.get('pid')} -- "
            f"poll with: render.py {edl_path} -o {out_path} --status"
        )
        return

    worker_argv = [sys.executable, str(Path(__file__).resolve()), str(edl_path), "-o", str(out_path), "--_worker"]
    if args.preview:
        worker_argv.append("--preview")
    if args.draft:
        worker_argv.append("--draft")
    if args.build_subtitles:
        worker_argv.append("--build-subtitles")
    if args.no_subtitles:
        worker_argv.append("--no-subtitles")
    if args.no_matte:
        worker_argv.append("--no-matte")
    if args.no_loudnorm:
        worker_argv.append("--no-loudnorm")
    if args.force:
        worker_argv.append("--force")
    if args.notify_session:
        worker_argv += ["--notify-session", args.notify_session]
    if args.notify_profile:
        worker_argv += ["--notify-profile", args.notify_profile]

    job = _job_lock.spawn_background_worker(edit_dir, job_key, worker_argv)
    print(
        f"STARTED: pid={job['pid']} -- "
        f"poll with: render.py {edl_path} -o {out_path} --status"
    )


if __name__ == "__main__":
    main()
