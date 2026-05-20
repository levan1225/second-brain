"""Smoke tests — proves the package imports and core helpers work."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from secondbrain import __version__
from secondbrain.core import work_items
from secondbrain.core.wiki import WikiPage
from secondbrain.core.workspace import Workspace, WorkspaceError


def test_version() -> None:
    assert __version__.startswith("3.")


def test_workspace_resolves_explicit_path(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    assert ws.project_home == tmp_path.resolve()


def test_workspace_errors_when_path_missing() -> None:
    with pytest.raises(WorkspaceError):
        Workspace("/definitely/does/not/exist/secondbrain")


def test_workspace_info_empty_project(tmp_path: Path) -> None:
    info = Workspace(tmp_path).info()
    assert info["project_home"] == str(tmp_path.resolve())
    assert info["db_exists"] is False


def test_upsert_creates_and_dedups(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    work_items.ensure_schema(conn)

    r1 = work_items.upsert(
        conn,
        item_type="action",
        title="ship the v3 prototype",
        owner="vnguyen8",
        due_date="2026-05-25",
        source="self-dm://test",
    )
    assert r1["created"] is True

    r2 = work_items.upsert(
        conn,
        item_type="action",
        title="ship the v3 prototype",
        owner="vnguyen8",
        due_date="2026-05-25",
        source="self-dm://test",
    )
    assert r2["created"] is False
    assert r2["work_item_id"] == r1["work_item_id"]


def test_query_filters_overdue(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    work_items.ensure_schema(conn)

    # one overdue, one future
    work_items.upsert(conn, item_type="action", title="overdue thing", due_date="2020-01-01")
    work_items.upsert(conn, item_type="action", title="future thing", due_date="2099-01-01")

    overdue = work_items.query(conn, item_type="action", overdue=True)
    assert len(overdue) == 1
    assert overdue[0]["title"] == "overdue thing"


def test_wiki_page_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "test.md"
    page = WikiPage(path, {"title": "Test", "tags": ["foo"]}, "# Hello\n\nBody.\n")
    page.write()

    reread = WikiPage.read(path)
    assert reread.frontmatter["title"] == "Test"
    assert "Hello" in reread.body
