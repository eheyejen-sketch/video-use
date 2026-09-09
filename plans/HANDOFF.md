# Handoff — video-use: fully-unattended agent editing pipeline

_Last updated: 2026-09-09T16:45:00-05:00_

## 1. Goal

`video-use` is a conversation-driven video editor. Target: Mike hands Beast (or
Jensen) a video from anywhere — including his phone, away from a terminal — and
gets back a finished edit with **zero mid-run interaction**. The agent supplies
only the two subjective artifacts (`strategy.md`, `edl.json`); everything else
(transcription, filler analysis, cut validation, render, QC) is deterministic
pipeline code. On DONE the agent pings Mike's Discord channel with `final.mp4` +
a QC verdict; Mike watches it and replies `recut …` or `ship it`.

Why the machinery exists: prose "Hard Rules" repeatedly failed to stop the local
model (qwen3.6-35b-a3b) from fabricating filler counts, silence gaps, a self-eval
review, and a `## User confirmation` quote, and from skipping steps. Step order
is now code-enforced by a state machine the agent cannot bypass.

Full chronological record: `plans/OBJECTIVE.md`. Design: `plans/AUTO-ADVANCE-DESIGN.md`,
`plans/PIPELINE-DRIVER-DESIGN.md`.

## 2. Current state

`video-use` HEAD `e7849cd` (last logic commit `2c5baf3`); `openclaw-config`
`268a3dc`; `unleashed` `a10b4c8`. All pushed.

**VERIFIED WORKING end to end (live Beast runs, 2026-09-09):**
- Unattended run #2: `init → DONE` with no terminal interaction. Transcription
  survived, STRATEGY auto-advanced (no confirm gate), the EDL gate rejected the
  first `edl.json` and Beast self-corrected, RENDER + `qc_render` ran, DONE +
  Discord ping. `final.mp4` 55.8s, `qc_render` PASS.
- Recut round-trip: Mike replied "remove a duplicate 'for'"; Beast did
  `--restage edl`, split a range + declared the omission, re-rendered cleanly
  (55.8s → 55.4s, the cut took), qc PASS, DONE + ping.

**`helpers/pipeline.py`** — the driver. `INGEST → STRATEGY → EDL → RENDER →
SELF_EVAL → DONE`, state in `<edit>/pipeline_state.json`.
- `init <video> [--notify-session K --notify-profile P] [--no-watch]` — forces
  `<video-parent>/edit/`, re-execs under `<repo>/.venv/bin/python3`, spawns the
  `--watch` loop for agent runs. **Refuses** if a watcher is live for that edit
  dir or if `init` ran <150s ago.
- **STRATEGY** — agent runs auto-advance to EDL once `strategy.md` (≥200 chars)
  exists. No confirm step. `--confirm-strategy` is a manual checkpoint for
  Claude-Code-driven runs only.
- **EDL** — schema check + `edl_intent_warnings()` (rejects an `omissions` entry
  inside a kept range; rejects ranges keeping ≥95 % of source + `strip_fillers`)
  + `render.py --validate-only --json` (mid-word clips, undeclared speech,
  unresolvable `background`). `strip_fillers` → `filler_cuts.py` →
  `edl.effective.json`.
- **RENDER** — `render.py … --build-subtitles`, self-backgrounding. Force
  re-renders if the EDL is newer than `final.mp4` (stale-render guard).
- **SELF_EVAL** — agent runs → `qc_render.py` (deterministic, ~0.2 s) → verdict →
  DONE → `_notify_user()` pings the channel. Claude-Code runs → manual checklist
  + `--eval-verdict pass|fail`.
- `--restage strategy|edl|render|self_eval` — `_invalidate_render()` deletes
  `final.mp4`, `final.prenorm.mp4`, `jobs/render_<stem>.json`, `eval/*.png`,
  `eval/eval_review.md`; re-spawns the watcher for agent runs. **Refuses** if a
  restage ran <90s ago.
- `--watch` — detached loop. Drives INGEST/RENDER/SELF_EVAL by re-running the
  driver; one rate-limited `system event` nudge at STRATEGY and EDL. Stop caps:
  `MAX_CONSEC_FAIL=2` (retry-storm breaker), 6 nudges/phase, 5400 s wall clock,
  60 driver-runs/phase. Coord markers `~/.cache/video-use/locks/<sha1>.{init,watch,restage}`
  survive `rm -rf <edit-dir>`; `_spawn_watcher` no-ops if one is live; `do_watch`
  exits if a newer watcher takes the marker.

**`helpers/qc_render.py`** (NEW, replaced a failed local-VLM reviewer) —
deterministic only: duration vs EDL, `final.mp4` stream integrity, background-swap
log line, caption sanity (count, in-bounds, no pure-filler cues, abrupt-end
heuristic), `final.mp4 older than EDL` → stale ISSUE. Writes `eval/eval_review.md`,
prints `VERDICT: PASS|ISSUES`, exit 2 on failure. On both agents' exec allowlists.

**`helpers/transcribe.py`** — whisper thread pools capped at 8
(`OMP/MKL/OPENBLAS/...`, override `VIDEO_USE_WHISPER_THREADS`); whisper launched
`start_new_session=True`, whole process group SIGKILL'd on failure/interrupt.

**`helpers/render.py`** — `--validate-only [--json]`; `resolve_asset_path()`
resolves a bare-filename `background` against the edit-dir parent / source dirs;
`build_master_srt()` drops all-filler caption cues and (with `strip_fillers`)
filler words; `_declared_omission` overlap check; empty-SRT guard.

**SKILL.md (canonical + both scoped, scoped byte-identical) + both SOUL.md** —
rewritten for the unattended model.

**test data (not committed):** `~/Desktop/video-use/test-beast/` —
`IMG_4328.MOV`, the AVIF, and `edit/` at DONE with the recut render (55.38s).

## 3. Files being touched

- `helpers/pipeline.py` — driver + watcher + all guards. Committed.
- `helpers/qc_render.py` (new), `helpers/transcribe.py`, `helpers/render.py`,
  `helpers/filler_cuts.py`, `helpers/_job_lock.py` — committed.
- `SKILL.md` + `~/.openclaw/skills/video-use/SKILL.md` +
  `~/.openclaw-unleashed/skills/video-use/SKILL.md` — the two scoped copies MUST
  stay byte-identical to each other (`diff` then `cp`), NOT to canonical.
- `~/.openclaw/workspace/SOUL.md` + `~/.openclaw-unleashed/workspace/SOUL.md`.
- `~/.openclaw/workspace/reference/exec-approvals-baseline-{jensen,unleashed}.json`.
- `plans/{OBJECTIVE,HANDOFF,AUTO-ADVANCE-DESIGN,PIPELINE-DRIVER-DESIGN}.md`.
- Coord state (runtime, not repo): `~/.cache/video-use/locks/`.

## 4. What's been tried that failed

- **Prose Hard Rules alone** — violated repeatedly; the reason the driver exists.
- **Gating the human steps behind `--confirmed-by` / `--reviewer` flags** — an
  agent just types them. Beast passed `--confirmed-by mike` itself.
- **An out-of-band nonce** (`~/.video-use-gate/`) — worked but was obscure
  password friction on Mike. Removed; the human gate is gone for agent runs
  (Mike reviews the video instead).
- **A local VLM as SELF_EVAL reviewer** (`describe_frames.py`, deleted) — qwen3.6
  (base and `-vl-oq8`) burns its whole token budget on unsuppressable reasoning →
  empty `content` on image calls, 20–90 s each, ~15 min per edit. `/no_think`
  barely helps. `qc_render.py` is deterministic instead.
- **LM Studio instability** blocked two live runs (8-model RAM pileup; then
  qwen3.6 "Context size has been exceeded" on every prompt). Cleared by a gateway
  restart / `POST 10.211.55.2:1234/api/v1/models/unload {"instance_id":…}`.
- **Unthrottled whisper on the VM** — one turbo + word_timestamps job pins all
  16 vCPUs; the VM's flaky jetsam SIGKILLs it at ~98% ("leaked semaphore").
  Fixed by the thread cap + process-group kill.
- **Beast loops any "re-run X" instruction** — re-ran `init` in a loop after a
  transcribe failure, and `--restage edl` in a loop after the recut request. Both
  stacked watchers + renders and spiked the box (load 13). Fixed by the
  150s/90s guards + `_spawn_watcher` dedupe.
- **Beast stalls after announcing it will act** — said "let me push it through
  the render" then did nothing. Only the watcher reliably drives a run.
- **Requiring omissions to exactly bound a removed span** — model declares the
  content span, not the arithmetic gap. Relaxed to overlap.
- **A lazy full-source range + `strip_fillers` as "the edit"** — rejected at the
  EDL gate now.

## 5. What to do next

1. **Confirm the recut removed the right word.** Watch
   `~/Desktop/video-use/test-beast/edit/final.mp4` — the output still has "FOR
   VIDEO" (~6.3 s) and "FOR GRAPHIC" (~10.6 s) captions; Beast cut source
   7.42–7.84. Ask Mike whether that's the "for" he meant.
2. **One clean unattended run from scratch, zero interventions**, to prove the
   guards hold: `rm -rf ~/Desktop/video-use/test-beast/edit`, then Mike sends
   Beast a video + edit request. Watch `<edit>/jobs/watch.log`. Expect
   `init → transcription → auto STRATEGY → EDL nudge → auto RENDER → qc_render →
   DONE + Discord ping` with no terminal use.
3. **Firm up agent-facing wording** so Beast stops looping: the DONE/recut ping
   and the STRATEGY/EDL nudges should say "run this ONCE, then wait." The runtime
   REFUSED messages + a scoped-SKILL "don't re-run --restage/init" line are in
   place; Beast looped anyway before the guards existed.
4. **Backlog (OBJECTIVE.md):** a `recut` that regenerates `strategy.md` is
   untested; vision QC if a fast non-reasoning local VLM appears; a global
   anti-drift SessionStart hook.
5. **Unrelated:** `MEMORY.md` (Claude Code memory index) is near its read-size
   limit — a system hook flagged it for compaction. Separate task.
