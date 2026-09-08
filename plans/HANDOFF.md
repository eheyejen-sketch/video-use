# Handoff — video-use: `pipeline.py` process driver

_Last updated: 2026-09-08T10:40:00-05:00_

## 1. Goal

Make the `video-use` editing process **code-enforced end to end** so an LLM agent
(Claude Code, Jensen, Beast) physically cannot skip a step, reorder steps, work from
the wrong artifact, or self-certify that a step happened. The LLM keeps only the
subjective calls: cut selection, pacing, grade, caption style, animation design.

Driven by the standing rule `feedback_deterministic_over_probabilistic` and by three
failed agent runs (2026-09-07 ×2, 2026-09-08 Beast) where the skill's prose Hard Rules
did not stop an unverified filler claim, a fabricated silence gap, or a skipped step.
Full context: `plans/OBJECTIVE.md` (the charter — has the goal in Mike's words, the
acceptance criteria, and the directives log) and `plans/PIPELINE-DRIVER-DESIGN.md`
(the design + build checklist).

## 2. Current state

**Built and verified from Claude Code (adversarial gate walk passed — see
`OBJECTIVE.md` work log for the full test matrix):**

- `helpers/pipeline.py` — new, ~450 lines. State machine
  `INGEST → STRATEGY → EDL → RENDER → SELF_EVAL → DONE`, state in
  `<edit>/pipeline_state.json`. `init` + phase-advance + `--status` +
  `--confirm-strategy` + `--eval-verdict pass|fail` + `--restage edl|render`.
  Runs ffprobe / `transcribe.py --verbatim` / `pack_transcripts.py` /
  `check_fillers.py` / silence-gap table / `render.py` itself; produces
  `<edit>/briefing.md` as the only filler+gap data the agent sees.
- `helpers/render.py` — added `--validate-only` (exit 0/1, no `-o`) and
  `--validate-only --json` (classified warnings: `clip` / `speech_gap` /
  `no_transcript`). The validator now honours an EDL `omissions` array (see below).
- `SKILL.md` (canonical) + both scoped copies (`~/.openclaw{,-unleashed}/skills/
  video-use/SKILL.md`, byte-identical to each other, 187 lines) — process section
  is now the driver phase table; Hard Rules 6 & 8 point at the briefing; new
  `## If the pipeline refuses`; EDL `omissions` documented.
- Both `SOUL.md` Video Editing sections rewritten to "drive through `pipeline.py`".
  `--notify-profile`: omitted for Jensen, `unleashed` for Beast.
- exec-approvals: `pipeline.py` added `--agent "*"` for Jensen + Beast; both audit
  baselines (`~/.openclaw/workspace/reference/exec-approvals-baseline-{jensen,unleashed}.json`)
  regenerated; `exec-approvals-audit.sh` runs clean, no drift alert.

**The `omissions` refinement (new, discovered mid-build):** `render.py`'s validator
flags *every* removed-speech span, so a real content cut could never pass a
no-`--force` gate. Fix: EDL gets `"omissions": [{source,start,end,reason}]`. The
validator suppresses a removed-speech warning only for a span declared there with a
reason; mid-word clips always fail. `pipeline.py` never passes `--force`.

**Not done:**
- `git commit` / push of `video-use`; `backup-workspace` sync for openclaw-config.
  (Nothing committed yet — `git status`: modified `SKILL.md`, `helpers/render.py`;
  new `helpers/pipeline.py`, `plans/`.)
- **Beast live run** from zero on `~/Desktop/video-use/test-beast/IMG_4328.MOV`
  (`test-beast/edit/` was deleted — clean cold start). Needs Mike to reset Beast's
  session so it picks up the new SOUL.md. (Acceptance criterion 4.)
- **Jensen live run** on `~/Desktop/video-use/test-jensen/IMG_4328.MOV`, sequential
  after Beast (Mike does not want them competing for CPU). Jensen omits
  `--notify-profile`. (Criterion 5.)

## 3. Files being touched

- `helpers/pipeline.py` — the driver (new, uncommitted).
- `helpers/render.py` — `--validate-only`, `--json`, `_classify_cut_warning`,
  `_declared_omission`, validator honours `omissions`; `-o` now optional.
- `SKILL.md` + `~/.openclaw/skills/video-use/SKILL.md` +
  `~/.openclaw-unleashed/skills/video-use/SKILL.md` — the two scoped copies must
  stay byte-identical to each other (`diff` then `cp`), NOT to canonical.
- `~/.openclaw/workspace/SOUL.md` + `~/.openclaw-unleashed/workspace/SOUL.md`.
- `~/.openclaw/workspace/reference/exec-approvals-baseline-{jensen,unleashed}.json`.
- `plans/OBJECTIVE.md`, `plans/PIPELINE-DRIVER-DESIGN.md`, `plans/HANDOFF.md`.
- Test data (not committed): `~/Desktop/video-use/test-beast/IMG_4328.MOV` and
  `~/Desktop/video-use/test-jensen/IMG_4328.MOV` (same file).

## 4. What's been tried that failed

- **Prose Hard Rules alone** (SKILL.md Rules 6, 8, 12, 13) — each added after a real
  incident, still violated. The whole reason for the driver.
- **`check_fillers.py` as a standalone helper** — correct design, but an agent that
  never runs it is unaffected. The driver now runs it automatically.
- **Original design assumed `render.py --validate-only` returns clean for a good
  EDL** — it doesn't; the validator flags all removed speech. Fixed with `omissions`.
- **`phase_render` launched a render before checking status** — a completed render
  got re-run. Fixed: status-check first, only spawn if NOT_FOUND/FAILED.
- **Parallel Jensen+Beast runs** — Mike rejected (CPU contention). Runs are sequential.

## 5. What to do next

1. **Commit + push `video-use`** (Claude-authored: `Co-Authored-By` + `Claude-Session`
   trailers per `feedback_separate_commits_by_authorship`). Then **`backup-workspace`**
   (`~/.openclaw/skills/backup-workspace/backup.sh`) to sync the SOUL.md / scoped
   SKILL.md / baseline changes into openclaw-config — keep NO `.bak` files under
   `workspace/reference/` or `workspace/scripts/`.
2. **Ask Mike to reset Beast's session** (`/reset` via Escape-to-dismiss, or
   `openclaw --profile unleashed sessions delete "agent:main:discord:channel:1544905269698498660" --agent main --yes`).
   Then Mike prompts Beast to edit `~/Desktop/video-use/test-beast/IMG_4328.MOV`.
   Monitor `test-beast/edit/` + video-use processes. Verify: Beast calls
   `pipeline.py init` with `--notify-session … --notify-profile unleashed`; the
   proposal cites `briefing.md` (not a recomputed gap); no fabricated pauses; the
   EDL passes the gate before any render; `check_fillers.py` output is used verbatim.
   → satisfies acceptance criterion 4.
3. **Jensen run** on `~/Desktop/video-use/test-jensen/IMG_4328.MOV`, after Beast is
   done. Jensen omits `--notify-profile`. → criterion 5.
4. When criteria 1–6 are all verified, walk `OBJECTIVE.md` with Mike criterion by
   criterion for sign-off. Claude never marks an item CLOSED — Mike does.
5. Separate deliverable, agreed but not started: the **global anti-drift system**
   (CLAUDE.md rule + SessionStart hook surfacing `plans/OBJECTIVE.md`; triggered,
   not default — see `HANDOFF.md` §4b of the prior version / `OBJECTIVE.md`).
