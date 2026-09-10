# Handoff — video-use: transcription engine swapped to faster-whisper (config D1)

_Last updated: 2026-09-10T21:28:00Z_

## 1. Goal

`video-use` is a conversation-driven video editor. Target: Mike hands Beast (or
Jensen) a video from anywhere — including his phone, away from a terminal — and
gets back a finished edit with **zero mid-run interaction**. The agent supplies
only the two subjective artifacts (`strategy.md`, `edl.json`); everything else
(transcription, filler analysis, cut validation, render, QC) is deterministic
pipeline code the agent cannot bypass. On DONE the agent pings Mike's Discord
channel with `final.mp4` + a QC verdict; Mike watches it and replies `recut …`
or `ship it`.

Why the machinery exists: prose "Hard Rules" repeatedly failed to stop the local
model (qwen3.6-35b-a3b) fabricating filler counts, silence gaps, a self-eval
review, and a `## User confirmation` quote, and skipping steps. Step order is
code-enforced by a state machine.

**This session's work:** replaced the transcription engine. `helpers/transcribe.py`
called the Homebrew `whisper` CLI (openai-whisper) via subprocess; it now runs
**faster-whisper** (CTranslate2) in-process. Motivation: ~3× faster on CPU, no
PyTorch, no orphaned-worker reaping, and it recovers more of hard/fast speech the
openai-whisper turbo path silently dropped. The hard part was verbatim filler
capture (the `--verbatim` path feeds `check_fillers.py` / `filler_cuts.py`) —
faster-whisper normalizes fillers out and loops on silence; config "D1" fixes
both.

Full chronological record: `plans/OBJECTIVE.md`. Design docs:
`plans/AUTO-ADVANCE-DESIGN.md`, `plans/PIPELINE-DRIVER-DESIGN.md`.

## 2. Current state

`video-use` HEAD **`010dbb1`** (transcribe swap), pushed to the fork
(`eheyejen-sketch/video-use`). `openclaw-config` HEAD `62460eb` (DECISIONS.md +
SOUL.md trim + evaluate-tool-watchlist v1.2.0), pushed.

### DONE + verified this session — faster-whisper swap
- `helpers/transcribe.py` runs faster-whisper 1.2.1, `large-v3-turbo`, CPU
  `int8`, in-process. Config **D1**:
  - `hotwords=VERBATIM_PROMPT` (verbatim mode only) — re-applied every decode
    window, so filler capture persists past 30s. **Not** `initial_prompt`
    (seeds window 1 only; loops in trailing silence).
  - `condition_on_previous_text=False` (**both** verbatim and non-verbatim) —
    the anti-loop lever. Removes the rolling-context feedback that runs away
    into repeated "uh"/repeated-phrase in trailing/near-silence.
  - `vad_filter=False`, `beam_size=5`, `cpu_threads` from
    `VIDEO_USE_WHISPER_THREADS` (default 8). `prompt_reset_on_temperature` left
    at default.
- Validated through the real `check_fillers.py` + `pack_transcripts.py` + the
  full foreground→worker→`--status`→`DONE` path:
  - `IMG_4328` (71s): 17 fillers / 193 words, spread, no loop (openai-whisper
    baseline 18/199).
  - `IMG_4345` (2:51, Mike recorded it to stress-test): 33 fillers / 417 words,
    spread across all 8 buckets, no loop, 2 zero-duration words.
  - word-timestamp offset vs openai-whisper: median 0ms, p90 20ms.
- Interface preserved: `call_whisper` / `to_scribe_schema` / `transcribe_one` /
  `load_api_key` signatures + the openai-whisper-shaped payload unchanged →
  `transcribe_batch.py` and `pack_transcripts.py` needed no edits.
- `WhisperModel` loaded once (module cache), `transcribe()` serialized behind a
  lock (not concurrency-safe; `transcribe_batch.py`'s 4-worker pool now queues —
  fine, CPU-bound).
- Deps added to `pyproject.toml` + `uv.lock`: `faster-whisper>=1.2,<2`,
  `onnxruntime>=1.16,<1.21` (1.21+ drops the cp310 wheels this venv — Python
  3.10 — needs). Model auto-downloads ~1.5 GB to the HF cache on first run;
  pre-pulled this session.

### DONE + verified earlier (2026-09-09) — the unattended pipeline (unchanged)
- `helpers/pipeline.py` drives `INGEST → STRATEGY → EDL → RENDER → SELF_EVAL →
  DONE`, state in `<edit>/pipeline_state.json`. Agent runs auto-advance every
  mechanical phase; one rate-limited `system event` nudge at STRATEGY and EDL.
- Live Beast run #2: `init → DONE`, no terminal interaction; EDL gate rejected
  the first `edl.json` and Beast self-corrected; recut round-trip worked.
- `helpers/qc_render.py` — deterministic QC (duration vs EDL, stream integrity,
  bg-swap log line, caption sanity, stale-render check). Replaced a failed
  local-VLM reviewer. On both agents' exec allowlists.
- Guards: `init` refuses if a watcher is live or ran <150s ago; `--restage`
  refuses if run <90s ago; `MAX_CONSEC_FAIL=2` retry-storm breaker; coord
  markers in `~/.cache/video-use/locks/<sha1>.{init,watch,restage}`.

### Not done / open
- faster-whisper D1 validated on two clips only (71s + 2:51). No longer-clip or
  multi-speaker run.
- `watch` and `summarize` skills still call the openai-whisper CLI — untouched,
  separate eval.
- "One clean unattended run from scratch" (2026-09-09 item) still not executed.

## 3. Files being touched

- `helpers/transcribe.py` — **swapped to faster-whisper, config D1.** Committed
  (`010dbb1`). Single file; no vendored copies anywhere (the exec-approved path
  is the live file for all agents; SKILL.md copies never referenced it).
- `pyproject.toml`, `uv.lock` — `faster-whisper` + `onnxruntime<1.21` added.
  Committed.
- `plans/HANDOFF.md` — this file.
- `helpers/{pipeline,qc_render,render,filler_cuts,_job_lock}.py` — stable, from
  the 2026-09-09 pipeline work. Not touched this session.
- `SKILL.md` + `~/.openclaw/skills/video-use/SKILL.md` +
  `~/.openclaw-unleashed/skills/video-use/SKILL.md` — the two scoped copies MUST
  stay byte-identical to each other (`diff` then `cp`), NOT to canonical. Not
  touched this session (the transcribe swap is an internal helper).
- Runtime state (not repo): `~/.cache/video-use/locks/`, HF model cache
  `~/.cache/huggingface/`.

## 4. What's been tried that failed

### faster-whisper config (this session)
- **`initial_prompt=VERBATIM_PROMPT`** (mirror of openai-whisper's
  `--carry_initial_prompt`) — faster-whisper only applies `initial_prompt` to
  window 1 and it falls out of the ~223-token rolling context after ~30–40s.
  Worse: it triggers a repetition loop in trailing silence. Every config
  containing `initial_prompt` looped. Use `hotwords` instead.
- **Config C2** = `hotwords` + `condition_on_previous_text=True` +
  `prompt_reset_on_temperature=1.0`. Passed on the 71s clip, then produced a
  **374-consecutive-"uh" runaway** on the 2:51 clip — disabling the temperature
  reset removes the only thing that breaks a loop once it starts.
- **Config D3** = C2 + `no_repeat_ngram_size=3` + `hallucination_silence_threshold`.
  Kills the loop, matches the openai-whisper reference filler count exactly, AND
  recovers a ~14-word clause openai-whisper dropped + catches fillers fused
  mid-sentence. **Rejected** because faster-whisper's `word_timestamps` collapse
  to zero-duration on exactly those hard segments (18 zero-dur words vs D1's
  1–2); video-use cuts on word boundaries. Revisit path if D1's missed
  hard-segment speech becomes a real problem: detect zero-duration runs and
  interpolate across the known window (~20 lines, no new dep) — **not** a forced
  aligner (torchaudio wav2vec2 = ~2 GB PyTorch + 1–2 days, poor ROI).
- **Non-verbatim path with stock `condition_on_previous_text=True`** — same
  trailing-silence loop (repeated a phrase 55× into a clip's last 1.3s). Fixed
  by putting `condition_on_previous_text=False` on both paths, not just verbatim.
- **Parakeet TDT 0.6B v2** (as a "better alternative" scout before touching
  faster-whisper) — NVIDIA docs claim it "preserves disfluencies"; a real
  `check_fillers` spike showed it dropped **all** fillers. Rejected for
  video-use; would suit `watch`/`summarize`. Fast (~21× realtime), no
  hallucination, but subword-only timestamps. Notes in `DECISIONS.md`.
- **MPS/Metal for transcription** — CTranslate2 has no Metal backend at all
  (`unsupported device mps`), same CPU-only outcome as openai-whisper's
  float64/DTW crash. Do not re-spike.

### Pipeline (2026-09-09, still relevant)
- **Prose Hard Rules alone** — violated repeatedly; the reason the driver exists.
- **Human-step flags** (`--confirmed-by`, `--reviewer`, an out-of-band nonce) —
  an agent just types them / it's password friction. Human gate removed for
  agent runs; Mike reviews the finished video instead.
- **A local VLM as SELF_EVAL reviewer** — qwen3.6 burns its token budget on
  unsuppressable reasoning → empty image-call `content`, 20–90s each.
  `qc_render.py` is deterministic instead.
- **Unthrottled whisper on the VM** — pinned all 16 vCPUs, flaky guest jetsam
  SIGKILL'd it at ~98%. Was fixed with an env thread cap + process-group kill;
  now handled by `cpu_threads` on the in-process faster-whisper model.
- **Beast loops any "re-run X" instruction** and **stalls after announcing an
  action** — contained by the 150s/90s guards + `_spawn_watcher` dedupe + the
  `--watch` loop being the only thing that reliably drives a run.

## 5. What to do next

1. **Longer-clip validation of D1.** Get a 5+ min or multi-speaker clip, run
   `helpers/transcribe.py <clip> --verbatim --language en`, then
   `helpers/check_fillers.py <edit-dir>/transcripts/<stem>.json` and eyeball the
   packed transcript for loops / zero-duration runs. D1 has only been checked on
   71s + 2:51.
2. **One clean unattended pipeline run from scratch** (carried over, still not
   done): `rm -rf ~/Desktop/video-use/test-beast/edit`, Mike sends Beast a video
   + edit request, watch `<edit>/jobs/watch.log` for
   `init → transcription → auto STRATEGY → EDL nudge → auto RENDER → qc_render →
   DONE + Discord ping` with zero terminal use. Confirms the guards hold with the
   new transcription engine in place.
3. **Decide on `watch` / `summarize` skills.** They still shell out to the
   openai-whisper CLI. faster-whisper D1 would likely drop into them easily
   (they need a plain transcript, not verbatim filler capture) and they'd gain
   the anti-hallucination behavior. Light per-site verification each. Only if
   Mike wants it — not started.
4. **If D1's missed hard-segment speech shows up on a real edit:** implement the
   D3 zero-duration-span interpolation (section 4). Don't do the forced aligner.
5. **Backlog (`OBJECTIVE.md`):** a `recut` that regenerates `strategy.md` is
   untested; vision QC if a fast non-reasoning local VLM appears; a global
   anti-drift SessionStart hook.
