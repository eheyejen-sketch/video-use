#!/Users/mikeattreys/Developer/video-use/.venv/bin/python3
"""Deterministic filler-word removal — expand coarse EDL ranges into the
concrete cut list that actually strips every um/uh.

Why: authoring a filler-removal edit by hand is 15-20 tiny ranges with
30-200ms padding each. LLMs get that wrong (zero-width ranges, drifted
timestamps) and a large local model can run out of its token budget
before finishing the JSON. So the LLM authors only COARSE structural
ranges plus `"strip_fillers": true`, and this module does the micro-cuts
from `check_fillers.py`'s exact timestamps.

For each kept range, every filler word wholly inside it (from the verbatim
transcript) becomes a split point: the range is broken into sub-ranges
that exclude `[filler_start - pad, filler_end + pad]`. Sub-ranges shorter
than `min_subrange` are dropped (two adjacent fillers collapse to one
cut). Zero-/near-zero-duration ASR filler artifacts are ignored.

The gaps this creates contain only filler words, so render.py's cut
validator passes them (see `_check_gap`'s filler-only allowance).

CLI (debugging):
    filler_cuts.py <edl.json> --edit-dir <dir> [-o <edl.effective.json>]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from check_fillers import FILLER_WORDS, _normalize as _norm_word
except Exception:  # pragma: no cover
    import re as _re
    FILLER_WORDS = {"um", "umm", "uhm", "uh", "uhh", "er", "erm", "ah", "hmm", "mm"}
    _rw = _re.compile(r"[a-z']+")

    def _norm_word(text: str) -> str:
        m = _rw.search(text.lower())
        return m.group(0) if m else ""

# outward padding on each side of a removed filler; Whisper word timestamps
# drift 50-100ms, so trimming a hair of the neighbours is safer than
# leaving an "uh" fragment. Kept small to limit neighbour erosion.
DEFAULT_PAD_S = 0.04
# a kept sub-range shorter than this is noise (e.g. between two adjacent
# fillers) -- drop it, which merges the cut.
DEFAULT_MIN_SUBRANGE_S = 0.08
# ignore filler entries this short -- zero-duration ASR decode artifacts
MIN_FILLER_DUR_S = 0.05


def _load_words(edit_dir: Path, source_name: str, edl: dict | None = None) -> list[dict]:
    # transcripts are named by the source file's stem, not the EDL key.
    names = [source_name]
    if edl:
        p = (edl.get("sources") or {}).get(source_name)
        if p:
            names.append(Path(p).stem)
    for name in names:
        p = edit_dir / "transcripts" / f"{name}.json"
        if p.exists():
            data = json.loads(p.read_text())
            return [w for w in data.get("words", []) if w.get("type", "word") == "word"]
    raise FileNotFoundError(
        f"no transcript for source {source_name!r} (tried {', '.join(names)}) in {edit_dir}"
    )


def _fillers_in(words: list[dict], lo: float, hi: float) -> list[tuple[float, float, str]]:
    out = []
    for w in words:
        s, e = w.get("start"), w.get("end")
        if s is None or e is None:
            continue
        if (e - s) < MIN_FILLER_DUR_S:
            continue
        if _norm_word(w.get("text", "")) not in FILLER_WORDS:
            continue
        if s >= lo and e <= hi:
            out.append((float(s), float(e), w.get("text", "")))
    out.sort()
    return out


def strip_fillers_from_range(
    r: dict, words: list[dict], pad: float, min_subrange: float
) -> list[dict]:
    """One coarse range -> 1..N sub-ranges with the fillers removed."""
    start, end = float(r["start"]), float(r["end"])
    fillers = _fillers_in(words, start, end)
    if not fillers:
        return [dict(r)]

    subs: list[tuple[float, float]] = []
    cursor = start
    for fs, fe, _txt in fillers:
        seg_end = fs - pad
        if seg_end - cursor >= min_subrange:
            subs.append((cursor, seg_end))
        cursor = fe + pad
    if end - cursor >= min_subrange:
        subs.append((cursor, end))

    if not subs:  # entire range was filler + slivers
        return []

    out = []
    n = len(subs)
    for i, (s, e) in enumerate(subs):
        sub = dict(r)
        sub["start"] = round(s, 3)
        sub["end"] = round(e, 3)
        sub["reason"] = f"{r.get('reason', '').rstrip('.')}. [filler-stripped {i + 1}/{n}]".lstrip(". ")
        out.append(sub)
    return out


def expand_edl(edl: dict, edit_dir: Path,
               pad: float = DEFAULT_PAD_S,
               min_subrange: float = DEFAULT_MIN_SUBRANGE_S) -> tuple[dict, dict]:
    """Return (effective_edl, stats). If strip_fillers is not set, effective
    == a copy of edl. `stats` = {author_ranges, effective_ranges, fillers_removed}."""
    eff = json.loads(json.dumps(edl))  # deep copy
    if not edl.get("strip_fillers"):
        return eff, {"author_ranges": len(edl.get("ranges", [])),
                     "effective_ranges": len(edl.get("ranges", [])),
                     "fillers_removed": 0}

    words_by_src: dict[str, list[dict]] = {}
    new_ranges: list[dict] = []
    removed = 0
    for r in edl.get("ranges", []):
        src = r["source"]
        if src not in words_by_src:
            words_by_src[src] = _load_words(edit_dir, src, edl)
        before = len(new_ranges)
        expanded = strip_fillers_from_range(r, words_by_src[src], pad, min_subrange)
        new_ranges.extend(expanded)
        # fillers removed from this range = (sub-ranges produced) implies
        # (sub-ranges - 1) internal cuts when >0, but count the actual fillers:
        removed += len(_fillers_in(words_by_src[src], float(r["start"]), float(r["end"])))
        _ = before  # (kept for readability)

    eff["ranges"] = new_ranges
    eff["_derived_from"] = "edl.json (strip_fillers)"
    return eff, {
        "author_ranges": len(edl.get("ranges", [])),
        "effective_ranges": len(new_ranges),
        "fillers_removed": removed,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Expand an EDL, stripping filler words")
    ap.add_argument("edl", type=Path)
    ap.add_argument("--edit-dir", type=Path, default=None,
                    help="dir containing transcripts/ (default: the EDL's parent)")
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument("--pad", type=float, default=DEFAULT_PAD_S)
    ap.add_argument("--min-subrange", type=float, default=DEFAULT_MIN_SUBRANGE_S)
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    edit_dir = (args.edit_dir or edl_path.parent).resolve()
    edl = json.loads(edl_path.read_text())
    eff, stats = expand_edl(edl, edit_dir, args.pad, args.min_subrange)

    out = args.output or edl_path.with_name("edl.effective.json")
    out.write_text(json.dumps(eff, indent=2))
    print(f"strip_fillers={bool(edl.get('strip_fillers'))}: "
          f"{stats['author_ranges']} author range(s) -> {stats['effective_ranges']} "
          f"effective range(s), {stats['fillers_removed']} filler(s) removed. "
          f"Wrote {out}")


if __name__ == "__main__":
    main()
