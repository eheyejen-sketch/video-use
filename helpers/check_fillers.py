#!/Users/mikeattreys/Developer/video-use/.venv/bin/python3
"""Deterministically report filler words in a transcript -- refuses to
answer on a non-verbatim transcript instead of silently reporting zero.

Local Whisper normalizes out filler words ("um"/"uh") by default; a
transcript produced without --verbatim will show zero fillers regardless
of what's actually in the audio, and an LLM reading that transcript has
repeatedly (2026-09-07, twice, two different agents) reported "no fillers
detected" as if it were a real finding. This script makes that claim
impossible to produce from the wrong input: it exits nonzero with a clear
message instead of printing a filler count when verbatim=False, so the
only way to get an answer is to actually run the verbatim pass first.

Usage:
    check_fillers.py <video_or_transcript_path> [--edit-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Conservative, low-false-positive filler set. Deliberately excludes
# context-dependent words like "like" or "you know" -- those need real
# judgment to classify as filler vs. meaningful word, which is exactly the
# kind of subjective call that stays with the agent. This script only
# reports the unambiguous cases.
FILLER_WORDS = {"um", "umm", "uhm", "uh", "uhh", "er", "erm", "ah", "hmm", "mm"}

_WORD_RE = re.compile(r"[a-z']+")


def _normalize(text: str) -> str:
    m = _WORD_RE.search(text.lower())
    return m.group(0) if m else ""


def resolve_transcript_path(target: Path, edit_dir: Path | None) -> Path:
    if target.suffix.lower() == ".json":
        return target
    ed = edit_dir or (target.parent / "edit")
    return ed / "transcripts" / f"{target.stem}.json"


def main() -> None:
    ap = argparse.ArgumentParser(description="Deterministic filler-word report (verbatim transcripts only)")
    ap.add_argument("path", type=Path, help="Video path or transcript JSON path")
    ap.add_argument("--edit-dir", type=Path, default=None, help="Edit dir (only used if path is a video)")
    args = ap.parse_args()

    transcript_path = resolve_transcript_path(args.path.resolve(), args.edit_dir)
    if not transcript_path.exists():
        sys.exit(f"NOT_FOUND: no transcript at {transcript_path}")

    data = json.loads(transcript_path.read_text())

    if not data.get("verbatim", False):
        sys.exit(
            "REFUSED: transcript was not produced in verbatim mode (verbatim=false) -- "
            "filler words are normalized out by default, so this transcript cannot answer "
            "whether fillers are present. Re-run: transcribe.py <video> --verbatim first, "
            "then re-run this check against the same video."
        )

    hits = []
    for w in data.get("words", []):
        norm = _normalize(w.get("text", ""))
        if norm in FILLER_WORDS:
            hits.append(w)

    print(f"FILLERS_FOUND: {len(hits)}")
    for w in hits:
        print(f"  [{w.get('start'):.2f}-{w.get('end'):.2f}] {w.get('text')}")


if __name__ == "__main__":
    main()
