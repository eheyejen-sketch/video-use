#!/Users/mikeattreys/Developer/video-use/.venv/bin/python3
"""pipeline.py -- the driver that OWNS the video-use editing process order.

Why this exists
---------------
`video-use` was prose guidance + a bag of standalone helper scripts, and the
LLM agent chose which to run and in what order. Three separate agent runs
(2026-09-07 x2, 2026-09-08 Beast) made the same class of mistake regardless of
how emphatically SKILL.md's Hard Rules were written: an unverified filler-word
claim, a fabricated "silence gap" that actually contained speech, skipping the
packed-transcript step. Every "you must run X before claiming Y" was a sentence
in a document, not a constraint.

This script makes step order a constraint. It is a small state machine:

    INGEST -> STRATEGY -> EDL -> RENDER -> SELF_EVAL -> DONE

Each `pipeline.py <edit-dir>` call runs ALL the deterministic work for the
current phase itself -- ffprobe, verbatim transcription, packing, the filler
check, the silence-gap computation, the render, the self-eval frame extraction
-- then either advances automatically or stops and prints the single artifact
the agent owes back (a strategy, an EDL, a self-eval verdict). The agent cannot
skip or reorder a step because it never invokes the underlying steps; this
script does. The agent's job is only the genuinely subjective calls: what to
cut, pacing, grade, caption style, animation design.

State lives in `<edit-dir>/pipeline_state.json`. Every phase is re-entrant;
deleting the edit dir and re-running `init` starts clean.

CLI
---
    pipeline.py init <video> [<video> ...] --edit-dir DIR
                 [--notify-session KEY] [--notify-profile PROFILE]
    pipeline.py <edit-dir>                     # run/advance the current phase
    pipeline.py <edit-dir> --status            # report only, never mutates
    pipeline.py <edit-dir> --confirm-strategy  # STRATEGY gate
    pipeline.py <edit-dir> --eval-verdict pass|fail [--restage edl|render]
    pipeline.py <edit-dir> --restage edl|render   # manual step-back

Exit codes: 0 = phase advanced / waiting on a background job / info printed
(the agent should read the message and act). 1 = a gate REFUSED the agent's
artifact (fix it and re-run). 2 = hard error (missing state, missing source,
transcription failed).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HELPERS = Path(__file__).resolve().parent
STATE_NAME = "pipeline_state.json"
PHASES = ["INGEST", "STRATEGY", "EDL", "RENDER", "SELF_EVAL", "DONE"]

# an inter-word gap at least this long is reported in the gap table
GAP_MIN_S = 0.30
# ... and at least this long is flagged a "clean cut candidate"
GAP_CLEAN_S = 0.40
# ignore ASR word timestamps shorter than this (zero-duration decode artifacts)
MIN_WORD_DUR_S = 0.001


# ─────────────────────────────── state ────────────────────────────────

def _fresh_gates() -> dict:
    return {
        "ingest_briefing": {"done": False, "artifact": "briefing.md"},
        "strategy_confirmed": {"done": False},
        "edl_validated": {"done": False},
        "render_done": {"done": False, "output": None},
        "self_eval": {"passes": 0, "verdict": None},
    }


def state_path(edit_dir: Path) -> Path:
    return edit_dir / STATE_NAME


def load_state(edit_dir: Path) -> dict:
    p = state_path(edit_dir)
    if not p.exists():
        sys.exit(
            f"no {STATE_NAME} in {edit_dir}. Run `pipeline.py init <video> "
            f"--edit-dir {edit_dir}` first."
        )
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as e:
        sys.exit(f"could not read {p}: {e}")


def save_state(state: dict) -> None:
    state["updated_at"] = time.time()
    p = state_path(Path(state["edit_dir"]))
    p.write_text(json.dumps(state, indent=2))


# ──────────────────────────── subprocess ──────────────────────────────

def run_helper(script: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Invoke a sibling helper script under the same interpreter."""
    cmd = [sys.executable, str(HELPERS / script), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        sys.exit(
            f"helper failed: {script} {' '.join(args)}\n"
            f"  stdout: {proc.stdout.strip()}\n  stderr: {proc.stderr.strip()}"
        )
    return proc


def notify_args(state: dict) -> list[str]:
    n = state.get("notify") or {}
    out: list[str] = []
    if n.get("session_key"):
        out += ["--notify-session", n["session_key"]]
    if n.get("profile"):
        out += ["--notify-profile", n["profile"]]
    return out


def running_as_agent(state: dict) -> bool:
    """True when the pipeline was `init`-ed by an OpenClaw agent (Jensen/Beast) --
    they pass --notify-session/--notify-profile. Claude Code passes neither.
    Used to gate steps an agent structurally can't do (viewing eval frames)."""
    n = state.get("notify") or {}
    return bool(n.get("profile") or n.get("session_key"))


def ffprobe_json(src: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(src)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"ffprobe failed on {src}: {proc.stderr.strip()}")
    raw = json.loads(proc.stdout)
    v = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in raw.get("streams", []) if s.get("codec_type") == "audio"), {})
    fmt = raw.get("format", {})
    fr = v.get("avg_frame_rate", "0/0")
    try:
        num, den = fr.split("/")
        fps = round(float(num) / float(den), 3) if float(den) else None
    except (ValueError, ZeroDivisionError):
        fps = None
    return {
        "path": str(src),
        "width": v.get("width"),
        "height": v.get("height"),
        "fps": fps,
        "video_codec": v.get("codec_name"),
        "audio_codec": a.get("codec_name"),
        "audio_channels": a.get("channels"),
        "duration_s": round(float(fmt["duration"]), 3) if fmt.get("duration") else None,
    }


# ─────────────────────────── transcript / gaps ────────────────────────

def transcript_path(edit_dir: Path, src: Path) -> Path:
    return edit_dir / "transcripts" / f"{src.stem}.json"


def load_words(edit_dir: Path, src: Path) -> list[dict]:
    data = json.loads(transcript_path(edit_dir, src).read_text())
    return data.get("words", [])


def raw_text(words: list[dict]) -> str:
    return " ".join(w.get("text", "") for w in words).strip()


def compute_gaps(words: list[dict]) -> dict:
    """Inter-word silence gaps computed from the per-word JSON -- the artifact
    that replaces an agent eyeballing the transcript. Zero-duration ASR
    artifacts are skipped so they don't mask a real gap or invent a fake one."""
    real = [
        w for w in words
        if w.get("start") is not None and w.get("end") is not None
        and (w["end"] - w["start"]) >= MIN_WORD_DUR_S
    ]
    real.sort(key=lambda w: w["start"])
    gaps = []
    for a, b in zip(real, real[1:]):
        dur = b["start"] - a["end"]
        if dur >= GAP_MIN_S:
            gaps.append({
                "after_word": a.get("text", ""),
                "before_word": b.get("text", ""),
                "gap_start": round(a["end"], 2),
                "gap_end": round(b["start"], 2),
                "duration_s": round(dur, 2),
                "clean_candidate": dur >= GAP_CLEAN_S,
            })
    return {
        "transcript_start_s": round(real[0]["start"], 2) if real else None,
        "transcript_end_s": round(real[-1]["end"], 2) if real else None,
        "word_count": len(real),
        "gap_min_threshold_s": GAP_MIN_S,
        "clean_candidate_threshold_s": GAP_CLEAN_S,
        "gaps": gaps,
    }


def timeline_sample(src: Path, start: float, end: float, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    run_helper("timeline_view.py", str(src), f"{start:.2f}", f"{end:.2f}",
               "-o", str(out), check=False)


# ───────────────────────────── briefing ──────────────────────────────

BRIEFING_HEADER = """# Editing briefing — generated by pipeline.py

**These numbers were computed from the per-word transcript JSON. Do not
recompute them by reading the transcript yourself. When you cite a gap, a
quote, or a timestamp in your strategy or EDL, cite it FROM THIS FILE.** The
silence-gap table below is the authoritative list of cut candidates; there are
no gaps other than the ones listed.
"""


def _gap_table(gaps: dict) -> str:
    if not gaps["gaps"]:
        return "_No inter-word gap >= %.2fs anywhere in this source._\n" % GAP_MIN_S
    lines = [
        f"Transcript spans {gaps['transcript_start_s']}s – {gaps['transcript_end_s']}s, "
        f"{gaps['word_count']} words. Gaps >= {GAP_MIN_S}s "
        f"(** = clean cut candidate, >= {GAP_CLEAN_S}s):",
        "",
        "| gap (s) | window | after word | before word | clean? |",
        "|---|---|---|---|---|",
    ]
    for g in gaps["gaps"]:
        lines.append(
            f"| {g['duration_s']:.2f} | {g['gap_start']:.2f}–{g['gap_end']:.2f} "
            f"| …{g['after_word']!r} | {g['before_word']!r}… "
            f"| {'**yes**' if g['clean_candidate'] else 'marginal'} |"
        )
    return "\n".join(lines) + "\n"


def write_briefing(state: dict, inv: dict, sources: list[Path], gaps_by_src: dict) -> Path:
    edit_dir = Path(state["edit_dir"])
    parts = [BRIEFING_HEADER, "\n## Source inventory\n",
             "| source | w×h | fps | v codec | a codec | ch | duration |",
             "|---|---|---|---|---|---|---|"]
    for s in sources:
        m = inv[s.name]
        parts.append(
            f"| {s.name} | {m['width']}×{m['height']} | {m['fps']} | "
            f"{m['video_codec']} | {m['audio_codec']} | {m['audio_channels']} | "
            f"{m['duration_s']}s |"
        )
    parts.append("")

    packed = edit_dir / "takes_packed.md"
    filler = edit_dir / "filler_report.txt"
    has_samples = any((edit_dir / "verify").glob("*_sample_*.png"))

    for s in sources:
        words = load_words(edit_dir, s)
        parts.append(f"\n## {s.name}\n")
        parts.append("### Verbatim transcript (raw — this is the ground truth)\n")
        parts.append("> " + raw_text(words).replace("\n", " ") + "\n")
        parts.append("### Silence-gap table (authoritative cut candidates)\n")
        parts.append(_gap_table(gaps_by_src[s.name]))
        if has_samples:
            parts.append("### Visual samples\n")
            parts.append(f"- `verify/{s.stem}_sample_head.png` (0–10s)\n"
                         f"- `verify/{s.stem}_sample_mid.png` (midpoint ±5s)\n"
                         f"Ask for more with `timeline_view.py` at any decision point.\n")

    if packed.exists():
        parts.append("\n## Packed phrase-level transcript (`takes_packed.md`)\n")
        parts.append("```\n" + packed.read_text().strip() + "\n```\n")
    if filler.exists():
        parts.append("\n## Filler-word report (`check_fillers.py`, verbatim pass)\n")
        parts.append("```\n" + filler.read_text().strip() + "\n```\n")
        parts.append(
            "\n_This is the ONLY basis for any filler-word statement. If it says "
            "`REFUSED` or is empty, you cannot claim anything about fillers._\n"
        )

    out = edit_dir / "briefing.md"
    out.write_text("\n".join(parts))
    return out


# ────────────────────────── phase: INGEST ────────────────────────────

INGEST_DONE_MSG = """\
INGEST complete. The pipeline has run: ffprobe, verbatim transcription,
pack_transcripts, check_fillers, and the silence-gap computation.

YOU OWE: read `{edit}/briefing.md` in full. Converse with the user about
content type, target length/aspect, aesthetic, pacing, must-keep and
must-cut moments. Then write your proposed strategy (4–8 sentences: shape,
cut direction, length target, grade, subtitle style) to `{edit}/strategy.md`,
INCLUDING a `## User confirmation` section that quotes the user approving the
plan in plain English. Then run:

    pipeline.py {edit} --confirm-strategy
"""


def phase_ingest(state: dict) -> None:
    edit_dir = Path(state["edit_dir"])
    sources = [Path(s) for s in state["sources"]]
    (edit_dir / "transcripts").mkdir(parents=True, exist_ok=True)

    # 1. inventory
    inv = {s.name: ffprobe_json(s) for s in sources}
    (edit_dir / "inventory.json").write_text(json.dumps(inv, indent=2))

    # 2. verbatim transcription -- forced; the agent never picks the mode
    pending = []
    for s in sources:
        common = [str(s), "--verbatim", "--edit-dir", str(edit_dir)]
        run_helper("transcribe.py", *common, *notify_args(state), check=False)
        st = run_helper("transcribe.py", *common, "--status", check=False)
        line = st.stdout.strip()
        if line.startswith("DONE:"):
            continue
        if line.startswith("FAILED:"):
            print(f"INGEST FAILED: verbatim transcription of {s.name}: {line}")
            sys.exit(2)
        pending.append(s.name)
    if pending:
        done = len(sources) - len(pending)
        print(f"WAITING: verbatim transcription in progress ({done}/{len(sources)} done). "
              f"Still running: {', '.join(pending)}.\n"
              f"You'll be notified when it finishes (or re-run `pipeline.py {edit_dir}`).")
        sys.exit(0)

    # 3. verbatim assertion
    for s in sources:
        tj = json.loads(transcript_path(edit_dir, s).read_text())
        if not tj.get("verbatim"):
            print(f"INGEST FAILED: transcript for {s.name} is not verbatim "
                  f"(verbatim={tj.get('verbatim')!r}). This should not happen — "
                  f"the pipeline forced --verbatim. Delete the transcript and re-run.")
            sys.exit(2)

    # 4. pack
    run_helper("pack_transcripts.py", "--edit-dir", str(edit_dir))

    # 5. filler report (deterministic; verbatim guaranteed by step 3)
    blocks = []
    for s in sources:
        r = run_helper("check_fillers.py", str(s), "--edit-dir", str(edit_dir), check=False)
        blocks.append(f"## {s.name}\n{(r.stdout or r.stderr).strip()}")
    (edit_dir / "filler_report.txt").write_text("\n\n".join(blocks) + "\n")

    # 6. silence-gap table
    gaps_by_src = {}
    for s in sources:
        g = compute_gaps(load_words(edit_dir, s))
        gaps_by_src[s.name] = g
    (edit_dir / "gaps.json").write_text(json.dumps(gaps_by_src, indent=2))

    # 7. two auto visual samples per source — skipped when running under an
    # OpenClaw agent (Jensen/Beast run text-only models and cannot view an
    # image anywhere; the frames are wasted ffmpeg work + an "I couldn't view
    # this" line every run). Claude Code (no notify profile) still gets them.
    if not running_as_agent(state):
        for s in sources:
            dur = inv[s.name]["duration_s"] or 0.0
            timeline_sample(s, 0.0, min(10.0, dur), edit_dir / "verify" / f"{s.stem}_sample_head.png")
            mid = dur / 2.0
            timeline_sample(s, max(0.0, mid - 5.0), min(dur, mid + 5.0),
                            edit_dir / "verify" / f"{s.stem}_sample_mid.png")

    # 8. briefing
    write_briefing(state, inv, sources, gaps_by_src)

    state["phase"] = "STRATEGY"
    state["gates"]["ingest_briefing"]["done"] = True
    save_state(state)
    print(INGEST_DONE_MSG.format(edit=edit_dir))
    sys.exit(0)


# ────────────────────────── phase: STRATEGY ──────────────────────────

STRATEGY_REMINDER = """\
Phase STRATEGY. The briefing is ready at `{edit}/briefing.md`.

YOU OWE: `{edit}/strategy.md` — 4–8 sentences (shape, cut direction, length
target, grade, subtitle style) plus a `## User confirmation` section quoting
the user's plain-English approval. Then run:

    pipeline.py {edit} --confirm-strategy
"""


def phase_strategy(state: dict, confirm: bool) -> None:
    edit_dir = Path(state["edit_dir"])
    if not confirm:
        print(STRATEGY_REMINDER.format(edit=edit_dir))
        sys.exit(0)

    sp = edit_dir / "strategy.md"
    if not sp.exists():
        print(f"REFUSED: {sp} does not exist. Write the strategy first (see "
              f"`pipeline.py {edit_dir} --status`).")
        sys.exit(1)
    text = sp.read_text()
    if len(text.strip()) < 200:
        print(f"REFUSED: {sp} is only {len(text.strip())} chars — too thin to be a "
              f"real strategy. Write 4–8 substantive sentences plus the confirmation.")
        sys.exit(1)
    if "## user confirmation" not in text.lower():
        print(f"REFUSED: {sp} has no `## User confirmation` section. Add it, quoting "
              f"the user approving the plan in plain English. The pipeline will not "
              f"proceed to cutting without recorded confirmation (SKILL.md Hard Rule 11).")
        sys.exit(1)

    state["phase"] = "EDL"
    state["gates"]["strategy_confirmed"]["done"] = True
    save_state(state)
    print(f"Strategy confirmed. Phase EDL.\n\n"
          f"YOU OWE: `{edit_dir}/edl.json` per SKILL.md 'EDL format'. Every range needs "
          f"source/start/end/beat/quote/reason. To strip filler words, author COARSE "
          f"structural ranges (keep the content you want; do NOT try to cut individual "
          f"um/uh yourself) and set `\"strip_fillers\": true` — the pipeline removes every "
          f"filler inside your ranges from check_fillers.py's timestamps. Use `omissions` "
          f"for deliberate CONTENT you drop. Then run:\n\n    pipeline.py {edit_dir}")
    sys.exit(0)


# ──────────────────────────── phase: EDL ─────────────────────────────

def validate_edl_schema(edl_path: Path, edit_dir: Path, state: dict) -> list[str]:
    errs: list[str] = []
    try:
        edl = json.loads(edl_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return [f"could not parse edl.json: {e}"]

    if "version" not in edl:
        errs.append("missing 'version'")
    if "strip_fillers" in edl and not isinstance(edl["strip_fillers"], bool):
        errs.append("'strip_fillers' must be true or false")
    srcmap = edl.get("sources")
    if not isinstance(srcmap, dict) or not srcmap:
        errs.append("'sources' must be a non-empty {name: abs_path} map")
        srcmap = {}
    inv_path = edit_dir / "inventory.json"
    inv = json.loads(inv_path.read_text()) if inv_path.exists() else {}
    dur_by_name = {}
    for name, p in srcmap.items():
        pp = Path(p)
        if not pp.is_absolute():
            errs.append(f"sources[{name!r}] is not an absolute path: {p}")
        elif not pp.exists():
            errs.append(f"sources[{name!r}] does not exist: {p}")
        m = inv.get(pp.name) or inv.get(name)
        if m and m.get("duration_s"):
            dur_by_name[name] = m["duration_s"]

    oms = edl.get("omissions", [])
    if not isinstance(oms, list):
        errs.append("'omissions' must be a list of {source,start,end,reason} if present")
    else:
        for i, o in enumerate(oms):
            for k in ("source", "start", "end", "reason"):
                if k not in o:
                    errs.append(f"omissions[{i}] missing {k!r}")
            if isinstance(o.get("start"), (int, float)) and isinstance(o.get("end"), (int, float)) \
                    and o["start"] >= o["end"]:
                errs.append(f"omissions[{i}] start >= end")

    ranges = edl.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        errs.append("'ranges' must be a non-empty list")
        ranges = []
    for i, r in enumerate(ranges):
        tag = f"ranges[{i}]"
        # required = the fields that affect the cut; beat/quote are
        # documentation and optional (a token-limited model routinely omits
        # them and the render doesn't need them).
        for k in ("source", "start", "end", "reason"):
            if k not in r:
                errs.append(f"{tag} missing {k!r}")
        if "source" in r and srcmap and r["source"] not in srcmap:
            errs.append(f"{tag} source {r['source']!r} not in 'sources' map")
        s_, e_ = r.get("start"), r.get("end")
        if not isinstance(s_, (int, float)) or not isinstance(e_, (int, float)):
            errs.append(f"{tag} start/end must be numbers")
        elif s_ >= e_:
            errs.append(f"{tag} start ({s_}) >= end ({e_})")
        elif s_ < 0:
            errs.append(f"{tag} start is negative")
        elif r.get("source") in dur_by_name and e_ > dur_by_name[r["source"]] + 0.05:
            errs.append(f"{tag} end ({e_}) exceeds {r['source']} duration "
                        f"({dur_by_name[r['source']]}s)")
    return errs


def phase_edl(state: dict) -> None:
    edit_dir = Path(state["edit_dir"])
    edl_path = edit_dir / "edl.json"
    if not edl_path.exists():
        print(f"WAITING: no {edl_path} yet. Produce it per SKILL.md 'EDL format', "
              f"then re-run `pipeline.py {edit_dir}`.")
        sys.exit(0)

    errs = validate_edl_schema(edl_path, edit_dir, state)
    if errs:
        print("EDL SCHEMA INVALID:")
        for e in errs:
            print(f"  - {e}")
        print(f"\nFix {edl_path} and re-run `pipeline.py {edit_dir}`.")
        sys.exit(1)

    # Cut-vs-transcript check via render.py's validator (which already suppresses
    # any removed-speech span the EDL declares in `omissions` with a reason).
    # So anything that comes back is a real problem:
    #   clip          -> a boundary lands mid-word; move it to a silence
    #   speech_gap    -> speech is being cut that the EDL does NOT declare;
    #                    add it to `omissions` with a reason, or change ranges
    #   no_transcript -> a source wasn't transcribed
    # `render.py --force` is never used here — declaring the omission is the
    # deterministic replacement (so a cut can't be made in the false belief a
    # span was silent — the 2026-09-07 incident).
    r = run_helper("render.py", str(edl_path), "--validate-only", "--json", check=False)
    try:
        warns = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        print("EDL gate: could not parse validator output:\n" + r.stdout + r.stderr)
        sys.exit(2)

    if warns:
        clips = [w for w in warns if w["kind"] == "clip"]
        gaps = [w for w in warns if w["kind"] == "speech_gap"]
        others = [w for w in warns if w["kind"] not in ("clip", "speech_gap")]
        print("EDL NOT ACCEPTED:")
        for w in warns:
            print(f"  - {w['message']}")
        tips = []
        if clips:
            tips.append("move the mid-word boundaries to a real silence (see "
                        "briefing.md's gap table)")
        if gaps:
            tips.append('for each span of speech you are cutting on purpose, add '
                        '{"source","start","end","reason"} to edl.json `omissions` '
                        "covering it — or adjust the ranges so no speech is dropped there")
        if others:
            tips.append("transcribe any missing source")
        print("\n" + ". ".join(t.capitalize() for t in tips) +
              f".\nThen re-run `pipeline.py {edit_dir}`. Do NOT use render.py --force.")
        sys.exit(1)

    print("CUT VALIDATION OK — boundaries land in silence; all removed speech is declared.")

    # strip_fillers: the author's ranges are coarse; expand them into the
    # concrete cut list (fillers removed from check_fillers.py timestamps).
    # edl.effective.json is what RENDER / self-eval actually use; edl.json
    # stays as the human-authored intent.
    edl_full = json.loads(edl_path.read_text())
    eff_path = edit_dir / "edl.effective.json"
    exp = run_helper("filler_cuts.py", str(edl_path), "--edit-dir", str(edit_dir),
                     "-o", str(eff_path), check=False)
    if exp.returncode != 0:
        print(f"filler-strip expansion failed:\n{exp.stdout}{exp.stderr}")
        sys.exit(2)
    if edl_full.get("strip_fillers"):
        print(exp.stdout.strip())
        r2 = run_helper("render.py", str(eff_path), "--validate-only", "--json", check=False)
        w2 = json.loads(r2.stdout or "[]")
        if w2:
            print("EXPANDED EDL FAILED VALIDATION (the filler-strip produced a bad cut):")
            for w in w2:
                print(f"  - {w['message']}")
            print(f"Your coarse ranges likely have a boundary that doesn't land in a "
                  f"gap. Fix edl.json and re-run `pipeline.py {edit_dir}`.")
            sys.exit(1)

    state["phase"] = "RENDER"
    state["gates"]["edl_validated"]["done"] = True
    save_state(state)
    print(f"EDL validated (schema + cut check). Phase RENDER.\n"
          f"Run `pipeline.py {edit_dir}` again to start the render.")
    sys.exit(0)


# ─────────────────────────── phase: RENDER ───────────────────────────

def _render_edl_path(edit_dir: Path) -> Path:
    """The EDL that actually renders: edl.effective.json (filler-stripped /
    normalized) if present, else the authored edl.json."""
    eff = edit_dir / "edl.effective.json"
    return eff if eff.exists() else edit_dir / "edl.json"


def phase_render(state: dict) -> None:
    edit_dir = Path(state["edit_dir"])
    edl_path = _render_edl_path(edit_dir)
    out_path = edit_dir / "final.mp4"
    common = [str(edl_path), "-o", str(out_path)]

    # Check status BEFORE launching -- render.py's launch path only skips a
    # *currently running* job, so a completed job would be re-rendered. Only
    # spawn a worker if there is no job (or a failed one) to resume.
    st = run_helper("render.py", *common, "--status", check=False)
    line = st.stdout.strip()
    if line.startswith(("NOT_FOUND:", "FAILED:")) and not state["gates"]["render_done"]["done"]:
        run_helper("render.py", *common, "--build-subtitles", *notify_args(state), check=False)
        st = run_helper("render.py", *common, "--status", check=False)
        line = st.stdout.strip()

    if line.startswith("DONE:"):
        state["phase"] = "SELF_EVAL"
        state["gates"]["render_done"] = {"done": True, "output": str(out_path)}
        save_state(state)
        print(f"Render complete: {out_path}\n"
              f"Run `pipeline.py {edit_dir}` again to generate the self-eval frames.")
        sys.exit(0)
    if line.startswith("FAILED:"):
        state["gates"]["render_done"]["done"] = False
        save_state(state)
        print(f"RENDER FAILED: {line}\n"
              f"Inspect `{edit_dir}/jobs/*.log`. If you changed edl.json to fix a cut, "
              f"run `pipeline.py {edit_dir} --restage edl` to re-validate first. "
              f"Otherwise re-run `pipeline.py {edit_dir}` to retry the render.")
        sys.exit(1)

    print(f"WAITING: render in progress ({line}). You'll be notified when it "
          f"finishes (or re-run `pipeline.py {edit_dir}`).")
    sys.exit(0)


# ────────────────────────── phase: SELF_EVAL ─────────────────────────

SELF_EVAL_CHECKLIST = """\
Phase SELF_EVAL. The pipeline extracted timeline frames of the RENDERED output
at every cut boundary (±1.5s) plus head/tail/mid samples, in `{edit}/eval/`.
{duration_line}

YOU OWE: inspect every `{edit}/eval/*.png` for —
  - visual discontinuity / flash / jump at a cut
  - a waveform spike at a boundary (audio pop past the 30 ms fade)
  - a subtitle hidden behind an overlay
  - an overlay showing the wrong frames / misaligned
  - the intended background swap / grade actually took effect
  - grade consistency, subtitle readability, overall coherence
Write your findings — one line per frame, what you checked and saw — to
`{edit}/eval/eval_review.md`. `--eval-verdict pass` is REFUSED without it.

All clear   →  write eval/eval_review.md, then  pipeline.py {edit} --eval-verdict pass
Any problem →  fix the EDL (or render settings), then
              pipeline.py {edit} --eval-verdict fail --restage edl   (or: --restage render)
Hard cap: 3 fail cycles, after which remaining issues must be surfaced to the
user rather than looped on.
{agent_note}"""

_AGENT_SELF_EVAL_NOTE = """
NOTE: this pipeline was started by an agent that cannot view images. You cannot
produce eval/eval_review.md yourself. Either (a) have a human or Claude Code
inspect eval/*.png and run `--eval-verdict pass` after writing the review, or
(b) run `describe_frames.py` once it exists to generate the review. Do NOT run
`--eval-verdict pass` blind — it will be refused.
"""


def _output_cut_boundaries(edl: dict) -> list[float]:
    t = 0.0
    bounds = [0.0]
    for r in edl.get("ranges", []):
        t += float(r["end"]) - float(r["start"])
        bounds.append(round(t, 3))
    return bounds


def generate_eval_frames(state: dict) -> str:
    edit_dir = Path(state["edit_dir"])
    out_path = edit_dir / "final.mp4"
    eval_dir = edit_dir / "eval"
    eval_dir.mkdir(parents=True, exist_ok=True)
    edl = json.loads(_render_edl_path(edit_dir).read_text())  # the real cut list

    total = _output_cut_boundaries(edl)[-1]
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(out_path)],
        capture_output=True, text=True,
    )
    actual = None
    try:
        actual = round(float(probe.stdout.strip()), 2)
    except (ValueError, AttributeError):
        pass

    boundaries = _output_cut_boundaries(edl)
    for i, b in enumerate(boundaries):
        if b <= 0.05 or b >= total - 0.05:
            continue
        timeline_sample(out_path, max(0.0, b - 1.5), min(total, b + 1.5),
                        eval_dir / f"cut_{i:02d}_{b:.2f}s.png")
    timeline_sample(out_path, 0.0, min(2.0, total), eval_dir / "head.png")
    timeline_sample(out_path, max(0.0, total - 2.0), total, eval_dir / "tail.png")
    for j in (0.25, 0.5, 0.75):
        m = total * j
        timeline_sample(out_path, max(0.0, m - 1.0), min(total, m + 1.0),
                        eval_dir / f"mid_{int(j * 100):02d}.png")

    expected = edl.get("total_duration_s") or round(total, 2)
    if actual is None:
        return f"Duration check: could not probe output. EDL expects ~{expected}s."
    delta = abs(actual - expected)
    flag = "OK" if delta <= 0.5 else f"MISMATCH (Δ{delta:.2f}s — investigate)"
    return f"Duration check: output {actual}s vs EDL {expected}s — {flag}."


def phase_self_eval(state: dict, verdict: str | None, restage: str | None) -> None:
    edit_dir = Path(state["edit_dir"])
    se = state["gates"]["self_eval"]

    review = edit_dir / "eval" / "eval_review.md"

    if verdict is None:
        dline = generate_eval_frames(state)
        note = _AGENT_SELF_EVAL_NOTE if running_as_agent(state) else ""
        print(SELF_EVAL_CHECKLIST.format(edit=edit_dir, duration_line=dline, agent_note=note))
        sys.exit(0)

    if verdict == "pass":
        # A pass must be backed by a written frame-by-frame review. An agent
        # that can't see the frames cannot produce this; a human, Claude Code,
        # or describe_frames.py must. Blocks the blind rubber-stamp that let a
        # visibly-broken render reach DONE on 2026-09-08.
        txt = review.read_text().strip() if review.exists() else ""
        if len(txt) < 100:
            print(f"REFUSED: --eval-verdict pass requires a real review at "
                  f"{review} (found {'nothing' if not txt else str(len(txt)) + ' chars'}). "
                  f"Inspect every eval/*.png and write what you checked and saw, one "
                  f"line per frame, then retry."
                  + (_AGENT_SELF_EVAL_NOTE if running_as_agent(state) else ""))
            sys.exit(1)
        se["verdict"] = "pass"
        state["phase"] = "DONE"
        save_state(state)
        _finish(state)
        sys.exit(0)

    # verdict == "fail"
    se["passes"] += 1
    n = se["passes"]
    if n >= 3:
        se["verdict"] = "capped_with_issues"
        state["phase"] = "DONE"
        save_state(state)
        print(f"Self-eval hit the 3-cycle cap with issues still present. Do NOT "
              f"present this as finished — tell the user exactly what still fails.")
        _finish(state)
        sys.exit(0)

    target = restage or "edl"
    if target == "render":
        state["phase"] = "RENDER"
        state["gates"]["render_done"]["done"] = False
    else:
        state["phase"] = "EDL"
        state["gates"]["edl_validated"]["done"] = False
        state["gates"]["render_done"]["done"] = False
    save_state(state)
    print(f"Self-eval fail #{n} recorded. Back to {state['phase']} phase. "
          f"Fix the {'render settings' if target == 'render' else 'EDL'}, then "
          f"continue with `pipeline.py {edit_dir}`.")
    sys.exit(0)


# ─────────────────────────── phase: DONE ────────────────────────────

def _finish(state: dict) -> None:
    edit_dir = Path(state["edit_dir"])
    proj = edit_dir / "project.md"
    n = 1
    if proj.exists():
        n = proj.read_text().count("\n## Session ") + 1
    strat = (edit_dir / "strategy.md").read_text().strip() if (edit_dir / "strategy.md").exists() else "(none)"
    # author intent (coarse), not the filler-expanded list
    edl = json.loads((edit_dir / "edl.json").read_text()) if (edit_dir / "edl.json").exists() else {"ranges": []}
    decisions = "\n".join(
        f"  - [{r.get('beat', '?')}] {r.get('source', '?')} {r.get('start')}–{r.get('end')}: {r.get('reason', '')}"
        for r in edl.get("ranges", [])
    )
    eff_p = edit_dir / "edl.effective.json"
    filler_note = ""
    if edl.get("strip_fillers") and eff_p.exists():
        eff = json.loads(eff_p.read_text())
        filler_note = (f"\n**Filler removal:** strip_fillers on — {len(edl.get('ranges', []))} "
                       f"coarse range(s) rendered as {len(eff.get('ranges', []))} "
                       f"effective range(s) (see edl.effective.json).\n")
    verdict = state["gates"]["self_eval"].get("verdict")
    block = (
        f"\n## Session {n} — {time.strftime('%Y-%m-%d')}\n\n"
        f"**Self-eval verdict:** {verdict}\n\n"
        f"**Strategy:**\n\n{strat}\n\n"
        f"**Cut decisions:**\n{decisions or '  (none)'}\n{filler_note}"
    )
    with proj.open("a") as f:
        f.write(block)
    print(f"Pipeline complete. Output: {edit_dir / 'final.mp4'}. "
          f"Session appended to {proj}.")


def phase_done(state: dict) -> None:
    print(f"Pipeline is DONE. Output: {Path(state['edit_dir']) / 'final.mp4'}. "
          f"Self-eval verdict: {state['gates']['self_eval'].get('verdict')}. "
          f"Nothing to advance. Re-`init` to start a new edit.")
    sys.exit(0)


# ───────────────────────────── restage ──────────────────────────────

def do_restage(state: dict, target: str) -> None:
    edit_dir = Path(state["edit_dir"])
    if target in ("strategy", "edl"):
        (edit_dir / "edl.effective.json").unlink(missing_ok=True)  # stale derived cut list
    if target == "strategy":
        state["phase"] = "STRATEGY"
        for g in ("strategy_confirmed", "edl_validated", "render_done"):
            state["gates"][g]["done"] = False
        save_state(state)
        print(f"Restaged to STRATEGY. Rewrite {state['edit_dir']}/strategy.md "
              f"(with a real `## User confirmation`), then "
              f"`pipeline.py {state['edit_dir']} --confirm-strategy`.")
        sys.exit(0)
    if target == "edl":
        state["phase"] = "EDL"
        state["gates"]["edl_validated"]["done"] = False
        state["gates"]["render_done"]["done"] = False
    elif target == "render":
        if not state["gates"]["edl_validated"]["done"]:
            print("REFUSED: cannot --restage render — the EDL is not currently "
                  "validated. Use --restage edl.")
            sys.exit(1)
        state["phase"] = "RENDER"
        state["gates"]["render_done"]["done"] = False
    else:
        print(f"REFUSED: unknown --restage target {target!r} (use strategy|edl|render)")
        sys.exit(1)
    save_state(state)
    print(f"Restaged to {state['phase']}. Run `pipeline.py {state['edit_dir']}` to continue.")
    sys.exit(0)


# ───────────────────────────── status ───────────────────────────────

def print_status(state: dict) -> None:
    edit_dir = state["edit_dir"]
    print(f"edit_dir: {edit_dir}")
    print(f"phase:    {state['phase']}")
    print(f"sources:  {', '.join(Path(s).name for s in state['sources'])}")
    n = state.get("notify") or {}
    print(f"notify:   session={n.get('session_key')!r} profile={n.get('profile')!r}")
    print("gates:")
    for k, v in state["gates"].items():
        print(f"  {k}: {json.dumps(v)}")
    owed = {
        "INGEST": "run `pipeline.py <edit-dir>` (it does ffprobe/transcribe/pack/"
                  "fillers/gaps, then hands you briefing.md).",
        "STRATEGY": "write strategy.md (+ `## User confirmation`), then "
                    "`pipeline.py <edit-dir> --confirm-strategy`.",
        "EDL": "write edl.json per SKILL.md, then `pipeline.py <edit-dir>`.",
        "RENDER": "run `pipeline.py <edit-dir>` to render (nothing to author).",
        "SELF_EVAL": "run `pipeline.py <edit-dir>` for eval frames, inspect them, "
                     "then `--eval-verdict pass|fail`.",
        "DONE": "nothing — the edit is complete.",
    }
    print(f"\nnext:     {owed[state['phase']]}")


# ────────────────────────────── main ────────────────────────────────

def do_init(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(prog="pipeline.py init")
    ap.add_argument("videos", nargs="+", type=Path)
    # --edit-dir is accepted for compatibility but IGNORED unless it already
    # equals <first-source-parent>/edit. The edit dir is always beside the
    # source video (agents kept inventing scattered locations -- test-beast/edit,
    # test-beast/IMG_4328-edited, ~/Desktop/video-use-edits/test-beast on three
    # runs). The script owns the path; the agent uses whatever it prints.
    ap.add_argument("--edit-dir", type=Path, default=None)
    ap.add_argument("--notify-session", type=str, default=None)
    ap.add_argument("--notify-profile", type=str, default=None)
    args = ap.parse_args(argv)

    sources = []
    for v in args.videos:
        vp = v.resolve()
        if not vp.exists():
            sys.exit(f"source not found: {vp}")
        sources.append(str(vp))

    canonical = Path(sources[0]).parent / "edit"
    if args.edit_dir is not None and args.edit_dir.resolve() != canonical:
        print(f"note: ignoring --edit-dir {args.edit_dir} — the edit folder always "
              f"lives beside the source video. Using {canonical}.")
    edit_dir = canonical
    edit_dir.mkdir(parents=True, exist_ok=True)

    existing = state_path(edit_dir)
    if existing.exists():
        old = json.loads(existing.read_text())
        sys.exit(f"REFUSED: {existing} already exists (phase {old.get('phase')}). "
                 f"Delete {edit_dir} to start over, or just run `pipeline.py {edit_dir}`.")

    state = {
        "version": 1,
        "phase": "INGEST",
        "sources": sources,
        "edit_dir": str(edit_dir),
        "created_at": time.time(),
        "updated_at": time.time(),
        "notify": {"session_key": args.notify_session, "profile": args.notify_profile},
        "gates": _fresh_gates(),
    }
    save_state(state)
    print(f"Pipeline initialized. Edit folder: {edit_dir}\n"
          f"(phase INGEST, {len(sources)} source(s))\n\n"
          f"Use this exact path for every following call:\n"
          f"    pipeline.py {edit_dir}")


ADVANCE = {
    "INGEST": lambda st: phase_ingest(st),
    "STRATEGY": lambda st: phase_strategy(st, confirm=False),
    "EDL": lambda st: phase_edl(st),
    "RENDER": lambda st: phase_render(st),
    "SELF_EVAL": lambda st: phase_self_eval(st, None, None),
    "DONE": lambda st: phase_done(st),
}


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        sys.exit(__doc__)
    if argv[0] == "init":
        do_init(argv[1:])
        return

    ap = argparse.ArgumentParser(prog="pipeline.py")
    ap.add_argument("edit_dir", type=Path)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--confirm-strategy", action="store_true")
    ap.add_argument("--eval-verdict", choices=["pass", "fail"], default=None)
    ap.add_argument("--restage", choices=["strategy", "edl", "render"], default=None)
    args = ap.parse_args(argv)

    edit_dir = args.edit_dir.resolve()
    state = load_state(edit_dir)
    state["edit_dir"] = str(edit_dir)  # tolerate a moved dir

    if args.status:
        print_status(state)
        return
    if args.confirm_strategy:
        if state["phase"] != "STRATEGY":
            sys.exit(f"REFUSED: --confirm-strategy only valid in STRATEGY phase "
                     f"(currently {state['phase']}).")
        phase_strategy(state, confirm=True)
        return
    if args.eval_verdict:
        if state["phase"] != "SELF_EVAL":
            sys.exit(f"REFUSED: --eval-verdict only valid in SELF_EVAL phase "
                     f"(currently {state['phase']}).")
        phase_self_eval(state, args.eval_verdict, args.restage)
        return
    if args.restage:
        do_restage(state, args.restage)
        return

    ADVANCE[state["phase"]](state)


if __name__ == "__main__":
    main()
