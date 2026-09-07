#!/Users/mikeattreys/Developer/video-use/.venv/bin/python3
"""Transcribe a video with local Whisper (openai-whisper) — free, fully offline.

Drop-in replacement for the original ElevenLabs Scribe-based transcribe.py
(kept alongside as transcribe_scribe.py.bak). Extracts mono 16kHz audio via
ffmpeg, runs the local `whisper` CLI with word-level timestamps, and reshapes
the output into the same {"words": [...]} schema pack_transcripts.py expects
(type/text/start/end/speaker_id).

Known gaps vs. Scribe (openai-whisper doesn't do either):
  - No speaker diarization — every word is tagged with a single constant
    speaker_id. Fine for solo-narrated content; a real limitation for
    multi-speaker interviews.
  - No audio-event tagging (laughter, applause, sighs) — those entries are
    simply absent, not approximated.

Cached: if the output file already exists, transcription is skipped.

Usage:
    python helpers/transcribe.py <video_path>
    python helpers/transcribe.py <video_path> --edit-dir /custom/edit
    python helpers/transcribe.py <video_path> --language en
    python helpers/transcribe.py <video_path> --model turbo
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _job_lock  # noqa: E402

WHISPER_BIN = "whisper"
DEFAULT_MODEL = "turbo"
DEFAULT_DEVICE = "cpu"  # MPS is broken for word-level timestamps in this openai-whisper
                        # version: the DTW alignment step casts to float64, which Apple's
                        # Metal backend doesn't support at all. Confirmed by direct test
                        # (2026-07-27) — not a flag fix, CPU is the only working option.
                        # "turbo" model on CPU still runs well faster than realtime on M2.


def load_api_key() -> str:
    """No API key needed for local Whisper. Kept only because
    transcribe_batch.py imports this name directly."""
    return "local"


def extract_audio(video_path: Path, dest: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(dest),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# Whisper normalizes out filler words ("um"/"uh") by default -- verified on a
# real clip 2026-09-04 across both the turbo and large-v3 models. This exact
# prompt, with --carry_initial_prompt so it applies to every internal decode
# window (not just the first 30s), reliably surfaces them as real words with
# timestamps instead. Only use --verbatim when a filler-removal pass is
# actually wanted -- it changes nothing else about transcription quality/timing.
VERBATIM_PROMPT = (
    "Um, so, like, this is a verbatim transcript that includes every um, uh, "
    "and filler word exactly as spoken, uh, without cleaning anything up."
)


def call_whisper(
    audio_path: Path,
    out_dir: Path,
    language: str | None = None,
    model: str = DEFAULT_MODEL,
    verbatim: bool = False,
) -> dict:
    cmd = [
        WHISPER_BIN, str(audio_path),
        "--model", model,
        "--device", DEFAULT_DEVICE,
        "--word_timestamps", "True",
        "--output_format", "json",
        "--output_dir", str(out_dir),
        "--verbose", "False",
    ]
    if language:
        cmd += ["--language", language]
    if verbatim:
        cmd += ["--initial_prompt", VERBATIM_PROMPT, "--carry_initial_prompt", "True"]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"whisper failed: {result.stderr[-2000:]}")

    raw_path = out_dir / f"{audio_path.stem}.json"
    payload = json.loads(raw_path.read_text())
    raw_path.unlink(missing_ok=True)
    return payload


def to_scribe_schema(whisper_payload: dict) -> dict:
    """Flatten Whisper's segments[].words[] into the flat words[] list
    pack_transcripts.py expects, using Scribe's field names."""
    words: list[dict] = []
    for seg in whisper_payload.get("segments", []):
        for w in seg.get("words", []):
            text = (w.get("word") or "").strip()
            if not text:
                continue
            words.append({
                "type": "word",
                "text": text,
                "start": w.get("start"),
                "end": w.get("end"),
                "speaker_id": "speaker_0",
            })
    return {
        "language_code": whisper_payload.get("language"),
        "words": words,
    }


def transcribe_one(
    video: Path,
    edit_dir: Path,
    api_key: str = "local",  # unused; kept for call-signature compatibility with transcribe_batch.py
    language: str | None = None,
    num_speakers: int | None = None,  # unused: no diarization locally
    verbose: bool = True,
    model: str = DEFAULT_MODEL,
    verbatim: bool = False,
) -> Path:
    """Transcribe a single video with local Whisper. Returns path to transcript JSON.

    Cached: returns existing path immediately if it exists AND was produced with
    the same `verbatim` mode being requested now (checked via a `"verbatim"` field
    stored in the payload) -- one canonical file per source, not two, so
    pack_transcripts.py's `*.json` glob never sees a duplicate for the same video.
    Switching modes re-transcribes and overwrites.

    `verbatim=True` primes Whisper to include filler words ("um"/"uh") that it
    otherwise normalizes out by default -- use when a filler-removal cut is
    actually wanted.
    """
    transcripts_dir = edit_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    out_path = transcripts_dir / f"{video.stem}.json"

    if out_path.exists():
        cached = json.loads(out_path.read_text())
        if cached.get("verbatim", False) == verbatim:
            if verbose:
                print(f"cached: {out_path.name}")
            return out_path
        if verbose:
            print(f"  cached transcript used verbatim={cached.get('verbatim', False)}, "
                  f"requested verbatim={verbatim} -- re-transcribing")

    if verbose:
        print(f"  extracting audio from {video.name}", flush=True)

    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        audio = tmp_path / f"{video.stem}.wav"
        extract_audio(video, audio)
        size_mb = audio.stat().st_size / (1024 * 1024)
        if verbose:
            mode = " [verbatim]" if verbatim else ""
            print(f"  transcribing {video.stem}.wav ({size_mb:.1f} MB) with local whisper ({model}){mode}", flush=True)
        raw = call_whisper(audio, tmp_path, language, model, verbatim)
        payload = to_scribe_schema(raw)
        payload["verbatim"] = verbatim

    out_path.write_text(json.dumps(payload, indent=2))
    dt = time.time() - t0

    if verbose:
        kb = out_path.stat().st_size / 1024
        print(f"  saved: {out_path.name} ({kb:.1f} KB) in {dt:.1f}s")
        print(f"    words: {len(payload['words'])}")

    return out_path


def _job_key(video: Path, verbatim: bool) -> str:
    return f"transcribe_{video.stem}_{'verbatim' if verbatim else 'default'}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Transcribe a video with local Whisper (free, offline)")
    ap.add_argument("video", type=Path, help="Path to video file")
    ap.add_argument(
        "--edit-dir",
        type=Path,
        default=None,
        help="Edit output directory (default: <video_parent>/edit)",
    )
    ap.add_argument(
        "--language",
        type=str,
        default=None,
        help="Optional ISO language code (e.g., 'en'). Omit to auto-detect.",
    )
    ap.add_argument(
        "--num-speakers",
        type=int,
        default=None,
        help="Unused (no diarization with local Whisper); kept for CLI compatibility.",
    )
    ap.add_argument(
        "--model",
        type=str,
        default=DEFAULT_MODEL,
        help="Whisper model size (default: turbo)",
    )
    ap.add_argument(
        "--verbatim",
        action="store_true",
        help="Prime Whisper to include filler words (um/uh) it otherwise normalizes out. "
             "Use before a filler-removal cut. Re-transcribes if the cached file used a different mode.",
    )
    ap.add_argument(
        "--status",
        action="store_true",
        help="Report status of a background job for this (video, mode) instead of starting one: "
             "RUNNING: / DONE: / FAILED: / NOT_FOUND:. Exit 0 unless failed/not_found.",
    )
    ap.add_argument(
        "--notify-session",
        type=str,
        default=None,
        help="OpenClaw session key to wake (via `openclaw system event`) when this background job "
             "finishes, instead of leaving the agent with no signal to check back. Pass your own "
             "session key. Omit if not running under OpenClaw (e.g. Claude Code).",
    )
    ap.add_argument(
        "--notify-profile",
        type=str,
        default=None,
        help="OpenClaw --profile to use for the completion notification (e.g. 'unleashed' for Beast; "
             "omit for Jensen's default profile). Only used with --notify-session.",
    )
    ap.add_argument(
        "--_worker",
        action="store_true",
        help=argparse.SUPPRESS,  # internal: runs the actual transcription synchronously
    )
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"video not found: {video}")

    edit_dir = (args.edit_dir or (video.parent / "edit")).resolve()
    job_key = _job_key(video, args.verbatim)
    cached_output = edit_dir / "transcripts" / f"{video.stem}.json"

    if args.status:
        sys.exit(_job_lock.print_status_line(edit_dir, job_key, cached_output=cached_output))

    if args._worker:
        # Runs detached, spawned by the block below. Does the real work and
        # reports the outcome into the lock file for --status to read.
        try:
            out_path = transcribe_one(
                video=video,
                edit_dir=edit_dir,
                language=args.language,
                num_speakers=args.num_speakers,
                model=args.model,
                verbatim=args.verbatim,
            )
            _job_lock.mark_done(edit_dir, job_key, str(out_path),
                                 session_key=args.notify_session, profile=args.notify_profile,
                                 script_name="transcribe.py")
        except Exception as e:  # noqa: BLE001 -- must record failure, not crash silently
            _job_lock.mark_failed(edit_dir, job_key, str(e),
                                   session_key=args.notify_session, profile=args.notify_profile,
                                   script_name="transcribe.py")
            raise
        return

    # Foreground entry point. Fast in every case: either the transcript is
    # already cached (returns immediately), a job for it is already running
    # (reports status, does not launch a duplicate whisper process), or a
    # new background worker is spawned and this call returns right away --
    # never blocks on the actual transcription, regardless of how it's
    # invoked (Bash, OpenClaw's exec, or a plain shell script).
    if cached_output.exists():
        try:
            cached = json.loads(cached_output.read_text())
        except (json.JSONDecodeError, OSError):
            cached = {}
        if cached.get("verbatim", False) == args.verbatim:
            print(f"CACHED: {cached_output}")
            return

    running = _job_lock.check_running_job(edit_dir, job_key)
    if running:
        elapsed = time.time() - running.get("started_at", time.time())
        print(
            f"ALREADY_RUNNING: elapsed={elapsed:.0f}s pid={running.get('pid')} -- "
            f"poll with: transcribe.py {video} --status" + (" --verbatim" if args.verbatim else "")
        )
        return

    worker_argv = [sys.executable, str(Path(__file__).resolve()), str(video), "--_worker"]
    if args.edit_dir:
        worker_argv += ["--edit-dir", str(edit_dir)]
    if args.language:
        worker_argv += ["--language", args.language]
    if args.model != DEFAULT_MODEL:
        worker_argv += ["--model", args.model]
    if args.verbatim:
        worker_argv += ["--verbatim"]
    if args.notify_session:
        worker_argv += ["--notify-session", args.notify_session]
    if args.notify_profile:
        worker_argv += ["--notify-profile", args.notify_profile]

    job = _job_lock.spawn_background_worker(edit_dir, job_key, worker_argv)
    print(
        f"STARTED: pid={job['pid']} -- "
        f"poll with: transcribe.py {video} --status" + (" --verbatim" if args.verbatim else "")
    )


if __name__ == "__main__":
    main()
