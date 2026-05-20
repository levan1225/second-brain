"""Tests for the Promise Ledger — multi-audience promise tracking + inconsistency detection."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from secondbrain.core import work_items


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    work_items.ensure_schema(conn)
    return conn


# ── upsert_promise basic semantics ────────────────────────────────────


def test_upsert_promise_creates_new(db):
    r = work_items.upsert_promise(
        db, title="Project Atlas ships by Q3",
        made_to="CEO", audience_type="executive", topic="atlas",
    )
    assert r["created"] is True
    assert r["promise_id"] > 0


def test_upsert_promise_idempotent_same_audience(db):
    r1 = work_items.upsert_promise(
        db, title="Project Atlas ships by Q3", made_to="CEO", topic="atlas",
    )
    r2 = work_items.upsert_promise(
        db, title="Project Atlas ships by Q3", made_to="CEO", topic="atlas",
    )
    assert r1["created"] is True
    assert r2["created"] is False
    assert r1["promise_id"] == r2["promise_id"]


def test_upsert_promise_different_audience_creates_separate_row(db):
    """Same content told to different people = two distinct promise rows."""
    r1 = work_items.upsert_promise(
        db, title="Project Atlas ships by Q3", made_to="CEO", topic="atlas",
    )
    r2 = work_items.upsert_promise(
        db, title="Project Atlas ships by Q3", made_to="CFO", topic="atlas",
    )
    assert r1["promise_id"] != r2["promise_id"]
    assert r1["created"] is True
    assert r2["created"] is True


def test_upsert_promise_latest_wins_appends_history(db):
    """Restate to same audience with different title → history grows, content updated."""
    r1 = work_items.upsert_promise(
        db, title="Atlas ships by Q3", made_to="CEO", topic="atlas",
    )
    r2 = work_items.upsert_promise(
        db, title="Atlas ships by Q4 (slipped)", made_to="CEO", topic="atlas",
    )
    # different content_hash → new row, NOT update
    # Because dedup is on (made_to, content_hash), and content_hash changed
    assert r2["created"] is True
    assert r1["promise_id"] != r2["promise_id"]


# ── query_promises ────────────────────────────────────────────────────


def test_query_promises_filter_by_made_to(db):
    work_items.upsert_promise(db, title="X", made_to="Aleks", topic="atlas")
    work_items.upsert_promise(db, title="Y", made_to="Tom", topic="atlas")
    work_items.upsert_promise(db, title="Z", made_to="Aleks Yordanov", topic="atlas")

    aleks = work_items.query_promises(db, made_to="Aleks")
    assert len(aleks) == 2  # Aleks + Aleks Yordanov (LIKE %Aleks%)


def test_query_promises_filter_by_topic(db):
    work_items.upsert_promise(db, title="A", made_to="X", topic="atlas")
    work_items.upsert_promise(db, title="B", made_to="X", topic="beacon")
    out = work_items.query_promises(db, topic="atlas")
    assert len(out) == 1
    assert out[0]["title"] == "A"


def test_query_promises_does_not_include_regular_actions(db):
    """Promises and actions share a table but query_promises filters kind='promise'."""
    # Insert a normal action
    work_items.upsert(db, item_type="action", title="regular action", owner="me")
    # Insert a promise
    work_items.upsert_promise(db, title="a promise", made_to="X")

    promises = work_items.query_promises(db)
    titles = [p["title"] for p in promises]
    assert "a promise" in titles
    assert "regular action" not in titles


# ── find_promise_inconsistencies ──────────────────────────────────────


def test_inconsistencies_detects_different_titles_same_topic(db):
    work_items.upsert_promise(db, title="Atlas ships Q3", made_to="CEO", topic="atlas")
    work_items.upsert_promise(db, title="Atlas ships end of year", made_to="CFO", topic="atlas")

    conflicts = work_items.find_promise_inconsistencies(db)
    assert len(conflicts) == 1
    assert conflicts[0]["topic"] == "atlas"
    assert conflicts[0]["promise_count"] == 2
    assert conflicts[0]["distinct_titles"] == 2
    audiences = [p["made_to"] for p in conflicts[0]["promises"]]
    assert "CEO" in audiences
    assert "CFO" in audiences


def test_inconsistencies_no_conflict_when_same_content(db):
    """Same promise made to two different people is fine — that's consistent."""
    work_items.upsert_promise(db, title="Atlas ships Q3", made_to="CEO", topic="atlas")
    work_items.upsert_promise(db, title="Atlas ships Q3", made_to="CFO", topic="atlas")

    conflicts = work_items.find_promise_inconsistencies(db)
    assert conflicts == []


def test_inconsistencies_detects_due_date_mismatch(db):
    work_items.upsert_promise(
        db, title="Atlas ships", made_to="CEO", topic="atlas", due_date="2026-09-30",
    )
    work_items.upsert_promise(
        db, title="Atlas ships", made_to="CFO", topic="atlas", due_date="2026-12-31",
    )
    conflicts = work_items.find_promise_inconsistencies(db)
    assert len(conflicts) == 1
    assert conflicts[0]["distinct_due_dates"] == 2


def test_inconsistencies_filter_by_topic(db):
    work_items.upsert_promise(db, title="A1", made_to="X", topic="atlas")
    work_items.upsert_promise(db, title="A2", made_to="Y", topic="atlas")
    work_items.upsert_promise(db, title="B1", made_to="X", topic="beacon")
    work_items.upsert_promise(db, title="B2", made_to="Y", topic="beacon")

    out = work_items.find_promise_inconsistencies(db, topic="atlas")
    assert len(out) == 1
    assert out[0]["topic"] == "atlas"


def test_inconsistencies_ignores_topicless_promises(db):
    """Promises with no topic can't be matched, so they don't appear in conflicts."""
    work_items.upsert_promise(db, title="A", made_to="X")
    work_items.upsert_promise(db, title="B", made_to="Y")
    assert work_items.find_promise_inconsistencies(db) == []


# ── MCP tool handlers ─────────────────────────────────────────────────


@pytest.fixture
def project_home(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    monkeypatch.setenv("SECONDBRAIN_HOME", str(tmp_path))
    return tmp_path


def test_handle_upsert_promise(project_home):
    from secondbrain.mcp.server import _handle_upsert_promise
    r = _handle_upsert_promise({
        "title": "Atlas ships Q3", "made_to": "CEO",
        "audience_type": "executive", "topic": "atlas",
    })
    assert r["created"] is True
    assert r["promise_id"] > 0


def test_handle_upsert_promise_rejects_bad_audience_type(project_home):
    from secondbrain.mcp.server import _handle_upsert_promise
    r = _handle_upsert_promise({
        "title": "X", "made_to": "Y", "audience_type": "random",
    })
    assert "error" in r


def test_handle_check_promises_filter_mode(project_home):
    from secondbrain.mcp.server import _handle_upsert_promise, _handle_check_promises
    _handle_upsert_promise({"title": "A", "made_to": "Aleks", "topic": "atlas"})
    _handle_upsert_promise({"title": "B", "made_to": "Tom", "topic": "atlas"})

    out = _handle_check_promises({"mode": "filter", "made_to": "Aleks"})
    assert out["mode"] == "filter"
    assert out["count"] == 1


def test_handle_check_promises_inconsistencies(project_home):
    from secondbrain.mcp.server import _handle_upsert_promise, _handle_check_promises
    _handle_upsert_promise({
        "title": "Atlas ships Q3", "made_to": "CEO",
        "audience_type": "executive", "topic": "atlas",
    })
    _handle_upsert_promise({
        "title": "Atlas ships end of year", "made_to": "CFO",
        "audience_type": "executive", "topic": "atlas",
    })

    out = _handle_check_promises({"mode": "inconsistencies"})
    assert out["mode"] == "inconsistencies"
    assert out["count"] == 1
    assert out["conflicts"][0]["topic"] == "atlas"
