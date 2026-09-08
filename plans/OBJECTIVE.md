# Objective — video-use: a code-enforced editing process

_Opened 2026-09-08. **Items are CLOSED by Mike only.** Claude may mark
`DONE-pending-review` but never `CLOSED`. This file is append-only for the
directives log and work log — do not delete history from those sections._

---

## The goal (Mike's words, 2026-09-07)

> "What can we change in this video-use skill to make it more deterministic instead
> of probabilistic? ... I am trying to create working processes no matter the tools
> required (opensource preferred of course). I am NOT trying to only use AI to see
> what it can do. I am trying to create processes that work regardless of tools
> required."

**Applied here:** the video-use editing process must enforce step order in code. An
agent must not be able to skip a step, reorder steps, work from the wrong artifact,
or self-certify that a step happened. The LLM is left only the genuinely subjective
calls: cut selection, pacing, grade direction, caption style, animation design.

---

## Acceptance criteria — each must be VERIFIED (ideally adversarially), not just built

1. An agent cannot produce a filler-word claim unless `check_fillers.py` actually ran
   on a verbatim transcript — verified by attempting the claim in a real agent run and
   being unable to make it without the tool output.
2. An agent cannot reach the render step without an `edl.json` that passed the
   cut-validator — verified by feeding a deliberately word-clipping EDL and being
   blocked with the specific lost-speech report.
3. An agent cannot skip the verbatim transcript, `pack_transcripts.py`, or the
   silence-gap analysis — verified: the briefing artifact is produced by the driver,
   and the agent's proposal cites it rather than recomputing.
4. Beast runs the full process start-to-finish on `~/Desktop/video-use/test-beast/IMG_4328.MOV`
   with zero step-order mistakes and no fabricated data — verified by a clean monitored run.
5. Same for Jensen on a separate file.
6. The process is documented as driver-led in `SKILL.md` + both scoped copies + both
   `SOUL.md` files, and the driver script is exec-approved for both agents, baselines
   regenerated.

---

## Direct directives log (verbatim, append-only; STATUS set by Mike)

- **2026-09-07 — Mike:** _"start with the code fixes, but I am going to follow your
  suggestion and use a script for this process."_
  → **STATUS: OPEN.** The script (orchestrator/driver that owns step order) is not built.
  Five smaller code fixes shipped; the driver itself did not.

- **2026-09-07 — Mike:** _"Do the more robust fix."_ (Context: the idle-agent gap after
  a background job. The "more robust fix" had just been defined verbatim as _"this exact
  hand-off ... is precisely the kind of thing an orchestrator script should own instead
  of trusting the LLM."_)
  → **STATUS: OPEN.** What was built instead: `--notify-session`/`--notify-profile` flags
  on `transcribe.py`/`render.py` — a point patch, not the orchestrator. **This directive
  is not satisfied.**

- **2026-09-08 — Mike:** _"Do whatever you have to do to make sure this process works
  from beginning to end."_
  → **STATUS: OPEN.** Full design written (`plans/PIPELINE-DRIVER-DESIGN.md`). Build
  deferred to 2026-09-08 by Mike's explicit choice ("plan now, build tomorrow").

---

## Done and verified (components only — none of these enforce step ORDER)

- `_job_lock.py` + self-backgrounding `transcribe.py`/`render.py` — verified live 2026-09-07.
- Auto-notify (`--notify-session`) — verified live, success + failure paths, 2026-09-07.
- `check_fillers.py` exists, refuses non-verbatim transcripts — verified 2026-09-07.

These are building blocks the driver will call. **The objective is not advanced by them
alone** — an agent can still skip all of them. Criteria 1–6 remain unmet.

---

## Work log

_One line per distinct sub-task. Tag each: `ADVANCES <criterion>` / `BLOCKER` / `DETOUR`.
Two consecutive `BLOCKER`/`DETOUR` lines → stop and post a drift check to Mike before
continuing._

- 2026-09-08 00:20 — Wrote `PIPELINE-DRIVER-DESIGN.md` (full driver design) — ADVANCES criteria 1–6 (design stage)
- 2026-09-08 00:35 — Updated `HANDOFF.md` to point at the driver build as the live task — ADVANCES criteria 1–6
- 2026-09-08 00:55 — Reviewed earlier session transcript to establish how the directive was missed — DETOUR (process retro)
- 2026-09-08 01:05 — Created this file; logged the two lost directives as OPEN — ADVANCES (anti-drift infrastructure)
- 2026-09-08 09:1x — Built `render.py --validate-only` (factored the cut-validator into a standalone exit-code gate) — ADVANCES criterion 2
- 2026-09-08 09:2x — Built `helpers/pipeline.py` (full state machine: INGEST→STRATEGY→EDL→RENDER→SELF_EVAL→DONE) — ADVANCES criteria 1,2,3
- 2026-09-08 09:3x — Adversarial test in progress on test-beast/IMG_4328.MOV (verbatim transcription running, ~6min) — ADVANCES criteria 1,2,3
- 2026-09-08 09:5x — Build refinement (discovered in adversarial test): render.py's cut-validator flags ALL removed speech, so a legitimate content cut couldn't pass a "no --force" gate. Added a first-class `omissions:[{source,start,end,reason}]` array to the EDL — render.py's validator suppresses a removed-speech warning iff the span is declared with a reason; mid-word clips still always fail. This is the deterministic replacement for `--force`. `render.py --validate-only --json` added for the gate to consume structured warnings. — ADVANCES criterion 2
- 2026-09-08 10:0x — Adversarial gate walk PASSED: bare-advance/thin/no-confirmation strategy → REFUSED; wrong-phase flag → REFUSED; word-clipping EDL → CUT VALIDATION FAILED with exact lost-speech quote; undeclared vs declared omissions → correctly gated; valid EDL → advanced to RENDER; render launched. — ADVANCES criteria 1,2,3
- 2026-09-08 10:1x — Fixed phase_render re-launch bug (checked status before launching; a completed render was being re-run). Full happy path verified: INGEST→STRATEGY→EDL→RENDER→SELF_EVAL→DONE, project.md appended with strategy+confirmation+cut decisions. eval-fail x3 → capped_with_issues verdict + "don't present as finished". Clean re-init with cached transcript works. — DONE-pending-review: criteria 1,2,3 mechanisms verified adversarially from Claude Code
- 2026-09-08 10:2x — Updated canonical SKILL.md: process section, Hard Rules 6/8, "If the pipeline refuses", Helpers, EDL format + omissions. Next: scoped copies, both SOUL.md, exec-approvals. — ADVANCES criterion 6
- 2026-09-08 10:3x — Propagated: both scoped SKILL.md copies updated (driver process table, Hard Rules 6/8, "If the pipeline refuses", EDL omissions) and re-synced byte-identical; both SOUL.md Video Editing sections rewritten to "drive through pipeline.py"; pipeline.py added to exec-approvals for Jensen + Beast (`approvals allowlist add --agent "*"`); both audit baselines regenerated; audit runs clean, no drift alert. — DONE-pending-review: criterion 6 (docs + approvals)
