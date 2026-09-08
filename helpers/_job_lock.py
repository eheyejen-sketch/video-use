"""Shared background-job lock/status helpers for transcribe.py and render.py.

Turns a long-running helper into a self-backgrounding command: the first
invocation forks a fully detached worker and returns almost immediately;
a second invocation for the same job while it's running reports status
instead of launching a duplicate whisper/ffmpeg process; `--status` polls
without blocking. This is deliberate: correctness must not depend on the
calling tool remembering a flag (OpenClaw's `background: true`, Claude
Code's `run_in_background`) -- confirmed 2026-09-07 that relying on a
caller-side flag is not reliable enough on its own. The script backgrounds
itself regardless of how it's invoked.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    except OSError:
        return False
    return True


def lock_path(edit_dir: Path, job_key: str) -> Path:
    jobs_dir = edit_dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    return jobs_dir / f"{job_key}.json"


def read_lock(lock_file: Path) -> dict | None:
    if not lock_file.exists():
        return None
    try:
        return json.loads(lock_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def write_lock(lock_file: Path, data: dict) -> None:
    lock_file.write_text(json.dumps(data, indent=2))


def check_running_job(edit_dir: Path, job_key: str) -> dict | None:
    """Live lock dict if a job for this key is genuinely running (status
    'running' AND pid still alive), else None. A 'running' lock whose pid
    is dead is a crashed job (killed, gateway restart, OOM, etc.) -- it's
    reclassified as failed in place so future checks don't re-derive this."""
    lock_file = lock_path(edit_dir, job_key)
    data = read_lock(lock_file)
    if not data or data.get("status") != "running":
        return None
    pid = data.get("pid")
    if pid and _pid_alive(pid):
        return data
    data["status"] = "failed"
    data["error"] = "process no longer running (killed or crashed without updating status)"
    data["finished_at"] = time.time()
    write_lock(lock_file, data)
    return None


def spawn_background_worker(edit_dir: Path, job_key: str, worker_argv: list[str]) -> dict:
    """Launches worker_argv fully detached (survives the parent exiting --
    including the calling tool's own foreground call returning or timing
    out) and writes the initial lock. Returns the lock dict."""
    jobs_dir = edit_dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    log_file = jobs_dir / f"{job_key}.log"
    lock_file = lock_path(edit_dir, job_key)

    with open(log_file, "a") as logf:
        proc = subprocess.Popen(
            worker_argv,
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    data = {
        "status": "running",
        "pid": proc.pid,
        "started_at": time.time(),
        "finished_at": None,
        "log_path": str(log_file),
        "output_path": None,
        "error": None,
    }
    write_lock(lock_file, data)
    return data


_TOKEN_RE = re.compile(r'^\s*export\s+OPENCLAW(?:_[A-Z0-9]+)*_GATEWAY_AUTH_TOKEN\s*=\s*(.+?)\s*$')


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _resolve_gateway_token(profile: str | None) -> str | None:
    """The worker is a detached grandchild of an OpenClaw `exec` call, and
    `exec` does NOT pass OPENCLAW_* secrets to spawned commands (a correct
    security boundary). So `openclaw system event` from here fails auth --
    which is why the whole notify path silently did nothing from an agent
    context until 2026-09-08. Recover the token from the profile's
    service-env file, which OpenClaw generates at a stable path.

      Jensen (default profile): ~/.openclaw/service-env/*.env
      Beast   (profile=unleashed): ~/.openclaw-unleashed/service-env/*.env
    """
    for var in ("OPENCLAW_GATEWAY_TOKEN", "OPENCLAW_GATEWAY_PASSWORD"):
        if os.environ.get(var):
            return os.environ[var]
    home = Path(os.path.expanduser("~"))
    root = home / (f".openclaw-{profile}" if profile else ".openclaw")
    env_dir = root / "service-env"
    if not env_dir.is_dir():
        return None
    for env_file in sorted(env_dir.glob("*.env")):
        try:
            for line in env_file.read_text().splitlines():
                m = _TOKEN_RE.match(line)
                if m:
                    return _unquote(m.group(1).strip())
        except OSError:
            continue
    return None


def notify_completion(session_key: str | None, profile: str | None, message: str) -> None:
    """Wake the OpenClaw session that launched this job so the agent resumes
    without waiting for a human prompt. A background job finishing gives the
    agent no signal on its own -- its turn ended when the job started. This
    fires `openclaw system event ... --mode now`.

    No-ops (returns) when session_key is unset (Claude Code has no session to
    notify). A notification failure never fails the job -- but unlike before
    2026-09-08 it is NOT silent: the outcome is printed (the worker's stdout
    is its job log), so a broken notify path is visible in jobs/<key>.log."""
    if not session_key:
        return
    token = _resolve_gateway_token(profile)
    cmd = ["openclaw"]
    if profile:
        cmd += ["--profile", profile]
    cmd += ["system", "event", "--session-key", session_key,
            "--text", message, "--mode", "now"]
    if token:
        cmd += ["--token", token]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30, text=True)
        if r.returncode == 0:
            print(f"[notify] system event sent to {session_key}"
                  f"{'' if token else ' (no token resolved -- may have failed auth)'}")
        else:
            print(f"[notify] FAILED rc={r.returncode}: "
                  f"{(r.stderr or r.stdout or '').strip()[:300]}")
    except Exception as e:  # noqa: BLE001 -- never let a notify failure fail the job
        print(f"[notify] FAILED (exception): {e}")


def mark_done(edit_dir: Path, job_key: str, output_path: str,
               session_key: str | None = None, profile: str | None = None,
               script_name: str = "job") -> None:
    lock_file = lock_path(edit_dir, job_key)
    data = read_lock(lock_file) or {}
    data.update({
        "status": "done",
        "finished_at": time.time(),
        "output_path": output_path,
        "error": None,
    })
    write_lock(lock_file, data)
    notify_completion(
        session_key, profile,
        f"System notice: your background {script_name} ({job_key}) finished. "
        f"Status: DONE. Output: {output_path}. Continue the video-use workflow from here.",
    )


def mark_failed(edit_dir: Path, job_key: str, error: str,
                 session_key: str | None = None, profile: str | None = None,
                 script_name: str = "job") -> None:
    lock_file = lock_path(edit_dir, job_key)
    data = read_lock(lock_file) or {}
    data.update({
        "status": "failed",
        "finished_at": time.time(),
        "error": error,
    })
    write_lock(lock_file, data)
    notify_completion(
        session_key, profile,
        f"System notice: your background {script_name} ({job_key}) failed. "
        f"Error: {error}. Check what happened before retrying.",
    )


def print_status_line(edit_dir: Path, job_key: str, cached_output: Path | None = None) -> int:
    """Prints one machine-parseable status line and returns a process exit
    code (0 for running/done, 1 for failed/not_found). `cached_output`, if
    given and it exists, short-circuits to DONE even with no lock file --
    covers output produced before this locking scheme existed."""
    lock_file = lock_path(edit_dir, job_key)
    data = read_lock(lock_file)

    if not data:
        if cached_output is not None and cached_output.exists():
            print(f"DONE: output={cached_output}")
            return 0
        print(f"NOT_FOUND: no job recorded for {job_key}")
        return 1

    status = data.get("status")
    if status == "running":
        pid = data.get("pid")
        if pid and _pid_alive(pid):
            elapsed = time.time() - data.get("started_at", time.time())
            print(f"RUNNING: elapsed={elapsed:.0f}s pid={pid} log={data.get('log_path')}")
            return 0
        data["status"] = "failed"
        data["error"] = "process no longer running (killed or crashed without updating status)"
        data["finished_at"] = time.time()
        write_lock(lock_file, data)
        status = "failed"

    if status == "done":
        print(f"DONE: output={data.get('output_path')}")
        return 0
    if status == "failed":
        print(f"FAILED: {data.get('error')}")
        return 1

    print(f"UNKNOWN_STATUS: {status}")
    return 1
