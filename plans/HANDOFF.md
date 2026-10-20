# Handoff — video-use: style-sample gate, audio denoise, version history, OBS/style-guide docs

_Last updated: 2026-10-19T20:05:00Z_

## 1. Goal

Mike found a creator's published AI-video-editing workflow ("AI with Kyle" —
prompt-only, stock Codex/Claude Code + Whisper/FFmpeg/Remotion/HyperFrames, no
code-enforced correctness) and a Claude-desktop chat about creator recording
tools (OBS, Descript, DaVinci Resolve). He asked for a comparison against
`video-use` and then asked to implement every improvement worth taking.
Conclusion of the comparison: `video-use`'s code-enforced pipeline is already
ahead of Kyle's prose-only approach on the things that matter (verbatim ASR +
deterministic filler detection, cut-vs-transcript validation, unattended agent
runs) — nothing there needed copying. Six concrete gaps were real and got
implemented this session, plus one gap from the Claude-desktop chat
(phone/laptop-mic noise with no isolated interface).

## 2. What shipped this session

### `helpers/render.py` — noise reduction, default on
- `extract_segment()` gained `denoise: bool = True`, prepending `afftdn=nf=-25`
  to the per-segment audio filter chain (before the existing 30ms fades).
  Loudness normalization (`loudnorm`, -14 LUFS/-1 dBTP/LRA 11) **already
  existed** — don't re-add it, the actual gap was noise reduction only.
- `--no-denoise` CLI flag mirrors `--no-loudnorm` exactly: threaded through
  `extract_all_segments()`, `_run_render()`, `_build_arg_parser()`, and the
  background-worker relaunch argv list.
- Verified: full render of a real 27s clip completed without an ffmpeg error
  (afftdn filter syntax is valid); `extract_segment`'s source confirmed to
  contain `afftdn` after the edit.

### `helpers/pipeline.py` — new STYLE_SAMPLE phase
- `INGEST → STRATEGY → EDL → STYLE_SAMPLE → RENDER → SELF_EVAL → DONE`.
  `phase_edl()` now transitions to `STYLE_SAMPLE` instead of `RENDER`.
- `_edl_is_stylable(edl)` — true only if `grade` is set (not `none`/empty) or
  `overlays` is non-empty. Captions alone never trigger it (`render.py`
  always burns them via `--build-subtitles`) — gating on that would fire on
  every single edit and become a third rubber-stamp, not a useful check.
- `_build_style_sample_edl(edl, edit_dir)` — picks the one kept range closest
  to 30s (window 15–45s), builds a one-range sub-EDL carrying the same
  grade/background, and re-bases any overlay whose output window falls
  entirely inside that range to the sample's local time. Returns `None` if no
  range qualifies (auto-skip, not a hard failure).
- `phase_style_sample(state, approve)` — renders the sample to
  `<edit>/style_sample.mp4` via `render.py --preview --build-subtitles
  --force`. **`--force` is correct and deliberate here** — found during
  testing: the sample's own validator sees everything outside its one range
  as undeclared removed speech, because that declaration lives in the FULL
  edl.json's `omissions`, which the one-range sample can't restate against
  its own narrower window. The REAL render (`phase_render`) still runs fully
  un-forced against `edl.json`/`edl.effective.json` — Hard Rule 6 is untouched
  for the actual output; `--force` is scoped to this one internal, throwaway
  preview artifact only.
- **Agent runs (`running_as_agent(state)`) always skip this phase outright,
  styled or not** — deliberately NOT inserted into the unattended path. Mike's
  2026-09-09 decision removed every mid-run human gate from Jensen/Beast on
  purpose ("review the finished video, not the plan"); re-adding one here
  would relitigate that call for no reason. Unit-tested directly (see below).
- `--approve-style` CLI flag, same family as `--confirm-strategy`/
  `--eval-verdict` (REFUSED outside STYLE_SAMPLE phase).
- `do_restage()`: restaging to `strategy` or `edl` now also resets the
  `style_sample` gate and deletes `style_sample.{mp4,edl.json}` — a sample
  approved against the old EDL is stale once grade/overlays change.
- `do_watch()`'s phase dispatch gained a `STYLE_SAMPLE` branch (uses the
  existing `_watch_job_phase` helper) so an agent's watcher passes through it
  the same mechanical way it already does INGEST/RENDER.
- `_fresh_gates()` gained `"style_sample": {"done", "approved", "path"}`.
  `print_status`'s `owed` dict and `ADVANCE` dict both updated.

### `helpers/pipeline.py` — versioned correction history
- `_invalidate_render(edit_dir, state)` (signature gained `state`): before
  clearing `final.mp4` on a restage, it's renamed to
  `<edit>/exports/vN_<timestamp>.mp4` instead of deleted outright.
  `final.mp4` stays the one current/canonical render everywhere else in the
  pipeline (SELF_EVAL, the DONE ping, `project.md`) — `exports/` is purely a
  comparable history, not a second canonical output. `state["version_counter"]`
  (new field, starts at 1 in `do_init`) tracks the next version number;
  missing on old on-disk states, handled via `.get(..., 1)` — no migration
  needed.
- Borrowed from Kyle's workflow: "keep the previous preview, version the new
  one" — but adapted to not disturb the existing single-`final.mp4` contract
  the rest of the state machine assumes.

### Full real smoke test — this session, not simulated
Ran the actual pipeline end-to-end on a real 2:51 clip
(`~/Desktop/IMG_4345.MOV`, copied into scratch) through every new code path:
init → INGEST (real faster-whisper transcription) → STRATEGY → EDL (grade
`neutral_punch`, one range + 2 omissions) → **STYLE_SAMPLE** (hit the
`--force` bug above, fixed it, re-ran — produced a real 27.1s `style_sample.mp4`,
correct dimensions/codec) → `--approve-style` → **RENDER** (real `final.mp4`,
denoise filter present and ran without ffmpeg error) → SELF_EVAL (duration
check OK) → **`--restage edl`** (confirmed `final.mp4` archived to
`exports/v1_<ts>.mp4`, `style_sample.mp4` deleted, gate reset,
`version_counter` → 2) → re-validated EDL (confirmed it cycles back through a
fresh STYLE_SAMPLE) → set `grade: "none"` (confirmed auto-skip straight to
RENDER, no sample built). Also unit-tested `phase_style_sample` directly with
a fake `notify.session_key` against a *styled* EDL to confirm the agent-run
bypass fires regardless of styling. All paths passed.

### Documentation
- Canonical `SKILL.md`: phase diagram/table, new "Style sample" and
  "Corrections keep history" sections, "Audio cleanup" note under the
  `render.py` helper bullet, new "Learning style from reference clips"
  sub-workflow (writes a project-level `<videos_dir>/style-guide.md`, sibling
  of `edit/` so it persists across every edit in that folder — distinct from
  `strategy.md`/`project.md`, which are per-edit), a "Treatment by rhetorical
  purpose" lookup table in Animations, an "editor meta-commentary" cut
  category note in Cut craft, and an OBS recording note under Setup.
- `install.md`: new "Recording footage (optional — OBS)" section. Key framing:
  OBS needs **zero** pipeline changes because its own Scenes/Sources
  compositor does webcam+screen+mic layering live, at record time — the
  export is still one flat file, same as a phone video. True multi-track
  *edit-time* compositing (two separate unsynced files layered in the EDL)
  does **not** exist and isn't being built; said so explicitly so a future
  session doesn't assume it's there.
- Scoped `SKILL.md` (`~/.openclaw/skills/video-use/` +
  `~/.openclaw-unleashed/skills/video-use/`, kept byte-identical per the
  established rule): STYLE_SAMPLE row added to the phase table framed as "you
  never see this gate, period"; denoise/`--no-denoise` mentioned on the
  `render.py` bullet; `exports/vN` auto-archiving mentioned on the DONE row;
  editor-meta-commentary cut category added to Cut craft; a short note that
  `style-guide.md` (if Mike creates one via Claude Code) is worth reading on
  startup, but building one is Claude-Code-only (requires viewing frames —
  Jensen/Beast are text-only). Deliberately **omitted** from scoped copies:
  the reference-clip-inspection workflow itself (no vision), the
  rhetorical-purpose animation table (scoped agents only composite pre-made
  overlay files, they don't author animations), and the OBS section (a
  human/Claude-Code setup task, not something Jensen/Beast do).

## 3. Files touched

- `helpers/render.py` — denoise (see above). Not yet committed.
- `helpers/pipeline.py` — STYLE_SAMPLE phase + versioned exports (see above).
  Not yet committed.
- `SKILL.md` (canonical) — doc updates (see above). Not yet committed.
- `install.md` — OBS section. Not yet committed.
- `~/.openclaw/skills/video-use/SKILL.md` + the Beast copy (kept identical) —
  scoped doc updates. These live outside this repo (symlink targets) — no git
  commit applies to them directly; check whether `openclaw-config` /
  `unleashed` need a matching commit (per
  [[feedback_separate_commits_by_authorship]] from memory, likely a separate
  commit from this repo's).
- This file.

## 4. What's been tried that failed (this session)

- **First style-sample render attempt without `--force`** — failed with
  `CUT VALIDATION FAILED` because the one-range sample sub-EDL has no
  `omissions` of its own; the full edl.json's omissions don't carry over
  automatically. Fixed by adding `--force` to specifically this internal
  render call, with a comment explaining why it's the sanctioned exception
  (see "STYLE_SAMPLE" above) — the real render path is untouched.

## 5. What to do next

1. **Commit + push.** Nothing in this session has been committed yet — all
   edits are live on disk only. Canonical repo (`video-use` fork) first; then
   decide whether the scoped `SKILL.md` copies need a separate commit in
   `openclaw-config` (Jensen) / `unleashed` (Beast) — they're symlink targets
   outside this repo's tree.
2. **One real STYLE_SAMPLE run on an actual deliverable edit** (not the
   synthetic smoke-test EDL used this session) — confirm the 15–45s
   range-selection heuristic picks a genuinely representative passage on a
   real multi-range cut, not just a contrived single-range test case.
3. **No live agent (Jensen/Beast) run has exercised STYLE_SAMPLE for real** —
   the agent-bypass was unit-tested directly (`phase_style_sample` called with
   a fake `notify.session_key`), not observed through an actual OpenClaw
   `init --notify-session` → watcher → DONE cycle. Worth doing once, low risk
   given the bypass is a simple early-return mirroring the already-proven
   `running_as_agent()` pattern used by `--confirm-strategy`/`--eval-verdict`.
4. **`style-guide.md` workflow is documented, not yet used on a real
   project.** First real use will surface whether the "sibling of `edit/`"
   placement and the observed-vs-proposed split actually work in practice.
5. **OBS section is pure documentation** — nobody has actually recorded a
   test clip with it yet. No action needed unless Mike tries it and hits a
   snag.
6. Carried over from the previous handoff, still not done: "one clean
   unattended pipeline run from scratch" on Beast with zero terminal use, and
   deciding whether `watch`/`summarize` adopt faster-whisper.
