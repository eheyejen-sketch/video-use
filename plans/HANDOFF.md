# Handoff — video-use `pipeline.py` driver: built, Beast-tested, 2 fixes shipped

_Last updated: 2026-09-08T11:05:00-05:00_

## 1. Goal

Make the `video-use` editing process **code-enforced end to end** so an LLM agent
(Claude Code, Jensen, Beast) physically cannot skip a step, reorder steps, work from
the wrong artifact, or self-certify a step happened. LLM keeps only the subjective
calls (cut selection, pacing, grade, captions). Driven by standing rule
`feedback_deterministic_over_probabilistic` and three failed agent runs
(2026-09-07 ×2, 2026-09-08) where prose Hard Rules didn't stop an unverified filler
claim, a fabricated silence gap, or a skipped step.

Full context: `plans/OBJECTIVE.md` (the charter — goal in Mike's words, acceptance
criteria 1–6, directives log, and a detailed work log of everything done today).
`plans/PIPELINE-DRIVER-DESIGN.md` (design + resolved open questions).

## 2. Current state

**Built + verified (committed/pushed):**
- `helpers/pipeline.py` — state machine `INGEST→STRATEGY→EDL→RENDER→SELF_EVAL→DONE`,
  state in `<edit>/pipeline_state.json`. Adversarial gate walk passed from Claude Code
  (word-clip EDL, undeclared/declared `omissions`, thin/unconfirmed strategy, 3-fail
  cap, happy path). See OBJECTIVE.md work log.
- `helpers/render.py` — `--validate-only` + `--validate-only --json` (classified
  warnings); validator honours an EDL `omissions:[{source,start,end,reason}]` array
  (declared removed-speech span = intentional, suppressed; mid-word clips always fail).
- `helpers/_job_lock.py` — `_resolve_gateway_token(profile)` recovers the gateway
  token from `~/.openclaw{-<profile>}/service-env/*.env` (handles single-quoted
  values) when absent from env; `notify_completion` passes it `--token` and now
  PRINTS `[notify] …` to the job log instead of `except: pass`.
- `SKILL.md` (canonical) + both scoped copies (`~/.openclaw{,-unleashed}/skills/
  video-use/SKILL.md`, byte-identical to each other, NOT to canonical) — process
  table, Hard Rules 6/8, "If the pipeline refuses", EDL `omissions`, SELF_EVAL
  `eval_review.md` requirement.
- Both `SOUL.md` — "drive through pipeline.py" (unchanged since first rollout).
- exec-approvals: `pipeline.py` allowlisted `--agent "*"` for Jensen + Beast;
  both audit baselines regenerated; audit clean.
- Commits: video-use `713aaeb` (HEAD), earlier `916eb90`/`91d5ada`.
  openclaw-config `3ab5988`, unleashed `fd80680`.

**Beast live run (2026-09-08, `test-beast/IMG_4328.MOV` → `test-beast/IMG_4328-edited/`):
reached DONE. Core objective PROVEN, with 4 gaps — 2 now fixed, 2 open.**
- PASSED: full pipeline, every gate enforced, `check_fillers.py` auto-run and cited
  ("19 fillers", not eyeballed), gap table cited correctly (no fabrication), EDL
  passed cut-validator clean first try with 2 `omissions` entries, render succeeded,
  **background swap worked** (verified against source frames), `project.md` written.
- GAP 1 (FIXED, unverified live): `--notify-session` never worked from a worker —
  exec strips OPENCLAW_* secrets, `openclaw system event` failed auth, swallowed
  silently. Beast stalled 15 min at RENDER, needed a manual Discord poke. Fix in
  `_job_lock.py` above. Still unproven: whether `system event --mode now` actually
  triggers a turn (one manual `--mode now` returned `ok` but didn't visibly wake
  Beast in 15 min — maybe confounded by heartbeat timing).
- GAP 2 (FIXED): blind self-eval — Beast ran `--eval-verdict pass` without seeing
  `eval/*.png`. `pipeline.py` now refuses `--eval-verdict pass` unless
  `eval/eval_review.md` exists (≥100 chars); `running_as_agent()` adds a
  "hand off to human/Claude Code/describe_frames.py" note. `--eval-verdict fail`
  never needs the review.
- GAP 3 (OPEN, backlog): edit fidelity ≠ strategy. Beast's strategy said "strip all
  19 fillers"/"~45s"; EDL kept 11 fillers inside 4 coarse ranges, output 54.9s, kept
  "Hi, everyone" despite the range `reason` claiming it was trimmed. The driver
  enforces order + cut safety, not "EDL does what strategy said."
- GAP 4 (OPEN, backlog): subtitles carry the 11 kept fillers (SRT = verbatim
  kept-range transcript; `render.py --build-subtitles` doesn't filter fillers).

## 3. Files being touched

- `helpers/pipeline.py` — the driver. `running_as_agent(state)` helper; SELF_EVAL
  gate requires `eval/eval_review.md`; INGEST skips visual samples for agents.
- `helpers/render.py` — `--validate-only[/--json]`, `_classify_cut_warning`,
  `_declared_omission`; validator honours `omissions`; `-o` optional.
- `helpers/_job_lock.py` — `_resolve_gateway_token`, `_unquote`, `_TOKEN_RE`;
  `notify_completion` resolves+passes `--token`, logs outcome.
- `SKILL.md` + `~/.openclaw/skills/video-use/SKILL.md` +
  `~/.openclaw-unleashed/skills/video-use/SKILL.md` — the two scoped copies MUST
  stay byte-identical to each other (`diff` then `cp`), NOT to canonical.
- `~/.openclaw/workspace/SOUL.md` + `~/.openclaw-unleashed/workspace/SOUL.md` —
  Video Editing section (done, unchanged this round).
- `~/.openclaw/workspace/reference/exec-approvals-baseline-{jensen,unleashed}.json`.
- `plans/OBJECTIVE.md` (charter+worklog), `PIPELINE-DRIVER-DESIGN.md`, `HANDOFF.md`.
- Test data (not committed): `~/Desktop/video-use/test-beast/IMG_4328.MOV` and
  `~/Desktop/video-use/test-jensen/IMG_4328.MOV` (same file). Beast's completed run
  is at `~/Desktop/video-use/test-beast/IMG_4328-edited/`.

## 4. What's been tried that failed

- **Prose Hard Rules alone** — violated repeatedly; the reason the driver exists.
- **`check_fillers.py` as a standalone helper** — an agent that never runs it is
  unaffected. Driver now runs it automatically.
- **Original design assumed `render.py --validate-only` returns clean for a good
  EDL** — it flags ALL removed speech, so a real content cut couldn't pass. Fixed
  with the `omissions` array.
- **`phase_render` launched a render before checking status** — re-ran a completed
  render. Fixed: status-check first, spawn only on NOT_FOUND/FAILED.
- **`--notify-session` "verified live" 2026-09-07** — that verification was from a
  shell with the token exported. In a real worker it's a no-op (see GAP 1).
- **Attributing Beast's ~80s INGEST resume to the notify** — it was a heartbeat
  catch. Don't assume the notify works until seen waking a session from a worker.
- **Assuming `final.mp4`'s background was the original** — it was the AVIF; the swap
  worked. Always extract a source frame before claiming a render defect.
- **Parallel Jensen+Beast runs** — Mike rejected (CPU contention). Runs are sequential.

## 5. What to do next

1. **Clean criterion-4 re-run on Beast.** Ask Mike to: (a) `rm -rf
   ~/Desktop/video-use/test-beast/IMG_4328-edited/`; (b) reset Beast's session so it
   loads the updated scoped SKILL.md (SOUL.md unchanged — `/reset` via Escape-to-
   dismiss, or `openclaw --profile unleashed sessions delete
   "agent:main:discord:channel:1544905269698498660" --agent main --yes`); (c) send
   the edit prompt again. Monitor `test-beast/IMG_4328-edited/` (Beast picks its own
   `--edit-dir` name) — watch `pipeline_state.json` phase, job status, `jobs/*.log`
   for `[notify]` lines. **Key checks:** does the render-done `[notify]` line say
   "sent", and does Beast actually resume RENDER→SELF_EVAL on its own? Does it hit
   the `eval_review.md` wall and hand the visual check off instead of passing blind?
2. When Beast reaches SELF_EVAL: inspect `eval/*.png` yourself (you're vision-
   capable — `ffmpeg -ss <t> -i final.mp4 -frames:v 1 out.jpg`, then Read), write
   `eval/eval_review.md`, run `pipeline.py <dir> --eval-verdict pass` from Claude
   Code to finish the run.
3. Then a Jensen run on `~/Desktop/video-use/test-jensen/IMG_4328.MOV` (criterion 5;
   Jensen omits `--notify-profile`).
4. When 1–6 verified, walk `OBJECTIVE.md` criterion-by-criterion with Mike for
   sign-off. **Claude never marks an item CLOSED — Mike does.**
5. Backlog (OBJECTIVE.md, not blocking sign-off): `describe_frames.py` (VL-model
   helper writing `eval_review.md`); strategy↔EDL fidelity warning; `render.py
   --build-subtitles --drop-fillers`; `pipeline.py init` default `--edit-dir` to
   `<video_parent>/edit/`.
6. Separate agreed deliverable, not started: the **global anti-drift system**
   (CLAUDE.md rule + SessionStart hook surfacing `plans/OBJECTIVE.md`; TRIGGERED not
   default — no file = normal session).
