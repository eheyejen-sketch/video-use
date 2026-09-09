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
                 [--notify-session KEY] [--notify-profile PROFILE] [--no-watch]
    pipeline.py <edit-dir>                     # run/advance the current phase
    pipeline.py <edit-dir> --status            # report only, never mutates
    pipeline.py <edit-dir> --confirm-strategy  # STRATEGY checkpoint (manual runs
                 #   only; an agent-started run auto-advances once strategy.md exists)
    pipeline.py <edit-dir> --eval-verdict pass|fail [--restage edl|render]
                 #   manual runs only; an agent-started run auto-QCs via qc_render.py
    pipeline.py <edit-dir> --restage strategy|edl|render|self_eval  # manual step-back
    pipeline.py <edit-dir> --watch            # detached auto-advance loop
                 [--watch-max-seconds N]      #   (auto-started by an agent init)

The --watch loop owns the mechanical phase transitions an idle agent keeps
missing (INGEST/RENDER/STRATEGY advance themselves once the agent's artifact
is on disk; EDL gets one targeted `system event` nudge; SELF_EVAL runs
`qc_render.py` for a deterministic render QC and finishes the run). An agent-started
run needs NO human gate -- it goes init -> DONE and pings the user with the
result. A retry storm, an unresponsive agent, or the wall-clock cap stops the
watcher and pings the session. See `plans/AUTO-ADVANCE-DESIGN.md`.

Exit codes: 0 = phase advanced / waiting on a background job / info printed
(the agent should read the message and act). 1 = a gate REFUSED the agent's
artifact (fix it and re-run). 2 = hard error (missing state, missing source,
transcription failed).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HELPERS = Path(__file__).resolve().parent

# Run under the project venv no matter how we were invoked. Some agents' `exec`
# does not honour this file's shebang and starts it with the macOS system
# python 3.9 (no numpy/PIL/etc) -- then every `run_helper` child inherits that
# via sys.executable and timeline_view.py / render.py crash on missing deps.
# Re-exec once under .venv/bin/python3 so the whole pipeline is consistent.
_VENV_PY = HELPERS.parent / ".venv" / "bin" / "python3"
if _VENV_PY.exists() and not os.environ.get("_VIDEO_USE_VENV_REEXEC"):
    try:
        if Path(sys.executable).resolve() != _VENV_PY.resolve():
            os.environ["_VIDEO_USE_VENV_REEXEC"] = "1"
            os.execv(str(_VENV_PY), [str(_VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]])
    except OSError:
        pass  # fall through and hope the current interpreter has what's needed
try:
    from _job_lock import _pid_alive, send_system_event
except ImportError:  # pragma: no cover - _job_lock is a sibling; this is belt-and-braces
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def send_system_event(*_a, **_k) -> bool:
        return False

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

YOU OWE: read `{edit}/briefing.md` in full. Then write your proposed strategy
(4–8 sentences: shape, cut direction, length target, grade, subtitle style) to
`{edit}/strategy.md`, and post the same plan to the user for visibility.

There is NO confirmation step for an agent-started run — once strategy.md is on
disk the pipeline advances to EDL on its own. The user reviews the finished
video, not the plan. (A Claude-Code-driven run uses
`pipeline.py {edit} --confirm-strategy` as a manual checkpoint.)
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
target, grade, subtitle style). Post the same plan to the user so they can
weigh in. There is NO confirmation step for an agent-started run — the pipeline
advances to EDL on its own once strategy.md is written; the user reviews the
finished video, not the plan.
"""

_EDL_OWED = (
    "YOU OWE: `{edit}/edl.json` per SKILL.md 'EDL format'. Author COARSE structural "
    "`ranges` (source/start/end/reason each — pick the content to KEEP), set "
    "`\"strip_fillers\": true` to drop um/uh, and put each deliberately-dropped "
    "content span in `omissions` (in the GAP between ranges — omissions annotate, "
    "they do not cut). `background` must be an ABSOLUTE path. Then run:\n"
    "    pipeline.py {edit}"
)


def phase_strategy(state: dict, confirm: bool) -> None:
    edit_dir = Path(state["edit_dir"])
    sp = edit_dir / "strategy.md"
    substantive = sp.exists() and len(sp.read_text().strip()) >= 200

    if not confirm:
        # bare `pipeline.py <dir>` at STRATEGY (agent runs + the watcher).
        if not substantive:
            print(STRATEGY_REMINDER.format(edit=edit_dir))
            sys.exit(0)
        if running_as_agent(state):
            state["phase"] = "EDL"
            state["gates"]["strategy_confirmed"] = {"done": True, "confirmed_by": "auto (agent run)"}
            save_state(state)
            print("strategy.md recorded — agent run, no confirmation gate. Phase EDL.\n"
                  "(Post your plan to the user for visibility; they review the final "
                  "video, not the plan.)\n\n" + _EDL_OWED.format(edit=edit_dir))
            sys.exit(0)
        # non-agent (Claude Code drove init): keep a manual checkpoint.
        print(f"strategy.md is ready. When the user has approved the plan, run:\n"
              f"    pipeline.py {edit_dir} --confirm-strategy")
        sys.exit(0)

    # --confirm-strategy (manual / Claude Code path only)
    if not sp.exists():
        print(f"REFUSED: {sp} does not exist. Write the strategy first.")
        sys.exit(1)
    if not substantive:
        print(f"REFUSED: {sp} is only {len(sp.read_text().strip())} chars — too thin. "
              f"Write 4–8 substantive sentences.")
        sys.exit(1)

    state["phase"] = "EDL"
    state["gates"]["strategy_confirmed"] = {"done": True, "confirmed_by": "user"}
    save_state(state)
    print("Strategy confirmed. Phase EDL.\n\n" + _EDL_OWED.format(edit=edit_dir))
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


def _total_source_seconds(edit_dir: Path) -> float:
    inv_p = edit_dir / "inventory.json"
    if not inv_p.exists():
        return 0.0
    try:
        inv = json.loads(inv_p.read_text())
    except (json.JSONDecodeError, OSError):
        return 0.0
    return sum(float(m.get("duration_s") or 0.0) for m in inv.values())


def _filler_count(edit_dir: Path) -> int:
    fr = edit_dir / "filler_report.txt"
    if not fr.exists():
        return 0
    n = 0
    for line in fr.read_text().splitlines():
        line = line.strip()
        if line.startswith("FILLERS_FOUND:"):
            try:
                n += int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    return n


def edl_intent_warnings(edl_path: Path, edit_dir: Path) -> list[str]:
    """Refusal reasons for an EDL that is *structurally* valid but does not
    express the edit the author thinks it does. Both were real Beast mistakes
    on 2026-09-08:
      - an `omissions` entry that lies inside the kept ranges cuts NOTHING
        (omissions annotate removed speech for the validator; they are not a
        cut instruction). The editor believes that span is gone; it renders.
      - a single ~full-source range + `strip_fillers` is not an edit. Filler
        strip only removes literal um/uh tokens, not dead space, rambling,
        "you know", or repeated content."""
    try:
        edl = json.loads(edl_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    ranges = [(float(r["start"]), float(r["end"]))
              for r in edl.get("ranges", [])
              if isinstance(r.get("start"), (int, float))
              and isinstance(r.get("end"), (int, float)) and r["end"] > r["start"]]
    out: list[str] = []

    def covered(s: float, e: float) -> float:
        c = 0.0
        for rs, re_ in ranges:
            lo, hi = max(s, rs), min(e, re_)
            if hi > lo:
                c += hi - lo
        return c

    for i, o in enumerate(edl.get("omissions", [])):
        try:
            s, e = float(o["start"]), float(o["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if e <= s:
            continue
        if covered(s, e) >= (e - s) - 0.15:
            out.append(
                f"omissions[{i}] {s:.2f}-{e:.2f}s is fully inside your kept ranges — "
                f"it removes NOTHING. `omissions` only annotate removed speech for the "
                f"cut validator; they do not cut. To drop that span, END a range "
                f"before {s:.2f}s and START the next one after {e:.2f}s.")

    total_src = _total_source_seconds(edit_dir)
    kept = sum(e - s for s, e in ranges)
    if (edl.get("strip_fillers") and total_src > 0
            and kept >= 0.95 * total_src and _filler_count(edit_dir) > 5):
        out.append(
            f"your ranges keep {kept / total_src * 100:.0f}% of the source "
            f"({kept:.1f}s of {total_src:.1f}s) and lean on `strip_fillers` for the "
            f"edit. strip_fillers only removes literal um/uh tokens "
            f"(~{_filler_count(edit_dir)} of them) — not dead space, rambling, "
            f'"you know", or repeated content. Select tighter content ranges (the '
            f"good runs kept ~60%), or drop strip_fillers if you truly want the "
            f"near-complete source.")
    return out


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

    intent = edl_intent_warnings(edl_path, edit_dir)
    if intent:
        print("EDL NOT ACCEPTED — it does not express the edit you intend:")
        for w in intent:
            print(f"  - {w}")
        print(f"\nFix {edl_path} and re-run `pipeline.py {edit_dir}`. Do NOT use "
              f"render.py --force.")
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
        assets = [w for w in warns if w["kind"] in ("background", "subtitles")]
        no_tr = [w for w in warns if w["kind"] == "no_transcript"]
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
        if assets:
            tips.append("fix the asset path — use an ABSOLUTE path in `background` "
                        "(or place the file beside the source video), the swap is "
                        "silently skipped otherwise")
        if no_tr:
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

Inspect every `{edit}/eval/*.png` for —
  - visual discontinuity / flash / jump at a cut
  - a waveform spike at a boundary (audio pop past the 30 ms fade)
  - a subtitle clipped at an edge or hidden behind an overlay
  - the intended background swap / grade actually took effect
  - grade consistency, subtitle readability, overall coherence
Write your findings — one line per frame — to `{edit}/eval/eval_review.md`.

All clear   →  pipeline.py {edit} --eval-verdict pass
Any problem →  pipeline.py {edit} --eval-verdict fail --restage edl   (or: --restage render)
Hard cap: 3 fail cycles.

(An agent-started run never reaches this text — it runs qc_render.py for an
automated render QC and finishes, pinging the user with the result.)"""


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


def _run_qc(state: dict) -> tuple[str, list[str]]:
    """Deterministic render QC (qc_render.py -- ~0.2s, no vision model). Returns
    (verdict 'PASS'|'ISSUES'|'ERROR', issues). Writes eval/eval_review.md."""
    edit_dir = Path(state["edit_dir"])
    r = run_helper("qc_render.py", str(edit_dir), "--json",
                   "-o", str(edit_dir / "eval" / "eval_review.md"), check=False)
    try:
        d = json.loads((r.stdout or "").strip().splitlines()[-1])
        return str(d.get("verdict", "ERROR")).upper(), list(d.get("issues", []))
    except (json.JSONDecodeError, IndexError):
        return "ERROR", [f"qc_render.py produced no parseable verdict: "
                         f"{(r.stdout + r.stderr).strip()[:200]}"]


def _notify_user(state: dict, message: str) -> None:
    n = state.get("notify") or {}
    if n.get("session_key"):
        try:
            send_system_event(n["session_key"], n.get("profile"), message)
        except Exception:  # noqa: BLE001
            pass


def _done_message(state: dict, verdict: str, issues: list[str]) -> str:
    edit_dir = Path(state["edit_dir"])
    final = edit_dir / "final.mp4"
    dur = "?"
    try:
        p = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(final)], capture_output=True, text=True)
        dur = f"{float(p.stdout.strip()):.1f}s"
    except (ValueError, OSError):
        pass
    try:
        edl = json.loads((edit_dir / "edl.json").read_text())
    except (json.JSONDecodeError, OSError):
        edl = {}
    bits = [f"{len(edl.get('ranges', []))} range(s)"]
    if edl.get("strip_fillers"):
        bits.append("fillers stripped")
    if edl.get("background"):
        bits.append("background swapped")
    bits.append("captions burned")
    summary = f"{final} ({dur}) — {', '.join(bits)}"
    if verdict == "PASS":
        return (f"✅ video-use edit ready: {summary}\n"
                f"Render QC: clean. Review the video and reply here to request a re-cut.")
    if verdict == "ISSUES":
        il = "\n".join(f"  • {x}" for x in issues) or "  • (see eval/eval_review.md)"
        return (f"⚠️ video-use edit ready: {summary}\nRender QC flagged:\n{il}\n"
                f"Reply 'recut <what to change>' to fix, or 'ship it' to accept as-is. "
                f"Full review: {edit_dir}/eval/eval_review.md")
    return (f"video-use edit ready: {summary}\nRender QC could NOT run — please "
            f"eyeball {final} and {edit_dir}/eval/*.png. Reply to request a re-cut.")


def phase_self_eval(state: dict, verdict: str | None, restage: str | None,
                    reviewer: str | None = None) -> None:
    edit_dir = Path(state["edit_dir"])
    se = state["gates"]["self_eval"]
    review = edit_dir / "eval" / "eval_review.md"

    if verdict is None:
        dline = generate_eval_frames(state)
        if not running_as_agent(state):
            print(SELF_EVAL_CHECKLIST.format(edit=edit_dir, duration_line=dline))
            sys.exit(0)
        # agent-started run -> deterministic render QC, then finish + ping the user.
        v, issues = _run_qc(state)
        se["verdict"] = {"PASS": "pass", "ISSUES": "issues"}.get(v, "unreviewed")
        se["reviewer"] = "qc_render.py"
        se["issues"] = issues
        state["phase"] = "DONE"
        save_state(state)
        _finish(state)
        _notify_user(state, _done_message(state, v, issues))
        sys.exit(0)

    if verdict == "pass":
        # manual / Claude Code path only (agent runs auto-QC above and never
        # reach here). Requires a real eval_review.md.
        txt = review.read_text().strip() if review.exists() else ""
        if len(txt) < 100:
            print(f"REFUSED: --eval-verdict pass requires a real review at "
                  f"{review} (found {'nothing' if not txt else str(len(txt)) + ' chars'}). "
                  f"Inspect every eval/*.png and write what you checked and saw.")
            sys.exit(1)
        se["verdict"] = "pass"
        se["reviewer"] = reviewer or "user"
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
    se = state["gates"]["self_eval"]
    verdict = se.get("verdict")
    qc_note = ""
    if se.get("issues"):
        qc_note = "\n**QC flagged:**\n" + "\n".join(f"  - {x}" for x in se["issues"]) + "\n"
    block = (
        f"\n## Session {n} — {time.strftime('%Y-%m-%d')}\n\n"
        f"**Self-eval verdict:** {verdict} (reviewer: {se.get('reviewer', '?')})\n{qc_note}\n"
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
    if target == "self_eval":
        # back to SELF_EVAL: drop the verdict, the review, and the QC job state
        for f in ("eval/eval_review.md",):
            (edit_dir / f).unlink(missing_ok=True)
        state["phase"] = "SELF_EVAL"
        state["gates"]["self_eval"] = {"passes": 0, "verdict": None}
        save_state(state)
        print(f"Restaged to SELF_EVAL (verdict + review + QC state cleared). "
              f"Run `pipeline.py {edit_dir}` to re-run eval frames + QC.")
        sys.exit(0)
    if target == "strategy":
        state["phase"] = "STRATEGY"
        state["gates"]["strategy_confirmed"] = {"done": False}
        for g in ("edl_validated", "render_done"):
            state["gates"][g]["done"] = False
        save_state(state)
        print(f"Restaged to STRATEGY. Rewrite {state['edit_dir']}/strategy.md. "
              f"An agent-started run then auto-advances to EDL; a Claude Code run "
              f"uses `pipeline.py {state['edit_dir']} --confirm-strategy`.")
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
        "STRATEGY": "write strategy.md; post the plan to the user. Agent run: "
                    "auto-advances to EDL. Claude Code run: `--confirm-strategy`.",
        "EDL": "write edl.json per SKILL.md, then `pipeline.py <edit-dir>`.",
        "RENDER": "run `pipeline.py <edit-dir>` to render (nothing to author).",
        "SELF_EVAL": "run `pipeline.py <edit-dir>`. Agent run: auto vision-QC via "
                     "qc_render.py, then DONE + user ping. Claude Code run: "
                     "inspect eval/*.png then `--eval-verdict pass|fail`.",
        "DONE": "nothing — the edit is complete.",
    }
    print(f"\nnext:     {owed[state['phase']]}")


# ──────────────────────────── watch mode ────────────────────────────
#
# `pipeline.py <edit-dir> --watch` is a detached loop that OWNS the mechanical
# transitions an idle agent keeps failing to make. It never authors an artifact.
# For an agent-started run there is no human gate: STRATEGY auto-advances once
# strategy.md exists, SELF_EVAL runs qc_render.py and finishes. It:
#   INGEST / RENDER / SELF_EVAL -> runs `pipeline.py <dir>` itself to advance; a job that
#                       fails MAX_CONSEC_FAIL times in a row -> stop + escalate
#                       (this is what stops a whisper/ffmpeg retry storm).
#   STRATEGY / EDL    -> the artifact (strategy.md / edl.json) is the agent's;
#                       fire ONE rate-limited `system event --mode now` nudge
#                       with the exact next action (and, for EDL, the exact
#                       validator error), keyed so each fresh edit gets one
#                       fresh nudge. Once the artifact is on disk the driver
#                       advances the phase itself (STRATEGY -> EDL auto).
# Absolute backstops: a wall-clock cap and a per-phase driver-run cap, both of
# which stop the watcher and ping the session for a human.
#
# Design note: `plans/AUTO-ADVANCE-DESIGN.md`. `system event --mode now`
# verified to wake an agent turn on live Beast runs (2026-09-08/09); every
# nudge's outcome is logged to jobs/watch.log.

POLL_INTERVAL_S = 20
WATCH_MAX_S = 5400            # 90 min absolute cap on a single watch
NUDGE_COOLDOWN_S = 300        # min seconds between two identical nudges
MAX_NUDGES_PER_PHASE = 6      # after this: stop (write phases) / go quiet (confirm)
MAX_CONSEC_FAIL = 2           # INGEST/RENDER: 1 failure + 1 retry, then stop
MAX_DRIVER_RUNS_PER_PHASE = 60  # ~20 min of polling without advancing -> stop
STATE_SETTLE_S = 12           # skip a cycle if the state file changed this recently


def _watch_paths(edit_dir: Path):
    jobs = edit_dir / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    return jobs / "watch.lock", jobs / "watch_state.json", jobs / "watch.log"


def _read_json(p: Path):
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _wlog(msg: str) -> None:
    # the watcher's stdout is redirected to jobs/watch.log by the spawner;
    # when run in the foreground this just prints to the terminal.
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _run_driver(edit_dir: Path) -> subprocess.CompletedProcess:
    """Invoke the driver exactly as an agent would: `pipeline.py <dir>`. The
    child skips the venv re-exec (this process already is the venv python)."""
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(edit_dir)],
        capture_output=True, text=True,
        env={**os.environ, "_VIDEO_USE_VENV_REEXEC": "1"},
    )


def _nudge(session_key, profile, text: str) -> bool:
    try:
        ok = send_system_event(session_key, profile, text)
    except Exception as e:  # noqa: BLE001
        _wlog(f"nudge raised: {e!r}")
        return False
    _wlog(f"nudge sent={ok}: {text.splitlines()[0][:120]}")
    return ok


def _nudge_gate(wstate: dict, key: str, cooldown: int = NUDGE_COOLDOWN_S):
    """True = send now, False = still cooling down, None = nudge budget for this
    key is exhausted (caller decides: stop, or just stay quiet)."""
    rec = wstate["nudges"].get(key)
    now = time.time()
    if rec is None:
        return True
    count, last = rec
    if count >= MAX_NUDGES_PER_PHASE:
        return None
    return (now - last) >= cooldown


def _nudge_mark(wstate: dict, key: str) -> None:
    rec = wstate["nudges"].get(key) or [0, 0.0]
    wstate["nudges"][key] = [rec[0] + 1, time.time()]


_STRATEGY_NUDGE = (
    "video-use pipeline for {edit} is at STRATEGY. Read {edit}/briefing.md in "
    "full, then write {edit}/strategy.md (4-8 sentences: shape, cut direction, "
    "length target, grade, subtitle style) AND post the same plan to the user "
    "for visibility. There is NO confirmation step — the pipeline advances to "
    "EDL on its own once strategy.md exists. Do only that, then stop."
)
_EDL_NUDGE = (
    "video-use pipeline for {edit} is at EDL. Write {edit}/edl.json per SKILL.md "
    "'EDL format' — coarse structural ranges (source/start/end/reason each, pick "
    "the content to KEEP); set \"strip_fillers\": true to drop um/uh; put each "
    "dropped content span in `omissions` (in the GAP between ranges — omissions "
    "annotate, they don't cut); `background` must be an ABSOLUTE path. Then run:  "
    "pipeline.py {edit}  — do only that, then stop; the watcher renders + QCs from there."
)
_EDL_REJECT_NUDGE = (
    "video-use pipeline for {edit}: edl.json was NOT accepted —\n\n{err}\n\n"
    "Fix {edit}/edl.json and stop. The watcher re-checks automatically."
)


def do_watch(edit_dir: Path, max_seconds: int) -> None:
    edit_dir = edit_dir.resolve()
    lock_file, wstate_file, _log = _watch_paths(edit_dir)

    if not state_path(edit_dir).exists():
        print(f"no {STATE_NAME} in {edit_dir} — nothing to watch. Run `init` first.")
        return

    other = _read_json(lock_file)
    if other and other.get("pid") and _pid_alive(other["pid"]) and other["pid"] != os.getpid():
        print(f"watcher already running for {edit_dir} (pid {other['pid']}); exiting.")
        return

    st0 = _read_json(state_path(edit_dir)) or {}
    if st0.get("phase") == "DONE":
        print(f"pipeline for {edit_dir} is already DONE; not starting a watcher.")
        return

    lock_file.write_text(json.dumps({"pid": os.getpid(), "started_at": time.time()}))
    wstate = _read_json(wstate_file) or {}
    wstate.setdefault("started_at", time.time())
    wstate.setdefault("nudges", {})          # "PHASE:tag[:mtime]" -> [count, last_ts]
    wstate.setdefault("consec_fail", {})     # "INGEST"/"RENDER"   -> int
    wstate.setdefault("driver_runs", {})     # PHASE               -> int
    wstate.pop("stopped", None)
    wstate.pop("stopped_reason", None)

    def persist() -> None:
        wstate_file.write_text(json.dumps(wstate, indent=2))

    def stop(reason: str, ping: bool = True) -> None:
        wstate["stopped"] = True
        wstate["stopped_reason"] = reason
        wstate["stopped_at"] = time.time()
        persist()
        lock_file.unlink(missing_ok=True)
        _wlog(f"STOP: {reason}")
        if ping:
            st = _read_json(state_path(edit_dir)) or {}
            n = st.get("notify") or {}
            if n.get("session_key"):
                _nudge(n["session_key"], n.get("profile"),
                       f"video-use watcher for {edit_dir} has STOPPED: {reason}. "
                       f"It needs manual attention — run  pipeline.py {edit_dir} --status  "
                       f"and check {edit_dir}/jobs/ .")

    deadline = time.time() + max_seconds
    _wlog(f"watcher up (pid {os.getpid()}); cap {max_seconds}s, poll {POLL_INTERVAL_S}s, "
          f"phase {st0.get('phase')}")

    def nap() -> None:
        time.sleep(max(1.0, min(POLL_INTERVAL_S, deadline - time.time())))

    while True:
        if time.time() > deadline:
            stop(f"wall-clock cap ({max_seconds}s) reached without finishing")
            return

        st = _read_json(state_path(edit_dir))
        if st is None:
            stop(f"{STATE_NAME} disappeared (edit dir deleted?)", ping=False)
            return
        phase = st.get("phase")
        n = st.get("notify") or {}
        skey, prof = n.get("session_key"), n.get("profile")

        if phase == "DONE":
            _wlog("pipeline reached DONE — watcher exiting")
            wstate["stopped"] = True
            wstate["stopped_reason"] = "pipeline DONE"
            persist()
            lock_file.unlink(missing_ok=True)
            return

        if wstate["driver_runs"].get(phase, 0) > MAX_DRIVER_RUNS_PER_PHASE:
            stop(f"phase {phase} did not advance after {MAX_DRIVER_RUNS_PER_PHASE} "
                 f"driver attempts")
            return

        try:
            settle = time.time() - state_path(edit_dir).stat().st_mtime
        except OSError:
            settle = 999
        if settle < STATE_SETTLE_S:
            # the agent (or a prior cycle) just wrote state; let it breathe
            nap()
            continue

        result = None
        try:
            if phase == "INGEST":
                result = _watch_job_phase(edit_dir, wstate, "INGEST", "STRATEGY",
                                          "INGEST FAILED")
            elif phase == "STRATEGY":
                result = _watch_strategy(edit_dir, st, wstate, skey, prof)
            elif phase == "EDL":
                result = _watch_edl(edit_dir, st, wstate, skey, prof)
            elif phase == "RENDER":
                result = _watch_job_phase(edit_dir, wstate, "RENDER", "SELF_EVAL",
                                          "RENDER FAILED")
            elif phase == "SELF_EVAL":
                # runs generate_eval_frames + qc_render.py, then
                # advances to DONE on its own and pings the user.
                result = _watch_job_phase(edit_dir, wstate, "SELF_EVAL", "DONE",
                                          "SELF_EVAL FAILED")
            else:
                _wlog(f"unknown phase {phase!r}; idling")
        except Exception as e:  # noqa: BLE001 - a loop bug must not kill the watcher silently
            _wlog(f"loop error in {phase}: {e!r}")

        persist()
        if isinstance(result, tuple) and result and result[0] == "stop":
            stop(result[1])
            return
        if result == "exit":
            wstate["stopped"] = True
            wstate["stopped_reason"] = "handed off at SELF_EVAL"
            persist()
            lock_file.unlink(missing_ok=True)
            return
        nap()


def _watch_job_phase(edit_dir: Path, wstate: dict, phase: str, next_phase: str,
                     fail_marker: str):
    """INGEST / RENDER: run the driver; it launches / polls / advances the
    background job. A run of MAX_CONSEC_FAIL consecutive failures -> stop."""
    r = _run_driver(edit_dir)
    wstate["driver_runs"][phase] = wstate["driver_runs"].get(phase, 0) + 1
    out = (r.stdout or "") + (r.stderr or "")

    now = _read_json(state_path(edit_dir)) or {}
    if now.get("phase") == next_phase:
        _wlog(f"{phase} -> {next_phase}")
        wstate["consec_fail"][phase] = 0
        return None

    failed = r.returncode == 2 or fail_marker in out or out.lstrip().startswith("RENDER FAILED")
    if failed:
        c = wstate["consec_fail"].get(phase, 0) + 1
        wstate["consec_fail"][phase] = c
        _wlog(f"{phase} job failed ({c}/{MAX_CONSEC_FAIL}): {out.strip()[:200]}")
        if c >= MAX_CONSEC_FAIL:
            return ("stop", f"{phase} background job failed {c} times in a row: "
                            f"{out.strip()[:280]}")
        return None

    wstate["consec_fail"][phase] = 0
    _wlog(f"{phase} in progress: {out.strip()[:160]}")
    return None


def _watch_strategy(edit_dir: Path, st: dict, wstate: dict, skey, prof):
    sp = edit_dir / "strategy.md"
    if not (sp.exists() and len(sp.read_text().strip()) >= 200):
        g = _nudge_gate(wstate, "STRATEGY:write")
        if g is None:
            return ("stop", f"agent never wrote a substantive strategy.md after "
                            f"{MAX_NUDGES_PER_PHASE} nudges")
        if g:
            _nudge(skey, prof, _STRATEGY_NUDGE.format(edit=edit_dir))
            _nudge_mark(wstate, "STRATEGY:write")
        return None
    # strategy.md is on disk -> the driver auto-advances an agent run to EDL.
    r = _run_driver(edit_dir)
    wstate["driver_runs"]["STRATEGY"] = wstate["driver_runs"].get("STRATEGY", 0) + 1
    now = _read_json(state_path(edit_dir)) or {}
    if now.get("phase") == "EDL":
        _wlog("STRATEGY -> EDL (auto)")
    return None


def _watch_edl(edit_dir: Path, st: dict, wstate: dict, skey, prof):
    ep = edit_dir / "edl.json"
    if not ep.exists():
        g = _nudge_gate(wstate, "EDL:write")
        if g is None:
            return ("stop", f"agent never wrote edl.json after {MAX_NUDGES_PER_PHASE} nudges")
        if g:
            _nudge(skey, prof, _EDL_NUDGE.format(edit=edit_dir))
            _nudge_mark(wstate, "EDL:write")
        return None

    r = _run_driver(edit_dir)
    wstate["driver_runs"]["EDL"] = wstate["driver_runs"].get("EDL", 0) + 1
    now = _read_json(state_path(edit_dir)) or {}
    if now.get("phase") == "RENDER":
        _wlog("EDL -> RENDER (validated)")
        return None

    out = ((r.stdout or "") + (r.stderr or "")).strip()
    try:
        mtime = int(ep.stat().st_mtime)
    except OSError:
        mtime = 0
    key = f"EDL:reject:{mtime}"  # one fresh nudge per fresh edit of edl.json
    g = _nudge_gate(wstate, key, cooldown=120)
    if g:
        _nudge(skey, prof, _EDL_REJECT_NUDGE.format(edit=edit_dir, err=out[:1200]))
        _nudge_mark(wstate, key)
    elif g is None:
        _wlog("EDL still rejected; nudge budget for this revision exhausted, idling")
    return None


def _spawn_watcher(edit_dir: Path) -> None:
    _, _, log = _watch_paths(edit_dir)
    try:
        with open(log, "a") as lf:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), str(edit_dir), "--watch"],
                stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ, "_VIDEO_USE_VENV_REEXEC": "1"},
            )
        print(f"auto-advance watcher started (log: {log}).")
    except OSError as e:
        print(f"note: could not start the auto-advance watcher ({e}); the pipeline "
              f"still works, it just won't self-advance between phases.")


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
    # An agent-started run (a --notify-session is present) gets a detached
    # auto-advance watcher unless this is passed. A Claude Code run never does
    # (it drives the pipeline interactively; a background loop racing on the
    # same state file would just be confusing).
    ap.add_argument("--no-watch", action="store_true")
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

    if args.notify_session and not args.no_watch:
        _spawn_watcher(edit_dir)


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
    ap.add_argument("--confirm-strategy", action="store_true",
                    help="STRATEGY checkpoint for a Claude-Code-driven run; an "
                         "agent-started run auto-advances once strategy.md exists")
    ap.add_argument("--eval-verdict", choices=["pass", "fail"], default=None,
                    help="SELF_EVAL verdict for a Claude-Code-driven run; an "
                         "agent-started run auto-QCs via qc_render.py")
    ap.add_argument("--reviewer", type=str, default=None,
                    help="who inspected the eval frames (recorded with --eval-verdict pass)")
    ap.add_argument("--restage", choices=["strategy", "edl", "render", "self_eval"], default=None)
    ap.add_argument("--watch", action="store_true",
                    help="run the detached auto-advance loop for this edit dir "
                         "(started automatically by an agent `init`; safe to run by hand)")
    ap.add_argument("--watch-max-seconds", type=int, default=WATCH_MAX_S,
                    help=f"wall-clock cap for a single --watch (default {WATCH_MAX_S})")
    args = ap.parse_args(argv)

    edit_dir = args.edit_dir.resolve()

    if args.watch:
        do_watch(edit_dir, args.watch_max_seconds)
        return

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
        phase_self_eval(state, args.eval_verdict, args.restage, args.reviewer)
        return
    if args.restage:
        do_restage(state, args.restage)
        return

    ADVANCE[state["phase"]](state)


if __name__ == "__main__":
    main()
