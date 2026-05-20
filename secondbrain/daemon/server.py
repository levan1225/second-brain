"""Daemon process — runs the scheduler in the foreground.

Designed to be supervised by launchd / systemd / Windows Task Scheduler.
On crash, the supervisor restarts. On Cmd+C / SIGTERM, we shut down cleanly.

Synchronous (BlockingScheduler) so the supervisor sees one foreground
process and the Python code stays simple.
"""

from __future__ import annotations

import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from secondbrain.core.workspace import Workspace, WorkspaceError
from secondbrain.daemon.registry import JOBS, load_builtin_jobs
from secondbrain.daemon import state as daemon_state


def _make_trigger(schedule: dict[str, Any]):
    """Convert a Job.schedule dict into an APScheduler trigger."""
    if "cron" in schedule:
        return CronTrigger(**schedule["cron"])
    if "interval_seconds" in schedule:
        return IntervalTrigger(seconds=schedule["interval_seconds"])
    raise ValueError(f"unsupported schedule: {schedule}")


def _wrap_job(job_id: str, job_fn):
    """Wrap a job's run() with timing + state recording."""
    def wrapped() -> None:
        ws = Workspace()
        start = time.monotonic()
        try:
            result = job_fn(ws)
            duration = int((time.monotonic() - start) * 1000)
            print(f"[daemon] {job_id} ok in {duration}ms — {result}", flush=True)
            try:
                conn = ws.open_db()
                daemon_state.record_fire(
                    conn, job_id,
                    status="ok", duration_ms=duration, result=result,
                )
            except Exception as e:
                print(f"[daemon] {job_id} state record failed: {e}", flush=True)
        except Exception as e:
            duration = int((time.monotonic() - start) * 1000)
            err = f"{type(e).__name__}: {e}"
            print(f"[daemon] {job_id} FAIL in {duration}ms — {err}", flush=True)
            traceback.print_exc()
            try:
                conn = ws.open_db()
                daemon_state.record_fire(
                    conn, job_id,
                    status="error", duration_ms=duration, error=err,
                )
            except Exception:
                pass
    return wrapped


def run_forever() -> int:
    """Main entry: register jobs, install signal handlers, block on scheduler."""
    if daemon_state.is_daemon_running():
        existing_pid = daemon_state.read_pid()
        print(f"[daemon] already running (pid {existing_pid}) — exiting", flush=True)
        return 1

    try:
        ws = Workspace()
    except WorkspaceError as e:
        print(f"[daemon] FATAL: {e}", flush=True)
        return 2

    print(f"[daemon] starting in {ws.project_home}", flush=True)

    load_builtin_jobs()
    if not JOBS:
        print("[daemon] no jobs registered — exiting", flush=True)
        return 3

    scheduler = BackgroundScheduler(timezone="UTC")
    for job_id, job in JOBS.items():
        trigger = _make_trigger(job.schedule)
        scheduler.add_job(_wrap_job(job_id, job.run), trigger=trigger, id=job_id, name=job_id)
        print(f"[daemon] registered {job_id} — schedule: {job.schedule}", flush=True)

    # Write PID + scheduler must be started before next_run_time is available
    daemon_state.write_pid()
    scheduler.start()
    print(f"[daemon] scheduler started — {len(JOBS)} job(s) registered", flush=True)

    # Now collect next-fire times for the state snapshot
    registered: list[dict] = []
    for job_id, job in JOBS.items():
        sched_job = scheduler.get_job(job_id)
        next_run = getattr(sched_job, "next_run_time", None) if sched_job else None
        registered.append({
            "id": job_id,
            "description": job.description,
            "schedule": job.schedule,
            "next_run": str(next_run) if next_run else None,
        })
        print(f"[daemon]   {job_id} next at {next_run}", flush=True)

    daemon_state.write_state({
        "started_at": datetime.now(timezone.utc).isoformat(),
        "pid": daemon_state.read_pid(),
        "project_home": str(ws.project_home),
        "jobs": registered,
    })

    # Clean-shutdown signal handlers
    stop_flag = {"requested": False}

    def _handle_signal(signum, _frame) -> None:
        sig_name = signal.Signals(signum).name
        print(f"[daemon] received {sig_name} — shutting down", flush=True)
        stop_flag["requested"] = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        while not stop_flag["requested"]:
            time.sleep(1)
    finally:
        print("[daemon] stopping scheduler...", flush=True)
        scheduler.shutdown(wait=False)
        daemon_state.clear_pid()
        print("[daemon] stopped", flush=True)

    return 0


def main() -> None:
    sys.exit(run_forever())


if __name__ == "__main__":
    main()
