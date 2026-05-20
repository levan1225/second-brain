"""Daemon process state: PID file, fire history, schedule for `sb daemon status`."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".config" / "secondbrain"
PID_FILE = CONFIG_DIR / "daemon.pid"
STATE_FILE = CONFIG_DIR / "daemon.state.json"


# ── Process management (PID file) ────────────────────────────────────────


def _ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def is_daemon_running() -> bool:
    """Returns True if the PID file points at a live process."""
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return False
    try:
        # Signal 0 = check existence without sending
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it — still treat as running
        return True


def read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def write_pid() -> None:
    _ensure_config_dir()
    PID_FILE.write_text(str(os.getpid()) + "\n", encoding="utf-8")


def clear_pid() -> None:
    if PID_FILE.exists():
        PID_FILE.unlink()


def stop_daemon() -> bool:
    """Send SIGTERM to the running daemon. Returns True if signal sent."""
    pid = read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        clear_pid()
        return False


# ── State snapshot (for `sb daemon status`) ──────────────────────────────


def write_state(payload: dict[str, Any]) -> None:
    _ensure_config_dir()
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def read_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


# ── Per-job fire history (in workbench.db so it's queryable) ─────────────


def _ensure_jobs_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daemon_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            fired_at TEXT NOT NULL,
            status TEXT NOT NULL,
            duration_ms INTEGER,
            result_json TEXT,
            error TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_daemon_jobs_job_id ON daemon_jobs(job_id, fired_at DESC)"
    )
    conn.commit()


def record_fire(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    status: str,
    duration_ms: int | None = None,
    result: dict | None = None,
    error: str | None = None,
) -> None:
    _ensure_jobs_table(conn)
    conn.execute(
        "INSERT INTO daemon_jobs (job_id, fired_at, status, duration_ms, result_json, error) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            job_id,
            datetime.now(timezone.utc).isoformat(),
            status,
            duration_ms,
            json.dumps(result) if result else None,
            error,
        ),
    )
    conn.commit()


def last_fire(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    _ensure_jobs_table(conn)
    row = conn.execute(
        "SELECT * FROM daemon_jobs WHERE job_id=? ORDER BY fired_at DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    return dict(row) if row else None


def fire_history(conn: sqlite3.Connection, job_id: str | None = None, limit: int = 20) -> list[dict]:
    _ensure_jobs_table(conn)
    if job_id:
        rows = conn.execute(
            "SELECT * FROM daemon_jobs WHERE job_id=? ORDER BY fired_at DESC LIMIT ?",
            (job_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM daemon_jobs ORDER BY fired_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
