---
name: video-use
description: Edit any video by conversation. Transcribe, cut, color grade, generate overlay animations, burn subtitles — for talking heads, montages, tutorials, travel, interviews. No presets, no menus. Ask questions, confirm the plan, execute, iterate, persist. Production-correctness rules are hard; everything else is artistic freedom.
---

# Video Use

## Principle

1. **LLM reasons from raw transcript + on-demand visuals.** The only derived artifact that earns its keep is a packed phrase-level transcript (`takes_packed.md`). Everything else — filler tagging, retake detection, shot classification, emphasis scoring — you derive at decision time.
2. **Audio is primary, visuals follow.** Cut candidates come from speech boundaries and silence gaps. Drill into visuals only at decision points.
3. **Ask → confirm → execute → iterate → persist.** Never touch the cut until the user has confirmed the strategy in plain English.
4. **Generalize.** Do not assume what kind of video this is. Look at the material, ask the user, then edit.
5. **Artistic freedom is the default.** Every specific value, preset, font, color, duration, pitch structure, and technique in this document is a *worked example* from one proven video — not a mandate. Read them to understand what's possible and why each worked. Then make your own taste calls based on what the material actually is and what the user actually wants. **The only things you MUST do are in the Hard Rules section below.** Everything else is yours.
6. **Invent freely.** If the material calls for a technique not described here — split-screen, picture-in-picture, lower-third identity cards, reaction cuts, speed ramps, freeze frames, crossfades, match cuts, L-cuts, J-cuts, speed ramps over breath, whatever — build it. The helpers are ffmpeg and PIL. They can do anything the format supports. Do not wait for permission.
7. **Verify your own output before showing it to the user.** If you wouldn't ship it, don't present it.

## Hard Rules (production correctness — non-negotiable)

These are the things where deviation produces silent failures or broken output. They are not taste, they are correctness. Memorize them.

1. **Subtitles are applied LAST in the filter chain**, after every overlay. Otherwise overlays hide captions. Silent failure.
2. **Per-segment extract → lossless `-c copy` concat**, not single-pass filtergraph. Otherwise you double-encode every segment when overlays are added.
3. **30ms audio fades at every segment boundary** (`afade=t=in:st=0:d=0.03,afade=t=out:st={dur-0.03}:d=0.03`). Otherwise audible pops at every cut.
4. **Overlays use `setpts=PTS-STARTPTS+T/TB`** to shift the overlay's frame 0 to its window start. Otherwise you see the middle of the animation during the overlay window.
5. **Master SRT uses output-timeline offsets**: `output_time = word.start - segment_start + segment_offset`. Otherwise captions misalign after segment concat.
6. **Never cut inside a word.** Snap every cut edge to a word boundary from the transcript. **In the normal flow `pipeline.py` hands you the authoritative silence-gap table in `briefing.md` — cite gaps from there, never recompute them by reading the transcript. The EDL gate runs `render.py --validate-only` and will not let you render an EDL where a boundary clips a word or a between-ranges gap contains real speech; it prints exactly what would be deleted.** (Added 2026-09-07 after a real incident: a cut derived by eyeballing `takes_packed.md`'s broad phrase groupings identified two "silence gaps" that actually contained real spoken sentences — one deleted "to create it on my system", the other deleted a full closing sentence.) `render.py` keeps a `--force` for direct manual use after you've read the warnings; **`pipeline.py` never passes `--force` — fix the EDL instead.**
7. **Pad every cut edge.** Working window: 30–200ms. Whisper word timestamps drift 50–100ms — padding absorbs the drift. Tighter for fast-paced, looser for cinematic.
8. **Word-level verbatim ASR only.** Never SRT/phrase mode (loses sub-second gap data). Never normalized fillers (loses editorial signal). **In the normal flow `pipeline.py` forces `transcribe.py --verbatim` and runs `check_fillers.py` for you — the result is in `briefing.md`'s filler report. That report is the ONLY basis for any filler-word statement. If it says `REFUSED` or is empty, you cannot claim anything about fillers.** `check_fillers.py` refuses (nonzero exit, no count) on a non-verbatim transcript rather than let you report a false "none found." This isn't optional caution: three real agent runs (2026-09-07 x2, 2026-09-08) produced a confident but false "no fillers detected" claim from reading a transcript directly — the whole driver exists because that mistake keeps recurring regardless of how clearly the rule is written down.
9. **Cache transcripts per source.** Never re-transcribe unless the source file itself changed. Both `transcribe.py` and `render.py` now self-background and are safe to call repeatedly — see the Helpers section below.
10. **Parallel sub-agents for multiple animations.** Never sequential. Spawn N at once via the `Agent` tool; total wall time ≈ slowest one.
11. **Strategy confirmation before execution.** Never touch the cut until the user has approved the plain-English plan.
12. **All session outputs in `<videos_dir>/edit/`.** Never write inside the `video-use/` project directory.
13. **Quote transcript timestamps verbatim from `takes_packed.md`'s `[SSS.ss-SSS.ss]` format or the raw JSON — never reformat into MM:SS by hand.** A real incident (2026-09-07) converted `14.42` seconds into the string `14:42` by substituting the decimal point for a colon instead of actually computing minutes — numerically wrong even though it looked like a plausible timestamp. If you want MM:SS for readability, compute it properly (`int(t//60)}:{t%60:05.2f`), don't string-substitute.
14. **For any claim about an exact moment in the transcript (a specific quote, a specific timestamp), verify against the raw per-word JSON, not `takes_packed.md`'s phrase-level groupings.** The packed view intentionally merges multiple sentences under one broad `[start-end]` range for fast scanning — good for orientation, not precise enough to cite as fact. A real incident mislabeled a closing line with a timestamp that actually belonged to a different sentence earlier in the same packed phrase block.

Everything else in this document is a worked example. Deviate whenever the material calls for it.

## Directory layout

The skill lives in `video-use/`. User footage lives wherever they put it. All session outputs go into `<videos_dir>/edit/`.

```
<videos_dir>/
├── <source files, untouched>
└── edit/
    ├── project.md               ← memory; appended every session
    ├── takes_packed.md          ← phrase-level transcripts, the LLM's primary reading view
    ├── edl.json                 ← cut decisions
    ├── transcripts/<name>.json  ← cached transcript JSON
    ├── animations/slot_<id>/    ← per-animation source + render + reasoning
    ├── clips_graded/            ← per-segment extracts with grade + fades
    ├── master.srt               ← output-timeline subtitles
    ├── downloads/               ← yt-dlp outputs
    ├── verify/                  ← debug frames / timeline PNGs
    ├── preview.mp4
    └── final.mp4
```

## Setup

First-time install lives in `install.md` (clone, deps, ffmpeg, skill registration, API key). Don't re-run it every session; on cold start just verify:

- Transcription is local Whisper — no API key needed. (Original stock skill assumed ElevenLabs Scribe; this fork replaced that 2026-07-27, see `helpers/transcribe.py`'s own docstring.)
- `ffmpeg` + `ffprobe` on PATH. `rsvg-convert` (librsvg) on PATH if building icon overlays — do not fall back to ImageMagick's built-in SVG delegate, it's broken (1-bit, no anti-aliasing).
- Python deps installed (`uv sync` or `pip install -e .` inside the repo).
- Node.js + npm available if the session needs HyperFrames or Remotion slots. HyperFrames currently requires Node.js 22+.
- `yt-dlp`, HyperFrames, Remotion, Manim installed only on first use.
- First-use animation setup happens inside the slot directory, never at the video-use repo root. HyperFrames can be invoked with `npx --yes hyperframes ...`; Remotion can be scaffolded with `npx create-video@latest` or installed as a project-local dependency before using its `remotion render` command.
- This skill vendors `skills/manim-video/`. Read its SKILL.md when building a Manim slot.

Helpers (`helpers/transcribe.py`, `helpers/render.py`, etc.) live alongside this SKILL.md. Resolve their paths relative to the directory containing this file — the skill is typically symlinked at `~/.claude/skills/video-use/` or `~/.codex/skills/video-use/`.

## Helpers

**In a normal edit you drive everything through `pipeline.py` (see "The process" below) — it calls the helpers below in the enforced order. The reference here is for one-off debugging and for understanding what the driver does.**

- **`pipeline.py init <video>…`** then **`pipeline.py <edit-dir>`** — the process driver. `init` creates the edit folder **next to the source video** (`<video_parent>/edit/`) and prints its path; **do not pass `--edit-dir`** — any value that isn't the canonical path is ignored. Use the printed path for every later call. Owns step order across `INGEST → STRATEGY → EDL → RENDER → SELF_EVAL → DONE`; runs `transcribe.py --verbatim` / `pack_transcripts.py` / `check_fillers.py` / the gap analysis / `render.py` for you and refuses to let you skip a step or render an unvalidated EDL. `--status` reports the phase; `--confirm-strategy`, `--eval-verdict pass|fail`, `--restage strategy|edl|render` are the gates. Self-backgrounds; pass `--notify-session`/`--notify-profile` on `init` under OpenClaw.

**`transcribe.py` and `render.py` self-background — you never need a background flag from the calling tool.** Every invocation returns almost immediately: the cache/lock is checked, a detached worker is spawned if needed, and the call exits. Poll with `--status` on the exact same command (same script, same args) until it prints `DONE:` or `FAILED:` — `RUNNING:` means keep waiting, `ALREADY_RUNNING:` means a prior call already started this exact job, don't launch another. This is deliberate and works identically in Claude Code's `Bash`, OpenClaw's `exec`, or a plain shell script — correctness does not depend on any caller-side background flag (confirmed 2026-09-07: relying on the calling tool's own flag was not reliable enough on its own — repeated blocking calls via OpenClaw's `exec` all died at the same ~2-minute mark before this fix). Calling either script again for the same job while it's still running is safe — it reports status instead of launching a duplicate whisper/ffmpeg process.

**In OpenClaw (Jensen/Beast): pass `--notify-session <your session key>` (plus `--notify-profile` if you're Beast — see your SOUL.md) on every `transcribe.py`/`render.py` launch.** Without it, once a background job outlives your current turn, nothing tells you it finished — your turn already ended when you started it, and no new message arrives on its own (confirmed 2026-09-07: a completed transcription sat idle for minutes with no follow-up until this was added). With it, the script calls `openclaw system event` itself the moment the job finishes or fails, waking your session immediately so you can continue without Mike having to prompt you again. Claude Code doesn't need this — polling with `--status` in the same turn is fine there.

- **`transcribe.py <video>`** — local Whisper (openai-whisper), not ElevenLabs Scribe — this fork was rewritten 2026-07-27 to run fully offline, no API key. `--model turbo` (default) or `--model large-v3`. Cached. **Normalizes out filler words ("um"/"uh") by default** — pass `--verbatim` to prime it with a verbatim-transcript prompt instead (added 2026-09-04, verified: surfaced 12 real fillers a default transcription showed zero of). Cached separately per mode (checked via a `verbatim` field in the transcript JSON) so switching modes re-transcribes correctly. Add `--status` (same args) to poll a background job instead of starting one.
- **`check_fillers.py <video_or_transcript.json>`** — deterministic filler-word report. Refuses (nonzero exit, no count) unless the transcript's `verbatim` field is `true` — this is the only way to check for fillers; never eyeball a transcript and conclude "none found" yourself (see Hard Rule 8).
- **`transcribe_batch.py <videos_dir>`** — 4-worker parallel transcription. Use for multi-take.
- **`pack_transcripts.py --edit-dir <dir>`** — `transcripts/*.json` → `takes_packed.md` (phrase-level, break on silence ≥ 0.5s).
- **`timeline_view.py <video> <start> <end>`** — filmstrip + waveform PNG. On-demand visual drill-down. **Not a scan tool** — use it at decision points, not constantly.
- **`render.py <edl.json> -o <out>`** — validates every cut against the source's real transcript first (refuses to render and prints exactly what would be lost if a range clips a word or a gap contains real speech — see Hard Rule 6; `--force` overrides after you've actually reviewed why it fired), then per-segment extract → concat → background matte (if EDL has `"background"`) → overlays (PTS-shifted) → subtitles LAST. `--preview` for 720p fast. `--build-subtitles` to generate master.srt inline. `--no-matte` to skip background swap. Add `--status` (same `edl`/`-o` args) to poll a background render instead of starting one. **`--validate-only <edl.json>`** runs just the cut-vs-transcript check and exits 0/1 (no `-o` needed) — this is what `pipeline.py`'s EDL gate calls.
- **`grade.py <in> -o <out>`** — ffmpeg filter chain grade. Presets + `--filter '<raw>'` for custom. `warm_cinematic`/sky-boost style grades are rejected for this account, including Beachcrewzr — don't use, default to `none`.
- **`matte.py <in> -o <out> --bg <image>`** — replace the background behind a person (RVM, no green screen). Own venv (`.matte-venv`). Invoked automatically by `render.py` when the EDL has `"background"` — you usually don't call this directly.
- **`build_icon_overlay.py --icon <name> --label <text> --color R,G,B -o <out.mov>`** — build a single icon+label badge overlay (ProRes 4444, real alpha) from the local Tabler icon library (`~/Developer/tabler-icons`, MIT licensed, 6,000+ icons — see that repo's own README for the full catalog, or `--list-icons '<pattern>'` to search). Same visual style as the launch video's overlays. Use `rsvg-convert` for any other SVG rasterization — ImageMagick's built-in SVG delegate produces a 1-bit bitmap with no anti-aliasing, verified broken 2026-09-04.

For animations beyond a simple icon badge, create `<edit>/animations/slot_<id>/` with `Bash` and spawn a sub-agent via the `Agent` tool.

## The process — driven by `pipeline.py`

**`helpers/pipeline.py` owns the step order. You do not call `transcribe.py`,
`transcribe_batch.py`, `pack_transcripts.py`, `check_fillers.py`, or `render.py`
yourself during a normal edit — the driver runs them, in order, and won't let you
skip ahead.** (Call them directly only for one-off debugging.) It is a state machine:

```
INGEST → STRATEGY → EDL → RENDER → SELF_EVAL → DONE
```

State lives in `<edit>/pipeline_state.json`. Every call is re-entrant and returns
fast; the slow steps (transcribe, render) self-background and wake you when done.
**Because state is on disk, a run survives a context overflow / session reset** — a
long edit fills an agent's context with transcript + briefing + render output; if
you hit "prompt too large", reset the session and re-run `pipeline.py <edit-dir>`,
which resumes at the saved phase (nothing re-transcribed or re-rendered).

**When a background job wakes you, or whenever you're unsure what's next: the only
action is `pipeline.py <edit-dir>` (or `--status`).** Do not read `transcripts/*.json`
or other edit-dir files directly and do not analyze the video or estimate filler
counts yourself — everything you reason from comes from `briefing.md`, which the
pipeline writes at the end of INGEST. No `briefing.md` yet means INGEST isn't
finished; run `pipeline.py <edit-dir>` again. (2026-09-08: a woken agent read the
raw transcript and posted "~15 uhs" instead of advancing — `check_fillers.py` never ran.)

| Command | Phase | The driver does (no choice for you) | You owe back |
|---|---|---|---|
| `pipeline.py init <video>… [--notify-session K --notify-profile P]` | — | create `<video_parent>/edit/` + state; prints the path (don't pass `--edit-dir`) | — |
| `pipeline.py <dir>` | INGEST | ffprobe → `transcribe.py --verbatim` → `pack_transcripts.py` → `check_fillers.py` → silence-gap table → **`briefing.md`** + 2 sample frames | read `briefing.md`, converse with the user |
| `pipeline.py <dir> --confirm-strategy` | STRATEGY | check `strategy.md` exists, is substantive, has a `## User confirmation` quote | write `strategy.md` (4–8 sentences + the confirmation section) |
| `pipeline.py <dir>` | EDL | schema-check `edl.json`; `render.py --validate-only` (cut-vs-speech); if `strip_fillers` → expand coarse ranges into `edl.effective.json`; refuse on any failure, no `--force` | write `edl.json` per **EDL format** — coarse ranges + `strip_fillers: true` for filler removal |
| `pipeline.py <dir>` | RENDER | `render.py … --build-subtitles` | nothing — wait |
| `pipeline.py <dir>` | SELF_EVAL | extract `timeline_view` frames of the **rendered output** at every cut (±1.5s) + head/tail/mids; check duration vs EDL | inspect every `eval/*.png` (see checklist), write findings to `eval/eval_review.md` (one line per frame — `--eval-verdict pass` is refused without it), then `--eval-verdict pass` or `--eval-verdict fail --restage edl\|render` (cap 3 fails) |
| `pipeline.py <dir> --eval-verdict pass` | → DONE | append the session block to `project.md` | — |

`pipeline.py <dir> --status` prints the current phase and exactly what's owed.
`pipeline.py <dir> --restage strategy\|edl\|render` steps back manually (`strategy` = rewrite strategy.md and re-confirm).

**Your part is only the subjective work:** what the material is and what to ask the
user (INGEST), the cut strategy (STRATEGY), the take/cut selection in the EDL and any
animations (EDL), and the visual judgement on the eval frames (SELF_EVAL). The
driver owns correctness; you own taste.

**Self-eval frame checklist** (what to look for in each `eval/*.png`, then record per-frame in `eval/eval_review.md`):
- visual discontinuity / flash / jump at the cut
- waveform spike at the boundary (audio pop past the 30 ms fade)
- subtitle hidden behind an overlay (Rule 1 violation)
- overlay misaligned or showing wrong frames (Rule 4 violation)
- the intended background swap / grade actually took effect (a matte can "succeed" and change nothing — verify visually)
- grade consistency, subtitle readability, overall coherence (head/tail/mid frames)

`--eval-verdict pass` is refused unless `eval/eval_review.md` exists with a real assessment. An OpenClaw agent (text-only model) **cannot** produce this — a human, Claude Code, or `describe_frames.py` must inspect the frames and write it. Do not pass blind.

## If the pipeline refuses

- **`WAITING: …`** — a background job (transcription or render) is still running.
  You'll be notified when it finishes; then re-run `pipeline.py <dir>`. Nothing is wrong.
- **`REFUSED: … strategy.md …`** — write a real `strategy.md` with a `## User
  confirmation` section quoting the user's plain-English approval. The pipeline will
  not cut without recorded confirmation.
- **`EDL SCHEMA INVALID:`** — fix the listed structural problems in `edl.json`
  (missing fields, bad paths, start ≥ end, range past source duration).
- **`CUT VALIDATION FAILED:`** — a range boundary clips a word, or a gap between kept
  ranges contains real speech. The message quotes exactly what would be deleted. Move
  the boundary to a real silence (see `briefing.md`'s gap table). Do **not** reach for
  `render.py --force` — the pipeline doesn't use it and neither should you here.
- **`INGEST FAILED: …`** — transcription failed or produced a non-verbatim file.
  Check `<edit>/jobs/*.log`.
- **`REFUSED: --eval-verdict pass requires a real review …`** — inspect every
  `eval/*.png` and write a per-frame assessment to `eval/eval_review.md` first. A
  text-only agent can't do this; hand off to a human, Claude Code, or
  `describe_frames.py`. `--eval-verdict fail` never needs the review.

## Cut craft (techniques)

- **Audio-first.** Candidate cuts from word boundaries and silence gaps.
- **Preserve peaks.** Laughs, punchlines, emphasis beats. Extend past punchlines to include reactions — the laugh IS the beat.
- **Speaker handoffs** benefit from air between utterances. Common values: 400–600ms. Less for fast-paced, more for cinematic. Taste call.
- **Audio events as signals.** `(laughs)`, `(sighs)`, `(applause)` mark beats. Extend past them.
- **Silence gaps are cut candidates.** Silences ≥400ms are usually the cleanest. 150–400ms phrase boundaries are usable with a visual check. <150ms is unsafe (mid-phrase).
- **Example cut padding** (the launch video shipped with this): 50ms before the first kept word, 80ms after the last. Tighter for montage energy, looser for documentary. Stay in the 30–200ms working window (Hard Rule 7).
- **Never reason audio and video independently.** Every cut must work on both tracks.

## The packed transcript (primary reading view)

`pack_transcripts.py` reads all `transcripts/*.json` and produces one markdown file where each take is a list of phrase-level lines, each prefixed with its `[start-end]` time range. Phrases break on any silence ≥ 0.5s OR speaker change. This is the artifact the editor sub-agent reads to pick cuts — it gives word-boundary precision from text alone at 1/10 the tokens of raw JSON.

Example line:
```
## C0103  (duration: 43.0s, 8 phrases)
  [002.52-005.36] S0 Ninety percent of what a web agent does is completely wasted.
  [006.08-006.74] S0 We fixed this.
```

## Editor sub-agent brief (for multi-take selection)

When the task is "pick the best take of each beat across many clips," spawn a dedicated sub-agent with a brief shaped like this. The structure is load-bearing; the pitch-shape example is not.

```
You are editing a <type> video. Pick the best take of each beat and 
assemble them chronologically by beat, not by source clip order.

INPUTS:
  - takes_packed.md (time-annotated phrase-level transcripts of all takes)
  - Product/narrative context: <2 sentences from the user>
  - Speaker(s): <name, role, delivery style note>
  - Expected structure: <pick an archetype or invent one>
  - Verbal slips to avoid: <list from the pre-scan pass>
  - Target runtime: <seconds>

Common structural archetypes (pick, adapt, or invent):
  - Tech launch / demo:   HOOK → PROBLEM → SOLUTION → BENEFIT → EXAMPLE → CTA
  - Tutorial:             INTRO → SETUP → STEPS → GOTCHAS → RECAP
  - Interview:            (QUESTION → ANSWER → FOLLOWUP) repeat
  - Travel / event:       ARRIVAL → HIGHLIGHTS → QUIET MOMENTS → DEPARTURE
  - Documentary:          THESIS → EVIDENCE → COUNTERPOINT → CONCLUSION
  - Music / performance:  INTRO → VERSE → CHORUS → BRIDGE → OUTRO
  - Or invent your own.

RULES:
  - Start/end times must fall on word boundaries from the transcript.
  - Pad cut boundaries (working window 30–200ms).
  - Prefer silences ≥ 400ms as cut targets.
  - Unavoidable slips are kept if no better take exists. Note them in "reason".
  - If over budget, revise: drop a beat or trim tails. Report total and self-correct.

OUTPUT (JSON array, no prose):
  [{"source": "C0103", "start": 2.42, "end": 6.85, "beat": "HOOK",
    "quote": "...", "reason": "..."}, ...]

Return the final EDL and a one-line total runtime check.
```

## Color grade (when requested)

Your job is to **reason about the image**, not apply a preset. Look at a frame (via `timeline_view`), decide what's wrong, adjust one thing, look again.

Mental model is ASC CDL. Per channel: `out = (in * slope + offset) ** power`, then global saturation. `slope` → highlights, `offset` → shadows, `power` → midtones.

**Example filter chains** (`grade.py` has `--list-presets`; use them as starting points or mix your own):

- **`warm_cinematic`** — retro/technical, subtle teal/orange split, desaturated. Shipped in a real launch video. Safe for talking heads.
- **`neutral_punch`** — minimal corrective: contrast bump + gentle S-curve. No hue shifts.
- **`none`** — straight copy. Default when the user hasn't asked.

For anything else — portraiture, nature, product, music video, documentary — invent your own chain. `grade.py --filter '<raw ffmpeg>'` accepts any filter string.

Hard rules: apply **per-segment during extraction** (not post-concat, which re-encodes twice). Never go aggressive without testing skin tones.

## Subtitles (when requested)

Subtitles have three dimensions worth reasoning about: **chunking** (1/2/3/sentence per line), **case** (UPPER/Title/Natural), and **placement** (margin from bottom). The right combo depends on content.

**Worked styles** — pick, adapt, or invent:

**`bold-overlay`** — short-form tech launch, fast-paced social. 2-word chunks, UPPERCASE, break on punctuation, Helvetica 18 Bold, white-on-outline, `MarginV=35`. `render.py` ships with this as `SUB_FORCE_STYLE`.

```
FontName=Helvetica,FontSize=18,Bold=1,
PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,
BorderStyle=1,Outline=2,Shadow=0,
Alignment=2,MarginV=35
```

**`natural-sentence`** (if you invent this mode) — narrative, documentary, education. 4–7 word chunks, sentence case, break on natural pauses, `MarginV=60–80`, larger font for readability, slightly wider max-width. No shipped force_style — design one if you need it.

Invent a third style if neither fits. Hard rules: subtitles LAST (Rule 1), output-timeline offsets (Rule 5).

## Animations (when requested)

Animations match the content and the brand. **Get the palette, font, and visual language from the conversation** — never assume a default. If the user hasn't told you, propose a palette in the strategy phase and wait for confirmation before building anything.

**Tool options:**

Pick the engine per animation slot. Do not default to Remotion just because the animation is web-adjacent.

- **HyperFrames** — Browser-native HTML/CSS/GSAP video compositions: product UI motion, website-to-video or mockup-to-video captures, kinetic typography, landing-page/storyboard promos, data-driven UI states, transparent WebM overlays, and clips that need deterministic frame capture plus HyperFrames lint/validate/render checks. Best when the animation should be authored and verified like a web composition instead of a React component tree.
- **Remotion** — React/CSS compositions with component state, reusable React primitives, or an existing Remotion brand system. Best when the user specifically asks for React/Remotion or when React composition is the simpler authoring model.
- **Manim** — formal diagrams, state machines, equation derivations, graph morphs. Read `skills/manim-video/SKILL.md` and its references for depth.
- **PIL + PNG sequence + ffmpeg** — simple overlay cards: counters, typewriter text, single bar reveals, progressive draws. Fast to iterate, any aesthetic you want. The launch video used this.

For HyperFrames slots, scaffold the slot inside `edit/animations/slot_<id>/` with `npx --yes hyperframes init . --example blank --non-interactive --skip-skills`, build the HTML composition there, run the HyperFrames checks that fit the slot (`lint`, `validate`, and a draft render when practical), then produce the final overlay video with `npx --yes hyperframes render . -o render.mp4` or `--format webm -o render.webm` when alpha is required. Point the EDL overlay `file` at the actual rendered path.

For Remotion slots, keep the Remotion project isolated inside the same slot directory, scaffold with `npx create-video@latest` or install Remotion locally there, render the composition to `render.mp4` with the project-local `remotion render` command, and verify duration and dimensions with `ffprobe`.

None is mandatory. Invent hybrids if useful (e.g., PIL background with a HyperFrames or Remotion layer on top).

**Duration rules of thumb, context-dependent:**

- **Sync-to-narration explanations.** A viewer needs to parse the content at 1×. Rough floor 3s, typical 5–7s for simple cards, 8–14s for complex diagrams. The launch video shipped at 5–7s per simple card.
- **Beat-synced accents** (music video, fast montage). 0.5–2s is fine — they're visual accents, not information. The "readable at 1×" rule becomes *"recognizable at 1×"*, not *"fully parseable."*
- **Hold the final frame ≥ 1s** before the cut (universal).
- **Over voiceover:** total duration ≥ `narration_length + 1s` (universal).
- **Never parallel-reveal independent elements** — the eye can't track two new things at once. One thing, pause, next thing.

**Animation payoff timing (rule for sync-to-narration):** get the payoff word's timestamp. Start the overlay `reveal_duration` seconds earlier so the landing frame coincides with the spoken payoff word. Without this sync the animation feels disconnected.

**Easing** (universal — never `linear`, it looks robotic):

```python
def ease_out_cubic(t):    return 1 - (1 - t) ** 3
def ease_in_out_cubic(t):
    if t < 0.5: return 4 * t ** 3
    return 1 - (-2 * t + 2) ** 3 / 2
```

`ease_out_cubic` for single reveals (slow landing). `ease_in_out_cubic` for continuous draws.

**Typing text anchor trick:** center on the FULL string's width, not the partial-string width — otherwise text slides left during reveal.

**Example palette** (the launch video — one aesthetic among infinite):
- Background `(10, 10, 10)` near-black
- Accent `#FF5A00` / `(255, 90, 0)` orange
- Labels `(110, 110, 110)` dim gray
- Font: Menlo Bold at `/System/Library/Fonts/Menlo.ttc` (index 1)
- ≤ 2 accent colors, ~40% empty space, minimal chrome
- Result: terminal / retro tech feel

This is one style. If the brand is warm and serif, use that. If it's colorful and playful, use that. If the user handed you a style guide, follow it. If they didn't, propose one and confirm.

**Parallel sub-agent brief** — each animation is one sub-agent spawned via the `Agent` tool. Each prompt is self-contained (sub-agents have no parent context). Include:

1. One-sentence goal: *"Build ONE animation: [spec]. Nothing else."*
2. Absolute output path (`<edit>/animations/slot_<id>/render.mp4`)
3. Exact technical spec: resolution, fps, codec, pix_fmt, CRF, duration
4. Style palette as concrete values (RGB tuples, hex, or reference to a design system)
5. Font path with index
6. Frame-by-frame timeline (what happens when, with easing)
7. Anti-list ("no chrome, no extras, no titles unless specified")
8. Code pattern reference (copy helpers inline, don't import across slots)
9. Deliverable checklist (script, render, verify duration via ffprobe, report)
10. **"Do not ask questions. If anything is ambiguous, pick the most obvious interpretation and proceed."**

One sub-agent = one file (unique filenames, parallel agents don't overwrite each other).

## Output spec

Match the source unless the user asked for something specific. Common targets: `1920×1080@24` cinematic, `1920×1080@30` screen content, `1080×1920@30` vertical social, `3840×2160@24` 4K cinema, `1080×1080@30` square. `render.py` defaults the scale to 1080p from any source; pass `--filter` or edit the extract command for other targets. Worth asking the user which delivery format matters.

## EDL format

```json
{
  "version": 1,
  "strip_fillers": true,
  "sources": {"C0103": "/abs/path/C0103.MP4", "C0108": "/abs/path/C0108.MP4"},
  "ranges": [
    {"source": "C0103", "start": 2.42, "end": 6.85,
     "beat": "HOOK", "quote": "...", "reason": "Cleanest delivery, stops before slip at 38.46."},
    {"source": "C0108", "start": 14.30, "end": 28.90,
     "beat": "SOLUTION", "quote": "...", "reason": "Only take without the false start."}
  ],
  "omissions": [
    {"source": "C0103", "start": 6.85, "end": 14.30,
     "reason": "repeated take of the same beat — kept the C0108 version"}
  ],
  "grade": "warm_cinematic",
  "overlays": [
    {"file": "edit/animations/slot_1/render.mp4", "start_in_output": 0.0, "duration": 5.0}
  ],
  "subtitles": "edit/master.srt",
  "total_duration_s": 87.4
}
```

`grade` is a preset name or raw ffmpeg filter. `overlays` are rendered animation clips. `subtitles` is optional and applied LAST.

**`strip_fillers`** (bool, default false) — to remove filler words, set this `true` and author **coarse** structural `ranges` (keep the content you want; do **not** try to cut individual um/uh yourself — you'll get the timestamps wrong and burn your token budget on 18 micro-ranges). After the EDL passes, `pipeline.py` expands your ranges into `edl.effective.json`, splitting each range around every filler word from `check_fillers.py`'s exact timestamps (±40 ms). That's what renders. `edl.json` stays as your intent; `project.md` records "N coarse → M effective ranges".

**`omissions`** — every span of *speech* your edit removes on purpose (the gap between two kept ranges, or speech before the first / after the last range) must be listed here with a `reason`, or the EDL gate rejects it as `UNDECLARED SPEECH REMOVAL`. This is for **content** you drop (a tangent, a retake, a bad outro) — not fillers, which `strip_fillers` handles. The 2026-09-07 incident deleted real sentences the editor believed were silence. A pure trim with one continuous range needs no `omissions`. `render.py --force` bypasses the check for direct manual use — `pipeline.py` never uses it; declare instead.

## Memory — `project.md`

Append one section per session at `<edit>/project.md`:

```markdown
## Session N — YYYY-MM-DD

**Strategy:** one paragraph describing the approach
**Decisions:** take choices, cuts, grades, animations + why
**Reasoning log:** one-line rationale for non-obvious decisions
**Outstanding:** deferred items
```

On startup, read `project.md` if it exists and summarize the last session in one sentence before asking whether to continue.

## Anti-patterns

Things that consistently fail regardless of style:

- **Hierarchical pre-computed codec formats** with USABILITY / tone tags / shot layers. Over-engineering. Derive from the transcript at decision time.
- **Hand-tuned moment-scoring functions.** The LLM picks better than any heuristic you'll write.
- **Whisper SRT / phrase-level output.** Loses sub-second gap data. Always word-level verbatim.
- **Assuming Whisper's plain transcript includes filler words.** It normalizes them out by default — see the `transcribe.py` note above for the verbatim-prompt workaround when filler removal is actually wanted.
- **Burning subtitles into base before compositing overlays.** Overlays hide them. (Hard Rule 1.)
- **Single-pass filtergraph when you have overlays.** Double re-encodes. Use per-segment extract → concat.
- **Linear animation easing.** Looks robotic. Always cubic.
- **Hard audio cuts at segment boundaries.** Audible pops. (Hard Rule 3.)
- **Typing text centered on the partial string.** Text slides left as it grows.
- **Sequential sub-agents for multiple animations.** Always parallel.
- **Editing before confirming the strategy.** Never.
- **Re-transcribing cached sources.** Immutable outputs of immutable inputs.
- **Assuming what kind of video it is.** Look first, ask second, edit last.
