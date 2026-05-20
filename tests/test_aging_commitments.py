"""Tests for the aging-commitment escalator (daemon job + MCP tool)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from secondbrain.core import work_items
from secondbrain.core.workspace import Workspace
from secondbrain.daemon.jobs.aging_commitments import (
    TIER_THRESHOLDS,
    _build_aging_payload,
    _tier_for_age,
    run,
)


def _seed_workitem(conn, *, title, owner, requester, days_old, status="open"):
    """Helper: insert a work_item with first_seen_at backdated by N days."""
    work_items.ensure_schema(conn)
    backdate = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    # upsert sets first_seen_at to now; we need to override it directly
    work_items.upsert(
        conn, item_type="action", title=title,
        owner=owner, requester=requester, status=status,
    )
    conn.execute(
        "UPDATE work_items SET first_seen_at=? WHERE title=?",
        (backdate, title),
    )
    conn.commit()


@pytest.fixture
def project_home(tmp_path, monkeypatch):
    """Workspace with an identity.md + a populated DB."""
    for sub in ("state", "wiki/people", "output/daemon/briefings"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)

    (tmp_path / "identity.md").write_text(
        """---
owner: Van Nguyen
role: PDX TPM
team: PDX
---
# Identity
""", encoding="utf-8")

    conn = sqlite3.connect(str(tmp_path / "state" / "workbench.db"))
    conn.row_factory = sqlite3.Row
    work_items.ensure_schema(conn)

    # Tom owes Van for 9 days — should escalate
    _seed_workitem(conn, title="Tom send migration plan", owner="Tom",
                   requester="Van Nguyen", days_old=9)
    # Sarah owes Van for 6 days — draft tier
    _seed_workitem(conn, title="Sarah review architecture", owner="Sarah",
                   requester="Van Nguyen", days_old=6)
    # Mike owes Van for 4 days — gentle
    _seed_workitem(conn, title="Mike post weekly status", owner="Mike",
                   requester="Van Nguyen", days_old=4)
    # Priya owes Van for 1 day — too young to fire
    _seed_workitem(conn, title="Priya share notes", owner="Priya",
                   requester="Van Nguyen", days_old=1)
    # Item Van OWNS — should not appear (he doesn't owe himself a chase)
    _seed_workitem(conn, title="Van update slides", owner="Van Nguyen",
                   requester="CTO Staff", days_old=10)
    # Closed item — should not appear
    _seed_workitem(conn, title="Tom done thing", owner="Tom",
                   requester="Van Nguyen", days_old=10, status="done")

    conn.close()
    monkeypatch.setenv("SECONDBRAIN_HOME", str(tmp_path))
    return tmp_path


# ── _tier_for_age unit logic ──────────────────────────────────────────


def test_tier_for_age_too_young():
    assert _tier_for_age(2, "") is None


def test_tier_for_age_gentle():
    assert _tier_for_age(3, "") == "gentle"
    assert _tier_for_age(4, "") == "gentle"


def test_tier_for_age_draft():
    assert _tier_for_age(5, "") == "draft"
    assert _tier_for_age(7, "") == "draft"


def test_tier_for_age_escalate():
    assert _tier_for_age(8, "") == "escalate"
    assert _tier_for_age(20, "") == "escalate"


def test_tier_for_age_skips_if_already_fired_at_higher_tier():
    """If escalate already fired, gentle shouldn't re-trigger."""
    assert _tier_for_age(5, "escalate") is None
    assert _tier_for_age(3, "draft") is None


def test_tier_for_age_promotes_to_higher_tier_when_aged_further():
    """An item fired at 'gentle' on day 3 should fire 'draft' on day 5."""
    assert _tier_for_age(5, "gentle") == "draft"
    assert _tier_for_age(8, "draft") == "escalate"


# ── _build_aging_payload ──────────────────────────────────────────────


def test_build_payload_buckets_correctly(project_home):
    ws = Workspace()
    payload = _build_aging_payload(ws)
    by_tier = payload["by_tier"]
    assert len(by_tier["escalate"]) == 1
    assert by_tier["escalate"][0]["title"] == "Tom send migration plan"
    assert len(by_tier["draft"]) == 1
    assert by_tier["draft"][0]["title"] == "Sarah review architecture"
    assert len(by_tier["gentle"]) == 1
    assert by_tier["gentle"][0]["title"] == "Mike post weekly status"


def test_build_payload_excludes_owner_self(project_home):
    """Items where Van is the owner should NOT appear (he doesn't chase himself)."""
    ws = Workspace()
    payload = _build_aging_payload(ws)
    all_titles = [item["title"] for items in payload["by_tier"].values() for item in items]
    assert not any("Van update slides" in t for t in all_titles)


def test_build_payload_excludes_done(project_home):
    """Closed items must not appear regardless of age."""
    ws = Workspace()
    payload = _build_aging_payload(ws)
    all_titles = [item["title"] for items in payload["by_tier"].values() for item in items]
    assert not any("Tom done thing" in t for t in all_titles)


def test_build_payload_excludes_too_young(project_home):
    ws = Workspace()
    payload = _build_aging_payload(ws)
    all_titles = [item["title"] for items in payload["by_tier"].values() for item in items]
    assert not any("Priya" in t for t in all_titles)


# ── run() — full job: writes file + stamps chased ─────────────────────


def test_run_writes_brief_and_stamps_items(project_home):
    ws = Workspace()
    result = run(ws)
    assert result["fired_count"] == 3  # tom + sarah + mike
    assert result["by_tier"]["escalate"] == 1
    assert result["by_tier"]["draft"] == 1
    assert result["by_tier"]["gentle"] == 1

    # File written
    briefing_dir = project_home / "output" / "daemon" / "briefings"
    files = list(briefing_dir.glob("*-aging.md"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "Tom send migration plan" in content
    assert "Escalate or drop" in content
    assert "Draft a chaser" in content

    # Items stamped — re-running should fire NOTHING (same tier, already done)
    result2 = run(ws)
    assert result2["fired_count"] == 0


def test_run_handles_empty_workspace(tmp_path, monkeypatch):
    """No DB, no work_items — should not crash."""
    (tmp_path / "state").mkdir(parents=True)
    monkeypatch.setenv("SECONDBRAIN_HOME", str(tmp_path))
    ws = Workspace()
    result = run(ws)
    assert result["fired_count"] == 0


def test_run_handles_no_identity_file(project_home):
    """If identity.md is missing, the job should still run (just can't filter
    out 'self-owned' items)."""
    (project_home / "identity.md").unlink()
    ws = Workspace()
    result = run(ws)
    # Without owner_self, items where Van is owner are no longer filtered out
    # So fired count goes up (Van's own slides item shows)
    assert result["fired_count"] >= 3


# ── MCP tool handler ──────────────────────────────────────────────────


def test_query_aging_commitments_handler(project_home):
    from secondbrain.mcp.server import _handle_query_aging_commitments
    result = _handle_query_aging_commitments({})
    assert result["owner_self"] == "Van Nguyen"
    assert result["fired_count"] == 3
    assert set(result["thresholds"].keys()) == {"gentle", "draft", "escalate"}


def test_query_aging_commitments_min_days_override(project_home):
    from secondbrain.mcp.server import _handle_query_aging_commitments
    # Setting min_days_aged=7 should drop the gentle (4d) + draft (6d) tier items
    result = _handle_query_aging_commitments({"min_days_aged": 7})
    assert result["fired_count"] == 1  # only Tom (9d) remains
    assert result["fired"][0]["title"] == "Tom send migration plan"


def test_query_aging_commitments_invalid_min_days(project_home):
    from secondbrain.mcp.server import _handle_query_aging_commitments
    result = _handle_query_aging_commitments({"min_days_aged": "five"})
    assert "error" in result
