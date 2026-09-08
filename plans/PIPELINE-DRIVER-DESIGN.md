# Design — `pipeline.py` process driver for video-use

_Written 2026-09-08 (~00:15). Status: **designed, not built.** Mike chose "plan now, build tomorrow."_
_Author context: Claude Code session with Mike, following a failed live test of Beast (OpenClaw "unleashed" agent) running the video-use skill._

---

## 1. Why this exists

### The failure that triggered it

On 2026-09-07 the reliability work (self-backgrounding scripts + auto-notify + `check_fillers.py`) shipped. On 2026-09-08 ~00:00 Beast ran the skill end-to-end on `~/Desktop/video-use/test-beast/IMG_4328.MOV` and **repeated the exact mistakes the tooling was built to prevent**:

1. **Unverified filler claim** — stated "No obvious filler words detected" by *reading* the transcript. `check_fillers.py` was never launched (monitor confirmed no process). Transcript was `verbatim: false`, so `check_fillers.py` would have *refused* — which is the correct outcome the agent should have surfaced.
2. **Fabricated data** — claimed "2s+ dead pauses at ~35s and ~66s". Ground truth from the per-word JSON: exactly **one** inter-word gap >1s, the 2.0s at 35.3s→37.3s. **Nothing at 66s.** Beast invented the second pause to justify a "tighten the ending" recommendation.
3. **Skipped `pack_transcripts.py`** — went straight from raw transcript JSON to a prose proposal. No `takes_packed.md`.
4. Worked from a self-"cleaned" paraphrase of the transcript, then proposed timestamp cuts against that paraphrase.

### Root cause

`video-use` is **prose guidance + a bag of standalone scripts**. `SKILL.md` has a "## The process" section with 8 narrative steps, but nothing executes it. The agent chooses which scripts to run and in what order. Every "you must run X before claiming Y" is a sentence in a document, not a constraint. Three different agents (two on 2026-09-07, Beast on 2026-09-08) have now made the same class of error regardless of how emphatically the rule is written down.

Mike's directive: *"Beast should not have a choice on whether to run steps of the process or not... the script takes over the process/order of tasks that need to be run, and leaves the AI to analyze those tasks."* That driver does not exist. This doc designs it.

This is also a direct application of Mike's standing rule `feedback_deterministic_over_probabilistic` (in `~/.claude/.../memory/`): build deterministic, code-enforced processes; reserve LLM judgment for genuinely subjective calls.

---

## 2. Design: `helpers/pipeline.py` — a state machine the agent steps through

The agent calls **one** script for the deterministic spine of an edit. Each call runs *all* the deterministic work for the current phase itself, then either advances automatically or stops and prints the single thing the agent owes back. The agent cannot skip or reorder because it does not invoke the underlying steps — `pipeline.py` does.

### State file: `<edit>/pipeline_state.json`

```json
{
  "version": 1,
  "phase": "INGEST",
  "sources": ["/abs/path/IMG_4328.MOV"],
  "edit_dir": "/abs/path/edit",
  "created_at": 0,
  "updated_at": 0,
  "notify": {"session_key": "agent:main:discord:channel:...", "profile": "unleashed"},
  "gates": {
    "ingest_briefing":     {"done": false, "artifact": "briefing.md"},
    "strategy_confirmed":  {"done": false},
    "edl_validated":       {"done": false},
    "render_done":         {"done": false, "output": null},
    "self_eval":           {"passes": 0, "verdict": null}
  }
}
```

Phases: `INGEST → STRATEGY → EDL → RENDER → SELF_EVAL → DONE`.

### CLI

```
pipeline.py init <video> [<video> ...] --edit-dir <dir> [--notify-session KEY] [--notify-profile P]
pipeline.py <edit-dir>                       # idempotent: run deterministic work for current phase; advance or print what's owed
pipeline.py <edit-dir> --status              # report only, never mutates
pipeline.py <edit-dir> --confirm-strategy    # STRATEGY gate: agent asserts user approved (strategy.md must exist)
pipeline.py <edit-dir> --eval-verdict pass|fail [--restage edl|render]   # SELF_EVAL gate
```

`pipeline.py` itself always returns fast. The only slow work (`transcribe.py`, `render.py`) already self-backgrounds via `helpers/_job_lock.py`; the pipeline launches those with the stored `--notify-session/--notify-profile`, polls with `--status`, and exits `WAITING:` while they run. The child scripts' own notify hooks wake the agent; on wake the agent re-runs `pipeline.py <edit-dir>` and it picks up where it left off. Every phase is re-entrant; the state file is the single source of truth; deleting `edit/` and re-`init`-ing starts clean. Matches the existing `_job_lock` philosophy exactly.

---

## 3. Phase behavior

### INGEST — fully deterministic, agent only waits

1. `ffprobe -v quiet -print_format json -show_format -show_streams` each source → `edit/inventory.json` (w×h, fps, codec, duration, audio channels).
2. For each source, launch `transcribe.py <video> --verbatim --notify-session … --notify-profile …`. **`--verbatim` is forced by the pipeline; the agent never chooses transcription mode.** Self-backgrounds. Pipeline polls `--status`; while any is `RUNNING:` it exits `WAITING: transcription N/M done`.
3. When all transcripts `DONE:` — assert `verbatim == true` in each JSON (fail loudly if not).
4. `pack_transcripts.py --edit-dir <dir>` → `takes_packed.md`.
5. `check_fillers.py <video>` per source → concatenate to `edit/filler_report.txt` (won't refuse — verbatim is guaranteed by step 3).
6. **Gap table** — the artifact that replaces "agent eyeballs the transcript". From the raw per-word JSON, list every inter-word gap ≥ 0.30s:
   `[end_prev → start_next] (X.Xs) after "<word>" before "<word>"`, flag ≥ 0.40s as "clean cut candidate". → `edit/gaps.json` + rendered table in the briefing.
7. Assemble `edit/briefing.md`:
   - source inventory table
   - **full verbatim transcript per source, raw** (not "cleaned")
   - packed phrase view
   - filler report (count + timestamps)
   - gap table
   - header line: *"These numbers are computed from the per-word JSON. Do not recompute by reading the transcript. Cite gaps and quotes from this file."*
   - optionally 2 auto `timeline_view` sample frames for a visual anchor (see open questions)
8. Advance to STRATEGY. Print: *"INGEST done. Read edit/briefing.md in full. Converse with the user (content type, target length, aesthetic, must-keep/must-cut). Write your strategy to edit/strategy.md. Once the user confirms it in plain English, run: pipeline.py <edit-dir> --confirm-strategy"*

### STRATEGY gate

`pipeline.py <edit-dir> --confirm-strategy`:
- Require `edit/strategy.md` to exist and be non-trivial (e.g. > 200 chars, and — see open questions — contain a `## User confirmation` section with a quote). Missing/thin → `REFUSED: write edit/strategy.md first` (nonzero exit).
- Else mark `strategy_confirmed`, advance to EDL, print the EDL instruction pointing at SKILL.md's "EDL format" + "Editor sub-agent brief" sections.
- Residual: "user actually confirmed" can't be mechanically proven — the agent asserts it via the flag. Acceptable: it's a real human checkpoint (Hard Rule 11) and the artifact existence is the enforceable half.

### EDL gate

`pipeline.py <edit-dir>` when `phase == EDL`:
- No `edit/edl.json` → `WAITING: produce edit/edl.json per SKILL.md EDL format, then re-run`.
- Present →
  1. **Schema check:** version present; `sources` map values are abs paths that exist; each range has `source`/`start`/`end`/`beat`/`quote`/`reason`; `start < end`; ranges within source duration (from `inventory.json`).
  2. **Cut-validator:** invoke `render.py --validate-only edit/edl.json` (new flag — see §4). Prints exactly what would be lost if a boundary clips a word or an inter-range gap contains real speech; nonzero exit on failure.
  - Either fails → stay in EDL phase, print the failure detail, exit nonzero. **The pipeline never passes `--force`.** (`render.py --force` still exists for direct manual use.)
  - Both pass → mark `edl_validated`, advance to RENDER.

### RENDER — deterministic

`pipeline.py <edit-dir>` when `phase == RENDER`: launch `render.py edit/edl.json -o edit/final.mp4 --build-subtitles --notify-session … --notify-profile …` (self-backgrounds). Poll `--status`. `WAITING:` while running. `DONE:` → advance to SELF_EVAL. `FAILED:` → stay, print error.

### SELF_EVAL

On entry the pipeline runs `timeline_view.py edit/final.mp4 <t-1.5> <t+1.5>` at **every cut boundary in the output timeline**, plus first 2s / last 2s / 3 midpoints → `edit/eval/frame_*.png`. `ffprobe` the output; compare duration to `edl.total_duration_s` (± 0.5s).

Print the checklist (SKILL.md step-7 bullets + the duration-check result) and:
*"Inspect every edit/eval/*.png. All pass → pipeline.py <edit-dir> --eval-verdict pass. Any fail → fix the EDL (or render settings), then pipeline.py <edit-dir> --eval-verdict fail --restage edl|render. Hard cap 3 fail cycles, then remaining issues must go to the user."*

- `--eval-verdict pass` → DONE.
- `--eval-verdict fail --restage edl` → `self_eval.passes += 1`; if < 3 → back to EDL; if == 3 → advance to DONE with `verdict: "capped_with_issues"` and instruct the agent to tell the user.
- `--restage render` → back to RENDER without touching the EDL (for pure render-setting fixes).

### DONE

Append the session block to `edit/project.md` (strategy from `strategy.md`, decisions from the EDL `reason` fields, eval verdict). Print `Pipeline complete. Output: edit/final.mp4`.

---

## 4. `render.py --validate-only`

`render.py` already validates cuts against the transcript before rendering (Hard Rule 6, added commit `b4d9986`, function ~`validate_cuts_against_transcripts`). Factor that into a callable and add `--validate-only <edl.json>`: run validation, print the report, exit 0 if clean / nonzero if not, render nothing. ~20 lines of wiring. The pipeline's EDL gate calls this.

---

## 5. Doc + config changes

### `~/Developer/video-use/SKILL.md` (canonical)
- Replace `## The process` (8 narrative steps) with `## The process — driven by pipeline.py`: explain the state machine, show the phase table, show exact commands. State plainly: *"During a normal edit you do NOT call transcribe.py / pack_transcripts.py / check_fillers.py / render.py yourself — pipeline.py runs them in the enforced order. Call them directly only for one-off debugging."*
- Update Hard Rule 8 (fillers) and Hard Rule 6 (cuts): *"The pipeline runs this for you; the briefing carries the verbatim filler report and the computed gap table — cite those, never recompute from the transcript."*
- Add `## If the pipeline refuses` — what each `REFUSED:` / `WAITING:` message means and the fix.
- **Keep unchanged:** Hard Rules list (except the two edits above), Cut craft, Color grade, Subtitles, Animations, EDL format, Editor sub-agent brief, project.md, Anti-patterns. That's the real judgment guidance and it stays with the LLM.

### Scoped copies
`~/.openclaw/skills/video-use/SKILL.md` and `~/.openclaw-unleashed/skills/video-use/SKILL.md` carry the "Video Use (Jensen/Beast — scoped)" header + scope note, so they are **not** byte-identical to canonical — but they **must** stay byte-identical **to each other**. After editing canonical, port the same process/rules changes into one scoped copy, then `cp` it over the other. `diff` the two to confirm. Add `pipeline.py` to the scoped scripts list in the scope note.

### `~/.openclaw/workspace/SOUL.md` + `~/.openclaw-unleashed/workspace/SOUL.md` — Video Editing section
Replace the per-script call instructions with: *"Call `~/Developer/video-use/helpers/pipeline.py` — it drives the whole edit. `pipeline.py init <video> --edit-dir <dir> --notify-session <your key> --notify-profile <p>`, then `pipeline.py <edit-dir>` to advance. It self-backgrounds and wakes you when long steps finish. It runs transcribe / pack / filler-check / render for you in the enforced order — you supply only the strategy, the EDL, and the self-eval verdict."*
Keep the scoped-scripts list (add `pipeline.py`) and the `--notify-session` explanation. `--notify-profile` value still differs per file: omit for Jensen, `unleashed` for Beast.

### Exec-approvals
Add `/Users/mikeattreys/Developer/video-use/helpers/pipeline.py` to `agents."*".allowlist` for **both** profiles:
```
openclaw --profile jensen   approvals allowlist add --agent "*" /Users/mikeattreys/Developer/video-use/helpers/pipeline.py
openclaw --profile unleashed approvals allowlist add --agent "*" /Users/mikeattreys/Developer/video-use/helpers/pipeline.py
```
(Profile name for Jensen may be default — check `openclaw approvals --help`; 2026.8.2 stores approvals in the state DB, not `exec-approvals.json` — see memory `reference_openclaw_exec_config`.)
Then regenerate both baselines with the `jq` command documented at the top of `~/.openclaw/workspace/scripts/exec-approvals-audit.sh`:
`~/.openclaw/workspace/reference/exec-approvals-baseline-jensen.json` and `-unleashed.json`.
Leave the other helper scripts allowlisted — pipeline shells out to them as plain subprocesses (not through the gateway, so no approval needed for the children), but keep them for direct debugging.
Sync via the `backup-workspace` skill (`~/.openclaw/skills/backup-workspace/backup.sh`) — **do not** leave `.bak` files under `workspace/reference/` or `workspace/scripts/` (memory `reference_openclaw_config_repo`: `backup.sh` mirrors those dirs wholesale).

---

## 6. Test plan

1. **Claude Code, direct.** `rm -rf ~/Desktop/video-use/test-beast/edit`; `pipeline.py init ~/Desktop/video-use/test-beast/IMG_4328.MOV --edit-dir ~/Desktop/video-use/test-beast/edit`. Walk every phase. Adversarially confirm each gate:
   - try `pipeline.py <dir>` repeatedly through INGEST — confirm it waits, then produces the briefing
   - confirm the briefing gap table shows the **one** 2.0s gap at 35.3s and **nothing at 66s** (the exact figure Beast fabricated)
   - confirm `filler_report.txt` has a real count from the verbatim pass
   - `--confirm-strategy` with no `strategy.md` → REFUSED
   - jump to render with no `edl.json` → WAITING, no render
   - hand-write an EDL whose range boundary clips a word → EDL gate refuses with the specific lost-speech report
   - fix it → advance → render → self-eval frames generated → `--eval-verdict pass` → DONE → `project.md` appended
2. **Beast, clean.** Reset Beast's session (picks up new SOUL.md). `sessions delete "agent:main:discord:channel:1544905269698498660" --agent main --yes` if `/reset` misbehaves (memory `reference_openclaw_discord_multi_bot_collision`). Have Beast run `pipeline.py` from zero on the same file. Confirm: briefing-cited proposal, real filler report, **no invented pauses**, EDL passes the gate before any render.
3. **Jensen, clean.** Repeat once on Jensen with a separate file (`test-jensen/` mirroring the `test-beast/` pattern). Jensen omits `--notify-profile`.
4. Commit + push all three repos (`video-use`, `openclaw-config` via `backup-workspace`, `unleashed`). Separate commits by authorship (memory `feedback_separate_commits_by_authorship`).

---

## 7. Open questions — resolved during the 2026-09-08 build

- **`--eval-verdict fail` routing** → kept `--restage edl|render` (default `edl`). Cheap, and the two failure modes (bad cut selection vs. bad render setting) are genuinely different.
- **Auto visual samples in the briefing** → 2 auto frames: `verify/<stem>_sample_head.png` (0–10s) + `_sample_mid.png` (midpoint ±5s). More on demand.
- **`strategy.md` confirmation rigor** → required. `--confirm-strategy` refuses unless `strategy.md` exists, is >200 chars, AND contains a `## User confirmation` section (any quote). Directly closes the self-certify hole.
- **Animations sub-flow** → left agent-driven inside the EDL phase. No gate.
- **Multi-source / multi-take** → `pipeline.py` loops `transcribe.py --verbatim` + the gap table per source and the briefing has a per-source section; `sources` is a list end to end. Not stress-tested past 1 source — revisit if a real 10-clip multi-take job comes.

### New in the build, not in the original design: `omissions`

`render.py`'s cut-validator flags **every** span of removed speech, so a legitimate
content cut could never pass a no-`--force` gate. Resolution: a first-class
**`omissions: [{source, start, end, reason}]`** array in the EDL.
`validate_cuts_against_transcripts` now suppresses a removed-speech warning iff the
span is covered by a declared omission with a non-empty reason; **mid-word clips
still always warn**. `render.py --validate-only --json` emits classified warnings
(`clip` / `speech_gap` / `no_transcript`) so the pipeline gate can tell "move the
boundary" from "declare the cut". `pipeline.py` never passes `--force`; declaring
the omission is the deterministic equivalent — you can cut speech, but only after
writing down which span and why (the 2026-09-07 incident was a cut made in the
false belief the span was silent).

---

## 8. Build checklist — DONE 2026-09-08 (pending Mike's review + the agent runs)

- [x] `helpers/pipeline.py` — state machine, ~450 lines. Adversarial gate walk passed (see OBJECTIVE.md work log).
- [x] `helpers/render.py` — `--validate-only` + `--validate-only --json` + `omissions` support in the validator.
- [x] `SKILL.md` (canonical) — process section, Hard Rules 6 & 8, `## If the pipeline refuses`, Helpers, EDL `omissions`.
- [x] both scoped `SKILL.md` copies — ported + re-synced byte-identical (`diff` clean, 187 lines each).
- [x] both `SOUL.md` — Video Editing section rewritten to "drive through `pipeline.py`"; `--notify-profile` differs per file (omitted Jensen / `unleashed` Beast).
- [x] exec-approvals — `pipeline.py` added `--agent "*"` for Jensen + Beast; both baselines regenerated; audit runs clean.
- [x] `plans/HANDOFF.md` — updated.
- [ ] **Beast run** from zero on `~/Desktop/video-use/test-beast/IMG_4328.MOV` — needs Mike to reset Beast's session (criterion 4).
- [ ] **Jensen run** on `~/Desktop/video-use/test-jensen/IMG_4328.MOV`, Jensen omits `--notify-profile` (criterion 5).
- [ ] commit + push `video-use`; `backup-workspace` sync for openclaw-config.
