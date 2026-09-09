#!/Users/mikeattreys/Developer/video-use/.venv/bin/python3
"""qc_render.py -- deterministic quality check of a video-use render.

Why this exists
---------------
SELF_EVAL used to require a sighted human (or Claude Code) to inspect the
`eval/*.png` composites, write `eval/eval_review.md`, and pass the verdict. An
OpenClaw agent is text-only and cannot do this -- and has fabricated a review to
get through. That made every agent-started edit need a person at the keyboard.

A local vision model was tried as the reviewer (`describe_frames.py`, now gone):
qwen3.6's unsuppressable reasoning ate the token budget on every image call
(empty responses) and each call ran 20-90s -- ~15 min for one edit, unreliable.

So this checks what CAN be verified deterministically and fast, which happens to
be exactly the regression classes we've actually hit:
  - output duration vs the EDL
  - final.mp4 integrity (video + audio streams, non-empty)
  - background swap ACTUALLY ran (from render.py's own log lines)
  - captions: present, sane count, in-bounds, no pure-filler cues, not an
    obviously abrupt mid-sentence ending

It writes `eval/eval_review.md` and prints a verdict:

    VERDICT: PASS
  or
    VERDICT: ISSUES
    - <concrete problem>

`pipeline.py` uses PASS to finish and ISSUES to finish-but-flag ("reply 'recut
...' or 'ship it'"). Exit 2 = the check itself could not run.

Aesthetic judgement (is this the RIGHT cut, does it feel tight) is NOT checked --
the user reviews the finished video for that. This gate only catches things that
are objectively broken.

CLI:  qc_render.py <edit-dir> [--json] [-o PATH]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

DUR_TOLERANCE_S = 1.5
try:
    from check_fillers import FILLER_WORDS, _normalize as _norm_word
except Exception:  # pragma: no cover
    FILLER_WORDS = {"um", "umm", "uhm", "uh", "uhh", "er", "erm", "ah", "hmm", "mm"}
    _wre = re.compile(r"[a-z']+")

    def _norm_word(t: str) -> str:
        m = _wre.search(t.lower())
        return m.group(0) if m else ""


def _ffprobe(path: Path, *entries: str) -> str:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-of", "default=nw=1:nk=1", *entries, str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _effective_edl(edit_dir: Path) -> dict:
    for name in ("edl.effective.json", "edl.json"):
        p = edit_dir / name
        if p.exists():
            try:
                return json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                pass
    return {}


def _authored_edl(edit_dir: Path) -> dict:
    p = edit_dir / "edl.json"
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _expected_duration(eff: dict) -> float:
    return round(sum(float(r["end"]) - float(r["start"]) for r in eff.get("ranges", [])), 2)


def check(edit_dir: Path) -> tuple[str, list[str], list[str]]:
    """Returns (verdict, issues, notes). verdict in PASS / ISSUES / ERROR."""
    final = edit_dir / "final.mp4"
    issues: list[str] = []
    notes: list[str] = []

    if not final.exists() or final.stat().st_size == 0:
        return "ERROR", [f"{final} missing or empty"], []

    # A restage that didn't re-render leaves final.mp4 older than the EDL it was
    # supposedly built from (2026-09-09 recut bug). Catch it here regardless of
    # how the render was (not) triggered.
    for edl_name in ("edl.effective.json", "edl.json"):
        ep = edit_dir / edl_name
        try:
            if ep.exists() and ep.stat().st_mtime > final.stat().st_mtime + 2:
                issues.append(f"final.mp4 is OLDER than {edl_name} — the render is "
                              f"stale, it was not rebuilt after the EDL changed")
                break
        except OSError:
            pass

    # --- mp4 integrity -------------------------------------------------------
    vcodec = _ffprobe(final, "-select_streams", "v:0", "-show_entries", "stream=codec_name")
    acodec = _ffprobe(final, "-select_streams", "a:0", "-show_entries", "stream=codec_name")
    dur_s = _ffprobe(final, "-show_entries", "format=duration")
    width = _ffprobe(final, "-select_streams", "v:0", "-show_entries", "stream=width")
    height = _ffprobe(final, "-select_streams", "v:0", "-show_entries", "stream=height")
    try:
        actual = round(float(dur_s), 2)
    except ValueError:
        return "ERROR", ["could not probe final.mp4 duration"], []
    if not vcodec:
        issues.append("final.mp4 has no video stream")
    if not acodec:
        issues.append("final.mp4 has no audio stream")
    notes.append(f"final.mp4: {width}x{height} {vcodec}/{acodec}, {actual}s")

    # --- duration vs EDL --------------------------------------------------
    eff = _effective_edl(edit_dir)
    expected = _expected_duration(eff) or actual
    delta = abs(actual - expected)
    if delta > DUR_TOLERANCE_S:
        issues.append(f"duration {actual}s vs EDL {expected}s (Δ{delta:.2f}s) — "
                      f"the render is not the length the EDL describes")
    else:
        notes.append(f"duration {actual}s vs EDL {expected}s — ok (Δ{delta:.2f}s)")

    # --- background swap actually ran -----------------------------------
    authored = _authored_edl(edit_dir)
    if authored.get("background"):
        log = edit_dir / "jobs" / "render_final.log"
        text = log.read_text() if log.exists() else ""
        ran = bool(re.search(r"background matte .*→", text)) and "frames matted" in text
        skipped = re.search(r"warning: background .*(not found|does not exist|skipping matte|"
                            r"matte failed|NOT swapped)", text)
        if skipped or not ran:
            issues.append("background swap was requested but did NOT run — "
                          f"{'render log: ' + skipped.group(0) if skipped else 'no matte log lines found'}")
        else:
            notes.append("background swap ran (matte log present)")

    # --- captions ------------------------------------------------------
    srt = edit_dir / "master.srt"
    if not srt.exists():
        if authored.get("subtitles") or eff.get("strip_fillers") is not None:
            notes.append("no master.srt (subtitles may be off for this edit)")
    else:
        cues = _parse_srt(srt.read_text())
        if len(cues) < 3:
            issues.append(f"master.srt has only {len(cues)} cue(s) — captions likely broke")
        else:
            notes.append(f"master.srt: {len(cues)} cues")
        fillers = [t for _, _, t in cues
                   if t and all(_norm_word(w) in FILLER_WORDS for w in t.split())]
        if fillers:
            issues.append(f"{len(fillers)} caption cue(s) are pure filler words "
                          f"({', '.join(sorted(set(fillers))[:5])}) — strip_fillers "
                          f"should have removed these")
        if cues:
            last_end = cues[-1][1]
            if last_end > actual + 0.5:
                issues.append(f"last caption ends at {last_end:.2f}s but video is {actual:.2f}s "
                              f"— captions run past the end")
            # abrupt-ending heuristic: last cue butts right up against the end
            if actual - last_end < 0.15 and acodec:
                notes.append(f"note: last caption ends {actual - last_end:.2f}s before the "
                             f"video ends — check the final line isn't cut mid-sentence")

    verdict = "ISSUES" if issues else "PASS"
    return verdict, issues, notes


_SRT_TS = re.compile(r"(\d\d):(\d\d):(\d\d),(\d\d\d)\s*-->\s*(\d\d):(\d\d):(\d\d),(\d\d\d)")


def _parse_srt(text: str) -> list[tuple[float, float, str]]:
    out: list[tuple[float, float, str]] = []
    for block in text.strip().split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        m = _SRT_TS.search(block)
        if not m:
            continue
        a = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + int(m[4]) / 1000
        b = int(m[5]) * 3600 + int(m[6]) * 60 + int(m[7]) + int(m[8]) / 1000
        txt = " ".join(lines[2:]) if lines[0].isdigit() else " ".join(lines[1:])
        out.append((a, b, txt.strip()))
    return out


def _markdown(edit_dir: Path, verdict: str, issues: list[str], notes: list[str]) -> str:
    lines = [f"# Render QC — qc_render.py\n",
             f"**Verdict: {verdict}**\n",
             "Deterministic checks only (duration, mp4 integrity, background-swap "
             "log, captions). Aesthetic judgement is the user's — they review the "
             "finished video.\n"]
    if issues:
        lines.append("## Issues\n" + "\n".join(f"- {i}" for i in issues) + "\n")
    lines.append("## Checks\n" + "\n".join(f"- {n}" for n in notes) + "\n")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(prog="qc_render.py")
    ap.add_argument("edit_dir", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    edit_dir = args.edit_dir.resolve()
    out_path = args.out or (edit_dir / "eval" / "eval_review.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        verdict, issues, notes = check(edit_dir)
    except Exception as e:  # noqa: BLE001
        verdict, issues, notes = "ERROR", [f"qc_render.py crashed: {e!r}"], []

    out_path.write_text(_markdown(edit_dir, verdict, issues, notes))

    if args.json:
        print(json.dumps({"verdict": verdict, "issues": issues, "review": str(out_path)}))
    else:
        print(f"VERDICT: {verdict}")
        for i in issues:
            print(f"- {i}")
        print(f"(review written to {out_path})")
    sys.exit(0 if verdict in ("PASS", "ISSUES") else 2)


if __name__ == "__main__":
    main()
