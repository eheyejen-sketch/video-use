# Handoff — video-use `pipeline.py` driver: built, Beast run complete, Jensen parked

_Last updated: 2026-09-08T18:10:00-05:00_

## 1. Goal

Make the `video-use` editing process **code-enforced end to end** so an LLM agent
(Claude Code, Jensen, Beast) physically cannot skip a step, reorder steps, work from
the wrong artifact, or self-certify a step. LLM keeps only the subjective calls (cut
selection, pacing, grade, captions). Driven by standing rule
`feedback_deterministic_over_probabilistic` and repeated agent failures where prose
Hard Rules did not stop an unverified filler claim, a fabricated silence gap, a
skipped step, or (2026-09-08) a fabricated self-eval review.

Full context: `plans/OBJECTIVE.md` — the charter: goal in Mike's words, acceptance
criteria 1–6, the directives log (3 still marked OPEN — Mike closes those, not
Claude), and a dense chronological work log of everything done today.
`plans/PIPELINE-DRIVER-DESIGN.md` — the original design + resolved open questions.

## 2. Current state

**DONE & verified (all committed + pushed; `video-use` HEAD `e2c6088`,
`openclaw-config` + `unleashed` synced via each profile's `backup-workspace`):**

- `helpers/pipeline.py` — the driver. State machine
  `INGEST → STRATEGY → EDL → RENDER → SELF_EVAL → DONE`, state in
  `<edit>/pipeline_state.json`. Features added across today:
  - forces `--edit-dir` to `<first-source-parent>/edit` (agents kept inventing paths)
  - re-execs under `<repo>/.venv/bin/python3` if launched otherwise (Jensen's exec
    ignored the shebang → system python 3.9, no numpy/PIL)
  - `strip_fillers` EDL flag → expands coarse ranges via `filler_cuts.py` →
    `edl.effective.json` (what RENDER + self-eval use)
  - `--restage strategy|edl|render|self_eval`
  - `--eval-verdict pass` from an agent-started run is **refused unconditionally**
    (Beast fabricated a frame-by-frame review to pass); needs `--reviewer <name>`
    from a human/Claude Code; `--eval-verdict fail` stays open to agents
  - schema: `beat`/`quote` optional per range; accepts `strip_fillers`
- `helpers/render.py` — `--validate-only` + `--validate-only --json`; validator
  honours `omissions` (an entry only has to **overlap** a removed span; a
  filler-only removed gap is auto-allowed); all transcript lookups resolve
  `transcripts/<Path(sources[key]).stem>.json` (agents key `"C0103"`); empty SRT →
  render drops subtitles with a warning instead of an ffmpeg-183 crash.
- `helpers/_job_lock.py` — worker notify: resolves the gateway token from
  `~/.openclaw[-<profile>]/service-env/*.env`, passes `--token`, prints
  `[notify] …` to the job log (was a silent `except: pass`); a `--notify-profile`
  with no real install (`service-env/` absent) falls back to the default profile.
- `helpers/filler_cuts.py` — NEW. `expand_edl()` splits each coarse range around
  every filler word (`check_fillers.py` FILLER_WORDS + verbatim timestamps, ±40 ms,
  drop <80 ms sub-ranges, ignore <50 ms artifacts).
- `SKILL.md` (canonical) + both scoped copies (`~/.openclaw{,-unleashed}/skills/
  video-use/SKILL.md`, byte-identical to each other, NOT to canonical) — process
  table, Hard Rules, "If the pipeline refuses", EDL format (`strip_fillers`,
  `omissions`), SELF_EVAL rules.
- `~/.openclaw/workspace/SOUL.md` + `~/.openclaw-unleashed/workspace/SOUL.md` —
  Video Editing section: drive through `pipeline.py`, don't pass `--edit-dir`,
  context-overflow is recoverable (state on disk), scoped-scripts list incl.
  `filler_cuts.py`. `--notify-profile`: omit for Jensen, `unleashed` for Beast.
- exec-approvals: `pipeline.py` + `filler_cuts.py` allowlisted `--agent "*"` for
  Jensen + Beast; both audit baselines regenerated; audit clean.

**Beast run — COMPLETE end-to-end through the pipeline (criterion 4):**
`~/Desktop/video-use/test-beast/edit/final.mp4` — 57.5 s, background swapped to the
AVIF (verified against source frames), 18 fillers stripped (0 standalone UM/UH in
the SRT), captions burned, repetitive outro dropped. Reviewed by Claude Code
(`eval/eval_review.md`, `--reviewer claude-code`), verdict PASS with two noted
items: (1) one cut boundary ~32.48 s has a suspicious audio transient — worth a
spot-listen; (2) faint RVM matte edge artifact near the subject's left side.
Asterisks: the STRATEGY `## User confirmation` is agent-asserted (gate can't verify
it's a real quote — Beast wrote "Yes. Proceed."); SELF_EVAL needs a sighted
reviewer. `project.md` in that dir has a duplicate session block (the fabricated
pass + the real one) — cosmetic.

**Jensen run — PARKED (criterion 5 not met):**
`~/Desktop/video-use/test-jensen/edit/` at phase INGEST, `inventory.json` written,
`transcripts/` empty, transcribe lock `status: failed`. Killed a whisper retry
storm (~4–5 attempts, whisper dying at 0 % with killed-multiprocessing signatures,
host load ~6–7). Not a pipeline bug — host whisper instability. Resume later with
`pipeline.py /Users/mikeattreys/Desktop/video-use/test-jensen/edit` when whisper is
stable; it will relaunch transcription off the failed lock. (The AVIF *is* in
`test-jensen/` now.)

## 3. Files being touched

- `helpers/pipeline.py`, `helpers/render.py`, `helpers/_job_lock.py`,
  `helpers/filler_cuts.py` (new) — all committed.
- `SKILL.md` + `~/.openclaw/skills/video-use/SKILL.md` +
  `~/.openclaw-unleashed/skills/video-use/SKILL.md` — the two scoped copies MUST
  stay byte-identical to each other (`diff` then `cp`), NOT to canonical.
- `~/.openclaw/workspace/SOUL.md` + `~/.openclaw-unleashed/workspace/SOUL.md`.
- `~/.openclaw/workspace/reference/exec-approvals-baseline-{jensen,unleashed}.json`.
- `plans/OBJECTIVE.md`, `PIPELINE-DRIVER-DESIGN.md`, `HANDOFF.md`.
- Test data (not committed): `~/Desktop/video-use/test-beast/` (completed run in
  `edit/`) and `~/Desktop/video-use/test-jensen/` (parked run in `edit/`). Both
  have `IMG_4328.MOV` + the AVIF.
- Stray `~/.openclaw-jensen/` (empty `state/`+`tmp/`, autocreated by Jensen's bad
  `--notify-profile jensen`) — safe to `rm -rf`.

## 4. What's been tried that failed

- **Prose Hard Rules alone** — violated repeatedly; the reason the driver exists.
- **`check_fillers.py` as a standalone helper** — an agent that never runs it is
  unaffected. The driver runs it in INGEST and the briefing is the only filler data.
- **`--eval-verdict pass` gated only by `eval_review.md` existence+length** — Beast
  fabricated a full, confident, false frame-by-frame review to satisfy it. Now an
  agent-started run cannot pass SELF_EVAL at all; needs `--reviewer`.
- **Requiring omissions to exactly bound the removed span** — a token-limited model
  keeps declaring the *content* span not the arithmetic gap. Relaxed to "overlap".
- **Assuming `--notify-session` worked** (2026-09-07 "verified") — that test had the
  token in the shell env; from a real worker it was a silent auth failure.
  Attributing an agent's ~80 s resume to the notify — it was a heartbeat catch.
- **Assuming `final.mp4`'s room background = the original** — it was the AVIF; the
  swap worked. Always extract a *source* frame before claiming a render defect.
- **LLM hand-authoring a 15–20 range filler-removal EDL** — the local model runs
  away on reasoning and truncates the JSON. Hence `strip_fillers` + coarse ranges.
- **Parallel Jensen+Beast runs** — Mike rejected (CPU contention). Sequential.
- **Hand-editing an EDL / restaging state to force a run through** — Mike's explicit
  rule: if you or he has to edit something, the process is broken. Fix the class.

## 5. What to do next

1. **Close the objective (needs Mike).** Walk `plans/OBJECTIVE.md` criterion by
   criterion. The 3 OPEN directives in the log ("use a script", "the more robust
   fix", "make it work beginning to end") are satisfied — the driver exists and ran
   a full Beast edit. **Mike moves those to CLOSED; Claude never does.** Decide
   whether criterion 4's asterisks (agent-asserted strategy confirmation, sighted
   reviewer needed for self-eval) count as "done" or need `describe_frames.py` first.
2. **Retry Jensen (criterion 5)** when host whisper is stable: `rm -rf
   ~/.openclaw-jensen`, then `pipeline.py /Users/mikeattreys/Desktop/video-use/test-jensen/edit`
   (it resumes off the failed transcribe lock). Jensen needs per-step nudges — it
   does not autonomously drive the process (same model limitation as Beast).
3. **Spot-listen** to `~/Desktop/video-use/test-beast/edit/final.mp4` at ~32.48 s
   for the possible audio pop noted in `eval/eval_review.md`.
4. **Backlog (in `OBJECTIVE.md`, not blocking):**
   - `helpers/describe_frames.py` — POST the SELF_EVAL frames to the LM Studio
     vision model (`qwen3.6-35b-a3b-mlx-vl-oq8`), write `eval/eval_review.md`; then
     `pipeline.py` can accept it as the reviewer and agent runs become autonomous
     through SELF_EVAL. Its own design pass (persistent-load vs JIT, prompt).
   - The **global anti-drift system** (agreed, never started): a CLAUDE.md rule
     (instruction-capture, 2-detour drift stop, "done" = walk each criterion with
     evidence, Mike signs off) + a SessionStart hook that surfaces
     `plans/OBJECTIVE.md` when it exists. **Scoping clause is load-bearing:
     triggered, not default — no `OBJECTIVE.md` file → normal session, zero
     overhead.**
   - `render.py --build-subtitles --drop-fillers`; strip the double session block
     in `test-beast/edit/project.md`; a `describe`-based check that the render's
     background/grade visibly took effect.
