"""Tests for the daemon plane — registry, state, morning_brief job."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from secondbrain.core import work_items
from secondbrain.core.workspace import Workspace
from secondbrain.daemon import state as daemon_state
from secondbrain.daemon.registry import JOBS, load_builtin_jobs


def _make_workspace_with_data(tmp_path: Path) -> Workspace:
    """Build a project home with a small set of work_items + people pages."""
    # Scaffold dirs
    for sub in ("state", "wiki/people", "wiki/patterns", "output/daemon/briefings"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)

    # Seed the DB with overdue/today/week items
    today = date.today()
    db = tmp_path / "state" / "workbench.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    work_items.ensure_schema(conn)

    yesterday = (today - timedelta(days=1)).isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()
    in_three_days = (today + timedelta(days=3)).isoformat()
    in_two_weeks = (today + timedelta(days=14)).isoformat()

    work_items.upsert(conn, item_type="action", title="overdue thing", due_date=yesterday)
    work_items.upsert(conn, item_type="action", title="due today thing", due_date=today.isoformat())
    work_items.upsert(conn, item_type="action", title="due tomorrow thing", due_date=tomorrow)
    work_items.upsert(conn, item_type="action", title="due in 3 days", due_date=in_three_days)
    work_items.upsert(conn, item_type="action", title="far future thing", due_date=in_two_weeks)
    conn.close()

    # Seed a stale trusted person
    stale_date = (today - timedelta(days=45)).isoformat()
    (tmp_path / "wiki" / "people" / "old-trusted-friend.md").write_text(
        f"---\ntitle: Old Trusted Friend\ntrust_tier: trusted\nlast_updated: {stale_date}\n---\n\n# OTF\n",
        encoding="utf-8",
    )
    return Workspace(tmp_path)


def test_jobs_register() -> None:
    load_builtin_jobs()
    assert "morning_brief" in JOBS
    job = JOBS["morning_brief"]
    assert job.id == "morning_brief"
    assert "cron" in job.schedule


def test_morning_brief_builds_payload(tmp_path: Path) -> None:
    ws = _make_workspace_with_data(tmp_path)
    from secondbrain.daemon.jobs.morning_brief import _build_briefing

    payload = _build_briefing(ws)
    assert len(payload["overdue"]) == 1
    assert payload["overdue"][0]["title"] == "overdue thing"
    assert len(payload["due_today"]) == 1
    assert len(payload["due_this_week"]) == 2  # tomorrow + in_three_days
    assert len(payload["quiet_relationships"]) == 1
    assert payload["quiet_relationships"][0]["slug"] == "old-trusted-friend"


def test_morning_brief_run_writes_file(tmp_path: Path) -> None:
    ws = _make_workspace_with_data(tmp_path)
    from secondbrain.daemon.jobs.morning_brief import run

    result = run(ws)
    assert result["overdue"] == 1
    assert result["due_today"] == 1
    assert result["due_this_week"] == 2

    out_file = tmp_path / result["path"]
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "Morning briefing" in content
    assert "overdue thing" in content
    assert "Old Trusted Friend" in content


def test_morning_brief_no_data_handles_gracefully(tmp_path: Path) -> None:
    """Job should not crash when there's no DB and no wiki yet."""
    (tmp_path / "wiki" / "people").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    ws = Workspace(tmp_path)

    from secondbrain.daemon.jobs.morning_brief import run

    result = run(ws)
    assert result["overdue"] == 0
    assert result["due_today"] == 0
    assert result["due_this_week"] == 0


def test_record_fire_and_history(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db = tmp_path / "state" / "workbench.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    daemon_state.record_fire(conn, "morning_brief", status="ok", duration_ms=42, result={"foo": "bar"})
    daemon_state.record_fire(conn, "morning_brief", status="error", duration_ms=99, error="boom")

    hist = daemon_state.fire_history(conn, job_id="morning_brief")
    assert len(hist) == 2
    # Most recent first
    assert hist[0]["status"] == "error"
    assert hist[1]["status"] == "ok"

    last = daemon_state.last_fire(conn, "morning_brief")
    assert last["status"] == "error"
