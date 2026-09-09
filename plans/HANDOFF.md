# Handoff — video-use: fully-unattended agent editing pipeline

_Last updated: 2026-09-09T11:30:00-05:00_

## 1. Goal

Make the `video-use` editing process **code-enforced end to end** so an LLM agent
(Jensen/Beast) cannot skip a step, reorder, work from the wrong artifact, or
self-certify a step — and so that Mike can hand it a video (from his phone, away
from his desk) and get back a finished edit with **zero mid-run interaction**.
The agent supplies only the two subjective artifacts (`strategy.md`, `edl.json`);
everything else — transcription, filler analysis, cut validation, render, QC — is
deterministic pipeline code. Mike reviews the finished video and replies
`recut …` or `ship it`.

Driven by standing rule `feedback_deterministic_over_probabilistic` and a long
run of agent failures where prose rules didn't stop unverified filler claims,
fabricated silence gaps, skipped steps, a fabricated self-eval review, and a
fabricated `## User confirmation` quote.

Full chronological record: `plans/OBJECTIVE.md`. Design notes:
`plans/PIPELINE-DRIVER-DESIGN.md`, `plans/AUTO-ADVANCE-DESIGN.md`.

## 2. Current state — all committed + pushed

`video-use` HEAD `9b1d6ec`; `openclaw-config` `f2020da`; `unleashed` `b00a0df`.

**`helpers/pipeline.py`** — the driver. `INGEST → STRATEGY → EDL → RENDER →
SELF_EVAL → DONE`, state in `<edit>/pipeline_state.json`. Each `pipeline.py
<edit-dir>` call does all deterministic work for the current phase then advances
or prints what the agent owes.
- `pipeline.py init <video> [--notify-session K --notify-profile P] [--no-watch]`
  forces `<video-parent>/edit/`, re-execs under `<repo>/.venv/bin/python3`, and
  (for agent runs) spawns the `--watch` loop.
- **STRATEGY**: agent runs auto-advance to EDL once `strategy.md` (≥200 chars) is
  on disk — no confirm step. `--confirm-strategy` is a manual checkpoint for
  Claude-Code-driven runs only.
- **EDL**: schema check + `edl_intent_warnings()` (rejects an `omissions` entry
  inside a kept range; rejects ranges keeping ≥95% of the source + `strip_fillers`)
  + `render.py --validate-only --json` (mid-word clips, undeclared speech,
  unresolvable `background`). `strip_fillers` → `filler_cuts.py` →
  `edl.effective.json`.
- **RENDER**: `render.py … --build-subtitles`, self-backgrounding.
- **SELF_EVAL**: agent runs → `qc_render.py` (deterministic, ~0.2s) → verdict →
  DONE → Discord ping to Mike. Claude-Code runs → the manual checklist +
  `--eval-verdict pass|fail`.
- `--watch` detached loop: drives INGEST/RENDER/SELF_EVAL by re-running the
  driver; nudges the agent once (rate-limited) at STRATEGY and EDL; stop caps
  (`MAX_CONSEC_FAIL=2` retry-storm breaker, 6 nudges/phase, 5400s wall clock,
  60 driver-runs/phase). `_job_lock.send_system_event()` is the shared wake path.

**`helpers/qc_render.py`** — NEW. Deterministic render QC, no vision model:
output duration vs EDL, `final.mp4` stream integrity, background-swap log line,
caption sanity (cue count, in-bounds, no pure-filler cues, abrupt-end heuristic).
Writes `eval/eval_review.md`, prints `VERDICT: PASS|ISSUES` (+ bullets) or exits
2. Allowlisted for both agents.

**`helpers/render.py`** — `--validate-only [--json]`; `resolve_asset_path()`
resolves a bare-filename `background` against the edit-dir parent / source dirs;
a truly-missing bg → `{"kind":"background"}` warning → EDL gate rejects.
`build_master_srt()` drops all-filler caption cues and (with `strip_fillers`)
filler words. `_declared_omission` = overlap check. Empty-SRT guard.

**`helpers/filler_cuts.py`, `helpers/_job_lock.py`, `helpers/check_fillers.py`** —
as before; see `OBJECTIVE.md`.

**SKILL.md (canonical + both scoped, scoped byte-identical to each other) +
both SOUL.md** — rewritten for the unattended model: agent writes `strategy.md`
(+ posts the plan) and `edl.json`, nothing else; no confirm, no review step;
`omissions` DO NOT cut; `strip_fillers` isn't the edit; `background` = absolute.

## 3. Files being touched

- `helpers/pipeline.py`, `helpers/qc_render.py` (new), `helpers/render.py`,
  `helpers/_job_lock.py`, `helpers/filler_cuts.py` — committed.
- `SKILL.md` + `~/.openclaw/skills/video-use/SKILL.md` +
  `~/.openclaw-unleashed/skills/video-use/SKILL.md` — the two scoped copies MUST
  stay byte-identical to each other (`diff` then `cp`), NOT to canonical.
- `~/.openclaw/workspace/SOUL.md` + `~/.openclaw-unleashed/workspace/SOUL.md`.
- `~/.openclaw/workspace/reference/exec-approvals-baseline-{jensen,unleashed}.json`
  — regenerated (contain `qc_render.py`).
- `plans/{OBJECTIVE,HANDOFF,AUTO-ADVANCE-DESIGN,PIPELINE-DRIVER-DESIGN}.md`.
- Test data (not committed): `~/Desktop/video-use/test-beast/` — `IMG_4328.MOV`,
  the AVIF, and `edit/` (a completed 2026-09-09-AM render under the *old* gated
  flow: 50.9s, minor issues Mike accepted).

## 4. What's been tried that failed

- **Prose Hard Rules alone** — violated repeatedly; the reason the driver exists.
- **Gating `--confirm-strategy` / `--eval-verdict pass` behind `--confirmed-by` /
  `--reviewer`** — an agent just types the flag. Beast passed `--confirmed-by
  mike` itself and drove the whole pipeline (2026-09-08).
- **An out-of-band nonce** (`~/.video-use-gate/<hash>/*.nonce`, agent has no file
  read primitive) — worked, but put obscure-password friction on Mike, who may
  be on his phone. Removed; the human gate is gone for agent runs instead.
- **A local VLM as the SELF_EVAL reviewer** (`describe_frames.py`) — qwen3.6
  (base and `-vl-oq8`) burns its whole token budget on unsuppressable reasoning →
  empty `content` on image calls; 20–90s per call, ~15 min per edit. `/no_think`
  helps marginally. Deleted; `qc_render.py` is deterministic instead.
- **Requiring omissions to exactly bound a removed span** — token-limited model
  declares the *content* span, not the arithmetic gap. Relaxed to overlap.
- **LLM hand-authoring a 15–20 range filler EDL** — model truncates the JSON.
  Hence `strip_fillers` + coarse ranges.
- **A lazy full-source range + `strip_fillers` as "the edit"** — Beast did this
  2026-09-08; now rejected at the EDL gate.
- **LM Studio instability** blocked two live runs (8-model RAM pileup; then
  qwen3.6 "Context size has been exceeded" on every prompt — a gateway restart
  cleared it). Not a pipeline bug but the recurring blocker for live testing.

## 5. What to do next

1. **First full LIVE agent run in unattended mode.** Clear `test-beast/edit`.
   Have Beast `init`, then write `strategy.md` (on the STRATEGY nudge) and
   `edl.json` (on the EDL nudge) — nothing else. Confirm the run reaches DONE
   and pings Mike's channel with the `qc_render` verdict, with **zero terminal
   interaction**. Watch `<edit>/jobs/watch.log`.
2. **Tune `qc_render.py`** from that run — the abrupt-end heuristic is the
   weakest check (a soft "note", not an "issue"); adjust if noisy or missing
   things.
3. **Test a `recut` round-trip**: Mike replies "recut the ending" to the DONE
   ping → Beast does `--restage edl`, rewrites `edl.json`, re-renders, re-QCs,
   re-pings. Confirm that loop works agent-side.
4. Scrap the stale `test-beast/edit` (2026-09-09-AM render) before the live test.
