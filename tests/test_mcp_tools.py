"""Tests for the v3 MCP tool handlers — directly invoking _handle_* functions.

Skips the JSON-RPC layer; we already know the SDK works. These tests cover
the v2-port additions: list_cadences, generate_person_context, and the
multi-mode prepare_meeting (person / project / cadence).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from secondbrain.core import work_items
from secondbrain.core.workspace import Workspace


@pytest.fixture
def project_home(tmp_path, monkeypatch):
    """A populated workspace with people, projects, cadences, work_items."""
    for sub in (
        "state", "wiki/people", "wiki/projects", "wiki/patterns",
        "wiki/context", "config/canonical", "output",
    ):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)

    # work_items
    conn = sqlite3.connect(str(tmp_path / "state" / "workbench.db"))
    conn.row_factory = sqlite3.Row
    work_items.ensure_schema(conn)

    work_items.upsert(conn, item_type="action", title="Tom's overdue thing",
                      owner="Tom Chen", due_date="2026-05-15")
    work_items.upsert(conn, item_type="action", title="Sarah's review",
                      owner="Sarah Lee", requester="Tom Chen", due_date="2026-05-25")
    work_items.upsert(conn, item_type="action", title="Update FY27 plan slides",
                      owner="Van", due_date="2026-05-22")

    # Seed cadence_registry rows (would have come from migrations in real DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cadence_registry (
            cadence_key TEXT PRIMARY KEY,
            timezone TEXT DEFAULT 'America/Los_Angeles',
            boundary_rule TEXT NOT NULL,
            interval_weeks INTEGER DEFAULT 1,
            enabled INTEGER DEFAULT 1,
            description TEXT DEFAULT ''
        )
    """)
    conn.execute(
        "INSERT INTO cadence_registry (cadence_key, boundary_rule, interval_weeks, description) "
        "VALUES (?, ?, ?, ?)",
        ("weekly_team", "weekly_fixed_weekday", 1, "Weekly team standup"),
    )
    conn.execute(
        "INSERT INTO cadence_registry (cadence_key, boundary_rule, interval_weeks, description) "
        "VALUES (?, ?, ?, ?)",
        ("biweekly_steerco", "biweekly_fixed_weekday", 2, "Biweekly SteerCo"),
    )
    conn.commit()
    conn.close()

    # People pages
    (tmp_path / "wiki" / "people" / "tom-chen.md").write_text(
        """---
title: Tom Chen
role: Senior Engineer
relationship: Peer
trust_tier: trusted
slack_user_id: U123ABC
email: tom@example.com
team: Platform
---

# Tom Chen

Long-time collaborator on infra migrations.
""", encoding="utf-8")

    (tmp_path / "wiki" / "people" / "sarah-lee.md").write_text(
        """---
title: Sarah Lee
role: VP, Platform
relationship: Skip-level
trust_tier: trusted
---

# Sarah Lee
""", encoding="utf-8")

    # Project page
    (tmp_path / "wiki" / "projects" / "fy27-plan.md").write_text(
        """---
title: FY27 Annual Plan
status: Active
---

# FY27 Annual Plan

Working with Tom Chen on the slides. Sarah Lee is the sponsor.
""", encoding="utf-8")

    # Patterns: decisions + voice-profiles
    (tmp_path / "wiki" / "patterns" / "decisions.md").write_text(
        """---
title: Decisions
---

# Decisions

## 2026-Q2

- 2026-05-10 — Tom Chen approved migration timeline
- 2026-05-12 — Decided to defer scope cut with Sarah Lee
- 2026-05-13 — Picked Kafka over RabbitMQ
""", encoding="utf-8")

    (tmp_path / "wiki" / "patterns" / "voice-profiles.md").write_text(
        """---
title: Voice Profiles
---

# Voice profiles

## To Tom Chen

Direct, technical. Skip pleasantries. Reference specific files.

## To Sarah Lee

Executive summary first. Always have a recommendation.
""", encoding="utf-8")

    monkeypatch.setenv("SECONDBRAIN_HOME", str(tmp_path))
    return tmp_path


# ── list_cadences ──────────────────────────────────────────────────────


def test_list_cadences_returns_registry(project_home):
    from secondbrain.mcp.server import _handle_list_cadences
    result = _handle_list_cadences({})
    assert result["count"] == 2
    keys = [c["cadence_key"] for c in result["cadences"]]
    assert "weekly_team" in keys
    assert "biweekly_steerco" in keys


# ── prepare_meeting: validation ────────────────────────────────────────


def test_prepare_meeting_requires_one_mode(project_home):
    from secondbrain.mcp.server import _handle_prepare_meeting
    result = _handle_prepare_meeting({})
    assert "error" in result
    assert "person" in result["error"] or "person" in str(result)


def test_prepare_meeting_rejects_multiple_modes(project_home):
    from secondbrain.mcp.server import _handle_prepare_meeting
    result = _handle_prepare_meeting({"person": "tom-chen", "cadence": "weekly_team"})
    assert "error" in result
    assert "only ONE" in result["error"]


# ── prepare_meeting(person=...) — unchanged contract ──────────────────


def test_prepare_meeting_person_mode(project_home):
    from secondbrain.mcp.server import _handle_prepare_meeting
    result = _handle_prepare_meeting({"person": "tom-chen"})
    assert result["mode"] == "person"
    assert result["person"]["slug"] == "tom-chen"
    assert result["person"]["name"] == "Tom Chen"
    # Tom owes "Tom's overdue thing"
    assert len(result["open_items"]["they_owe_you"]) >= 1
    # Tom is requester on "Sarah's review"
    assert len(result["open_items"]["you_owe_them"]) >= 1
    # Decisions mention Tom
    assert any("Tom" in d["line"] for d in result["recent_decisions"])
    # Voice profile mentions Tom
    assert "Tom Chen" in result["voice_excerpt"]


def test_prepare_meeting_person_fuzzy_match(project_home):
    from secondbrain.mcp.server import _handle_prepare_meeting
    result = _handle_prepare_meeting({"person": "Tom"})
    assert result["mode"] == "person"
    assert result["person"]["slug"] == "tom-chen"


def test_prepare_meeting_unknown_person(project_home):
    from secondbrain.mcp.server import _handle_prepare_meeting
    result = _handle_prepare_meeting({"person": "nonexistent"})
    assert "error" in result
    assert "available_slugs" in result


# ── prepare_meeting(project=...) ───────────────────────────────────────


def test_prepare_meeting_project_mode(project_home):
    from secondbrain.mcp.server import _handle_prepare_meeting
    result = _handle_prepare_meeting({"project": "fy27-plan"})
    assert result["mode"] == "project"
    assert result["project"]["slug"] == "fy27-plan"
    assert result["project"]["status"] == "Active"
    # Related work_items — should find "Update FY27 plan slides"
    related_titles = [r["title"] for r in result["related_open_items"]]
    assert any("FY27" in t for t in related_titles)


def test_prepare_meeting_unknown_project(project_home):
    from secondbrain.mcp.server import _handle_prepare_meeting
    result = _handle_prepare_meeting({"project": "nonexistent-project"})
    assert "error" in result


# ── prepare_meeting(cadence=...) — gracefully handles empty meeting data ──


def test_prepare_meeting_cadence_mode(project_home):
    from secondbrain.mcp.server import _handle_prepare_meeting
    result = _handle_prepare_meeting({"cadence": "weekly_team"})
    assert result["mode"] == "cadence"
    assert result["cadence"]["cadence_key"] == "weekly_team"
    # No meeting_series bound, so we expect the honest note
    assert result["note"] is not None
    assert "ingestion" in result["note"]
    # But work_items should still be returned for general prep
    assert len(result["recent_open_items"]) >= 1


def test_prepare_meeting_unknown_cadence(project_home):
    from secondbrain.mcp.server import _handle_prepare_meeting
    result = _handle_prepare_meeting({"cadence": "monthly_nonexistent"})
    assert "error" in result
    assert "available_cadences" in result
    assert "weekly_team" in result["available_cadences"]


# ── generate_person_context ─────────────────────────────────────────────


def test_generate_person_context_basic(project_home):
    from secondbrain.mcp.server import _handle_generate_person_context
    result = _handle_generate_person_context({"person": "tom-chen"})
    assert result["person"]["slug"] == "tom-chen"
    assert result["person"]["slack_user_id"] == "U123ABC"
    # Has decisions
    assert len(result["decisions_mentioning"]) >= 1
    # Has voice
    assert "Tom Chen" in result["voice_excerpt"]
    # Should find the FY27 project where Tom is mentioned
    project_slugs = [p["slug"] for p in result["related_projects"]]
    assert "fy27-plan" in project_slugs


def test_generate_person_context_no_slack_ingestion(project_home):
    """messages_canonical is empty — should report status, not crash."""
    from secondbrain.mcp.server import _handle_generate_person_context
    # Need messages_canonical table to exist; minimally create it for this test
    ws = Workspace()
    conn = ws.open_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages_canonical (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            conversation_id TEXT,
            user_id TEXT,
            body TEXT
        )
    """)
    conn.commit()
    result = _handle_generate_person_context({"person": "tom-chen", "include_messages": True})
    assert result["recent_messages"] == []
    assert "empty" in result["messages_status"].lower() or "0 messages" in result["messages_status"].lower()


def test_generate_person_context_include_messages_false(project_home):
    """include_messages=false skips the messages_canonical query entirely."""
    from secondbrain.mcp.server import _handle_generate_person_context
    result = _handle_generate_person_context({"person": "tom-chen", "include_messages": False})
    assert result["messages_status"] == "not_queried"


def test_generate_person_context_unknown(project_home):
    from secondbrain.mcp.server import _handle_generate_person_context
    result = _handle_generate_person_context({"person": "nope"})
    assert "error" in result
