# Design — `pipeline.py --watch`: auto-advance the video-use pipeline

_Written 2026-09-08. **Status: BUILT 2026-09-08** (`video-use` helpers/pipeline.py +
helpers/_job_lock.py). Not yet exercised on a live agent run — the nudge-transport
question below is still open and the first live run answers it._

## Build notes (what actually shipped)

- `pipeline.py <edit-dir> --watch [--watch-max-seconds N]` — the detached loop.
  `pipeline.py init` auto-spawns it **only when `--notify-session` is set** (an
  agent run); a Claude Code `init` never spawns one. `--no-watch` on `init`
  suppresses it. Lock: `jobs/watch.lock` (pid); state: `jobs/watch_state.json`;
  log: `jobs/watch.log`.
- Shared wake path: `_job_lock.send_system_event(session_key, profile, text)` —
  factored out of `notify_completion`, reused by the watcher. Same service-env
  token recovery + bogus-profile fallback.
- Caps that stop the watcher and ping the session for a human:
  `MAX_CONSEC_FAIL = 2` (INGEST/RENDER: one failure + one retry — this is the
  retry-storm breaker), `MAX_NUDGES_PER_PHASE = 6` (STRATEGY/EDL write nudges
  then stop; the human-gated confirm nudge just goes quiet), `WATCH_MAX_S = 5400`
  wall clock, `MAX_DRIVER_RUNS_PER_PHASE = 60` (~20 min no-progress backstop).
  `STATE_SETTLE_S = 12` — skips a cycle if the agent just wrote state.
- SELF_EVAL: the watcher generates the eval frames, fires one review nudge, then
  **exits** (an agent-started run can't pass that phase; the pipeline parks there
  safely for a human / Claude Code / future `describe_frames.py`).
- Tests run 2026-09-08 (in-tree, temp dirs): nudge cooldown + budget→stop;
  consec-fail cap stops on the 2nd failure with an escalation; full
  `init → watcher → INGEST (3 driver polls, "WAITING" not misread as failure)
  → STRATEGY` on a real 3 s clip; EDL-reject path emits one nudge keyed to the
  edl.json mtime (a fresh edit ⇒ a fresh nudge).

---

_Original design follows._

## Why

The `pipeline.py` driver enforces step *order* and *correctness*, but the agent
still has to (a) choose to run `pipeline.py <dir>` again after every phase, and
(b) produce the creative artifacts. On 2026-09-08, Beast and Jensen failed at (a)
repeatedly: stalled right after `init`; went idle in a WAITING state when a
notify failed; ended a turn with no usable output (model degradation); retry-
stormed a failing job. A better local model helps but doesn't fix this class —
what fails is *multi-step sequencing stamina*, not single-turn ability.

**The fix:** a deterministic watcher that owns all the mechanical transitions and
nudges the agent only for the genuinely creative artifacts. The agent's job
shrinks to "respond to a targeted nudge by producing one artifact." Stalls
self-heal because the watcher re-nudges.

This is orthogonal to model choice — even a mediocre model reliably produces one
artifact on request; the watcher removes the part they can't do.

## Shape

`pipeline.py --watch <edit-dir>` — a detached background loop (like the
transcribe/render workers), spawned by `pipeline.py init`, logging to
`<edit>/jobs/watch.log`. One per edit-dir (`<edit>/jobs/watch.lock` holds its
pid; a second `--watch` exits if one is alive). Self-terminates on phase `DONE`,
on a wall-clock cap (default 5400 s), or if `pipeline_state.json` vanishes.

Every ~20 s it reads `pipeline_state.json` and acts:

| Phase | Watcher does |
|---|---|
| **INGEST** | transcription job RUNNING → wait. all DONE / no job yet → run `pipeline.py <dir>` (does pack/fillers/gaps/briefing → STRATEGY). job FAILED → run `pipeline.py <dir>` once to relaunch; if it fails again → nudge the human, stop retrying. |
| **STRATEGY** | agent-owed. `strategy.md` missing → nudge agent (targeted): *"pipeline at STRATEGY — read briefing.md, write strategy.md + a `## User confirmation` section, then `pipeline.py <dir> --confirm-strategy`."* present but `strategy_confirmed=false` → nudge: *"strategy.md written — run `--confirm-strategy` once Mike has approved."* (confirmation stays human-gated — watcher never runs `--confirm-strategy`.) |
| **EDL** | agent-owed. `edl.json` missing → nudge with the EDL-format reminder + `strip_fillers`. present → run `pipeline.py <dir>` (validates + advances, or prints an error); on error → nudge the agent with the exact error text. |
| **RENDER** | self-advancing. render job RUNNING → wait. DONE / NOT_FOUND → run `pipeline.py <dir>` (launches render / advances). FAILED → relaunch once; twice → nudge human, stop. |
| **SELF_EVAL** | human/Claude-Code-owed (an agent can't pass — see the fabricated-review incident). `eval_review.md` missing → nudge: *"render done, needs a visual review — Mike or Claude Code: inspect `eval/*.png`, write `eval/eval_review.md`, run `--eval-verdict pass --reviewer <name>`."* Watcher never passes the verdict. (Once `describe_frames.py` exists, the watcher runs *it* here and then the pipeline can accept its review.) |
| **DONE** | log, release the lock, exit 0. |

### Nudge mechanism

`openclaw [--profile P] system event --session-key <key> --text "<instruction>" --mode now`,
reusing `_job_lock`'s token resolution + bogus-profile fallback. Rate-limited:
at most one nudge per phase per **nudge-cooldown** (default 300 s), tracked in
`<edit>/jobs/watch_state.json` (`{last_nudge_phase, last_nudge_at, job_retries:{}}`).

**KEY OPEN QUESTION — verify before building:** does `system event --mode now`
*reliably trigger an agent turn*? On 2026-09-08 a manually-confirmed `--mode now`
event did NOT visibly wake Beast within 15 min (possibly confounded by heartbeat
timing / session state). If it's not reliable, the watcher must nudge via a
channel that definitely triggers a turn — e.g. post the instruction to the
agent's Discord channel directly (`openclaw ... send`?), or `system event
--expect-final`. Pick the transport in a spike before committing the design.

### What the watcher never does
- Never writes `strategy.md`, `edl.json`, or `eval_review.md`.
- Never runs `--confirm-strategy` (human approval) or `--eval-verdict pass`
  (human/CC review).
- Never retries a job more than once (kills the retry-storm class).
- Never runs past the wall-clock cap.

## Net effect

Hand Beast a video → `pipeline.py init` starts the watcher → INGEST auto-completes
→ watcher nudges "write strategy.md" → Beast does → Mike confirms → watcher
advances → nudges "write edl.json" → Beast does → watcher validates + advances +
renders → watcher nudges "needs a visual review" → describe_frames.py or a human →
DONE. Beast's contribution: 2 nudged artifacts. A stall between them self-heals.

## Build checklist
- [x] `pipeline.py --watch <dir>` loop + `watch.lock` + `watch_state.json` +
      `watch.log`; wall-clock cap; self-terminate conditions.
- [x] Per-phase logic table above; consec-fail stop cap (retry-storm breaker) +
      per-phase driver-run backstop.
- [x] `pipeline.py init` spawns the watcher detached for agent runs
      (`--no-watch` to skip; never for Claude Code).
- [x] exec-approval: confirmed `pipeline.py` is allowlisted with no argPattern,
      so `--watch` is covered; the auto-spawn and `openclaw system event` are
      plain subprocesses, not gated.
- [ ] **Spike the nudge transport on a LIVE run** (`system event --mode now` vs
      Discord send vs `--expect-final`) — still the load-bearing unknown. Every
      nudge logs `sent=True/False` to `jobs/watch.log`; the first real agent run
      shows whether `sent=True` actually produces an agent turn.
- [ ] SKILL.md + SOUL.md wording: "a watcher advances the mechanical steps and
      will nudge you; when nudged, do exactly what it says — one artifact, then
      stop." (doing this now)
- [ ] Live test: start an agent run, let it stall mid-STRATEGY → confirm the
      watcher re-nudges after the cooldown and a fresh agent turn completes it;
      stall mid-RENDER → confirm the watcher advances with no agent turn.
