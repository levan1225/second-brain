"""Tests for the FastAPI web app — uses TestClient, no live server."""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from secondbrain.core import work_items
from secondbrain.web.app import create_app, _markdown_to_html


@pytest.fixture
def project_home(tmp_path, monkeypatch):
    """Build a populated project home + point SECONDBRAIN_HOME at it."""
    for sub in (
        "state", "wiki/people", "wiki/projects",
        "output/daemon/briefings", "config/canonical",
    ):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)

    # Seed work_items
    db = tmp_path / "state" / "workbench.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    work_items.ensure_schema(conn)
    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()
    work_items.upsert(conn, item_type="action", title="overdue thing", owner="Tom",
                      due_date=yesterday)
    work_items.upsert(conn, item_type="action", title="due today thing", owner="Sarah",
                      due_date=today.isoformat())
    work_items.upsert(conn, item_type="action", title="due tomorrow thing", owner="Tom",
                      due_date=tomorrow)
    conn.close()

    # Seed a person page
    (tmp_path / "wiki" / "people" / "tom.md").write_text(
        f"""---
title: Tom
role: Senior Engineer
relationship: Peer
trust_tier: trusted
last_updated: {today.isoformat()}
---

# Tom

Some body content.
""",
        encoding="utf-8",
    )

    # Seed a briefing
    (tmp_path / "output" / "daemon" / "briefings" / "2026-05-19-morning.md").write_text(
        "# Morning briefing\n\n## Overdue (1)\n- thing\n", encoding="utf-8",
    )

    monkeypatch.setenv("SECONDBRAIN_HOME", str(tmp_path))
    # Important: ensure stale CONFIG_PATH doesn't shadow us
    return tmp_path


@pytest.fixture
def client(project_home):
    return TestClient(create_app())


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "version" in body


def test_dashboard_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "secondbrain" in r.text
    assert "overdue thing" in r.text  # the overdue row should appear
    assert "due today thing" in r.text


def test_dashboard_json(client):
    r = client.get("/?format=json")
    assert r.status_code == 200
    body = r.json()
    assert body["buckets"]["overdue"] == 1
    assert body["buckets"]["due_today"] == 1
    assert body["buckets"]["due_this_week"] == 1


def test_people_list(client):
    r = client.get("/people")
    assert r.status_code == 200
    assert "Tom" in r.text
    assert "Senior Engineer" in r.text


def test_people_list_json(client):
    r = client.get("/people?format=json")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["people"][0]["slug"] == "tom"
    # Tom has 2 open commitments (overdue + tomorrow)
    assert body["people"][0]["open_count"] == 2


def test_person_detail(client):
    r = client.get("/people/tom")
    assert r.status_code == 200
    assert "Tom" in r.text
    assert "Senior Engineer" in r.text
    # His commitments should appear
    assert "overdue thing" in r.text


def test_person_detail_404(client):
    r = client.get("/people/nonexistent-slug")
    assert r.status_code == 404


def test_commitments(client):
    r = client.get("/commitments")
    assert r.status_code == 200
    assert "overdue thing" in r.text
    assert "Overdue" in r.text


def test_commitments_filtered_owner(client):
    r = client.get("/commitments?owner=Sarah&format=json")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["rows"][0]["owner"] == "Sarah"


def test_briefings_list(client):
    r = client.get("/briefings")
    assert r.status_code == 200
    assert "2026-05-19-morning.md" in r.text


def test_briefing_detail(client):
    r = client.get("/briefings/2026-05-19-morning.md")
    assert r.status_code == 200
    assert "Morning briefing" in r.text


def test_briefing_detail_404(client):
    r = client.get("/briefings/does-not-exist.md")
    assert r.status_code == 404


def test_briefing_path_traversal_blocked(client):
    r = client.get("/briefings/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code in (400, 404)


def test_connectors_view(client):
    r = client.get("/connectors")
    assert r.status_code == 200
    # v3.0 ships no built-in connectors — page should still render (empty table)
    # The page mentions third-party install via sb connect
    assert "Connectors" in r.text


def test_daemon_view(client):
    r = client.get("/daemon")
    assert r.status_code == 200


# ── markdown renderer unit tests ─────────────────────────────────────────


def test_markdown_renders_headings():
    html = _markdown_to_html("# Title\n\n## Sub\n\nbody")
    assert "<h1" in html
    assert "<h2" in html
    assert "Title" in html


def test_markdown_renders_bullets():
    html = _markdown_to_html("- one\n- two\n- three")
    assert "<ul" in html
    assert "<li" in html
    assert html.count("<li") == 3


def test_markdown_escapes_html():
    html = _markdown_to_html("<script>alert('xss')</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_markdown_renders_bold_and_links():
    html = _markdown_to_html("**bold** and [link](https://example.com)")
    assert "<strong>bold</strong>" in html
    assert 'href="https://example.com"' in html
