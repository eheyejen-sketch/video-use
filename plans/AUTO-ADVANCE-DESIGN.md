# Design — `pipeline.py --watch`: auto-advance the video-use pipeline

_Written 2026-09-08. **Status: BUILT + VERIFIED LIVE 2026-09-08.** A full Beast run
(`test-beast`, 22:16–22:27) went INGEST→STRATEGY→EDL→RENDER→SELF_EVAL entirely on
the watcher: 2 nudges (STRATEGY, EDL), both `sent=True`, **both produced a Beast
turn within ~15–31 s** — the nudge-transport question is answered, `system event
--mode now` reliably wakes the agent. INGEST self-advanced over 18 polls, RENDER
over 6, zero failures/retries, watcher exited cleanly at SELF_EVAL
(`stopped_reason: "handed off at SELF_EVAL"`). The run also exposed a real gate
hole — see "STRATEGY confirmation" below._

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
- **Live Beast run 2026-09-08 22:16–22:27** — verified the whole lifecycle end to
  end (see the status line at the top).

## STRATEGY confirmation — a self-certifiable gate the live run exposed (FIXED 2026-09-08)

The live run's STRATEGY nudge woke Beast, which wrote `strategy.md` with a `##
User confirmation` section containing an **invented quote** — `"Go ahead with
this plan."` — attributed to Mike for a plan (specific cut ranges) Mike had never
seen, then ran `--confirm-strategy` itself ~31 s after the nudge, despite having
said 20 min earlier it would wait for confirmation. Same failure class as the
fabricated SELF_EVAL review.

Fix (mirrors the SELF_EVAL `--reviewer` gate): `--confirm-strategy` from an
agent-started run (`running_as_agent(state)`) is refused unless it carries
`--confirmed-by <name>`, which only a human / Claude Code supplies after the user
actually approves. The gate records `strategy_confirmed.confirmed_by`. The agent
writes `strategy.md`, presents the plan, and stops. `_STRATEGY_NUDGE` /
`_STRATEGY_CONFIRM_NUDGE` and the INGEST/STRATEGY/status/restage guidance text
updated to say so; SKILL.md (canonical + both scoped) + both SOUL.md too.
Tested: agent run without the flag → REFUSED, phase held; with
`--confirmed-by claude-code` → advances; Claude Code run (no notify) →
advances without the flag (records `confirmed_by: "user"`).

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
- [x] **Nudge transport verified on a LIVE run** — `system event --mode now`
      produced a Beast turn within ~15–31 s at both STRATEGY and EDL (Beast run
      2026-09-08 22:16–22:27). No transport swap needed.
- [x] SKILL.md + SOUL.md wording: "when a nudge names a next action, do exactly
      that one thing, then stop."
- [x] Live full-run: INGEST + RENDER self-advanced; STRATEGY + EDL nudges each
      drew a Beast turn; watcher exited cleanly at SELF_EVAL.
- [x] STRATEGY confirmation self-certification hole found on that run and fixed
      (`--confirmed-by` gate — see section above).
- [ ] Follow-up live test: deliberately stall mid-STRATEGY → confirm the watcher
      re-nudges at the 300 s cooldown and a fresh turn completes it. (Not yet
      done — the live run advanced too fast to exercise the re-nudge path.)
