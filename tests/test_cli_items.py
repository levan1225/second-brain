"""Tests for sb add / done / rm / show CLI commands.

Uses Click's CliRunner so we exercise the full command lifecycle without
invoking subprocess.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from secondbrain.cli.commands.items import add, done, rm, show
from secondbrain.cli.commands.identity import identity
from secondbrain.core import work_items


@pytest.fixture
def project_home(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    (tmp_path / "identity.md").write_text(
        '---\nowner: "Van Nguyen"\nrole: TPM\n---\n# Identity\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SECONDBRAIN_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def runner():
    return CliRunner()


# ── sb add ──────────────────────────────────────────────────────────────


def test_add_creates_work_item(project_home, runner):
    result = runner.invoke(add, ["Send Tom the migration plan", "--owner", "Tom", "--due", "2026-05-25"])
    assert result.exit_code == 0, result.output
    assert "added" in result.output.lower()
    assert "work_item #1" in result.output or "#1" in result.output


def test_add_defaults_requester_from_identity(project_home, runner):
    """If --requester not given, falls back to identity.owner."""
    result = runner.invoke(add, ["Test item", "--owner", "Sarah"])
    assert result.exit_code == 0
    # Verify in DB
    conn = sqlite3.connect(str(project_home / "state" / "workbench.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT requester FROM work_items WHERE title='Test item'").fetchone()
    assert row["requester"] == "Van Nguyen"


def test_add_explicit_requester_wins(project_home, runner):
    result = runner.invoke(add, ["Item X", "--owner", "Tom", "--requester", "CEO"])
    assert result.exit_code == 0
    conn = sqlite3.connect(str(project_home / "state" / "workbench.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT requester FROM work_items WHERE title='Item X'").fetchone()
    assert row["requester"] == "CEO"


def test_add_idempotent(project_home, runner):
    """Adding the same title twice updates instead of creating."""
    r1 = runner.invoke(add, ["Duplicate test"])
    r2 = runner.invoke(add, ["Duplicate test"])
    assert r1.exit_code == 0 and r2.exit_code == 0
    assert "added" in r1.output.lower()
    assert "updated" in r2.output.lower() or "Latest-Wins" in r2.output


# ── sb done ─────────────────────────────────────────────────────────────


def test_done_marks_status(project_home, runner):
    runner.invoke(add, ["Something to finish"])
    result = runner.invoke(done, ["1"])
    assert result.exit_code == 0
    assert "marked" in result.output.lower() or "done" in result.output.lower()

    conn = sqlite3.connect(str(project_home / "state" / "workbench.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM work_items WHERE id=1").fetchone()
    assert row["status"] == "done"


def test_done_already_done(project_home, runner):
    runner.invoke(add, ["x"])
    runner.invoke(done, ["1"])
    result = runner.invoke(done, ["1"])
    assert result.exit_code == 0
    assert "already done" in result.output.lower()


def test_done_unknown_id(project_home, runner):
    result = runner.invoke(done, ["999"])
    assert result.exit_code != 0
    assert "no work_item" in result.output.lower()


# ── sb rm ───────────────────────────────────────────────────────────────


def test_rm_with_force(project_home, runner):
    runner.invoke(add, ["delete me"])
    result = runner.invoke(rm, ["1", "--force"])
    assert result.exit_code == 0
    assert "deleted" in result.output.lower()

    conn = sqlite3.connect(str(project_home / "state" / "workbench.db"))
    row = conn.execute("SELECT id FROM work_items WHERE id=1").fetchone()
    assert row is None


def test_rm_without_force_aborts(project_home, runner):
    """Without --force, the prompt defaults to no — input='' should not delete."""
    runner.invoke(add, ["keep me"])
    result = runner.invoke(rm, ["1"], input="\n")
    # Should abort cleanly
    assert "aborted" in result.output.lower() or result.exit_code != 0

    conn = sqlite3.connect(str(project_home / "state" / "workbench.db"))
    row = conn.execute("SELECT id FROM work_items WHERE id=1").fetchone()
    assert row is not None


def test_rm_unknown_id(project_home, runner):
    result = runner.invoke(rm, ["999", "--force"])
    assert result.exit_code != 0


# ── sb show ─────────────────────────────────────────────────────────────


def test_show_displays_item(project_home, runner):
    runner.invoke(add, ["My item", "--owner", "Tom", "--stakes", "very important"])
    result = runner.invoke(show, ["1"])
    assert result.exit_code == 0
    assert "My item" in result.output
    assert "Tom" in result.output
    assert "very important" in result.output


def test_show_unknown_id(project_home, runner):
    result = runner.invoke(show, ["999"])
    assert result.exit_code != 0


# ── sb identity ─────────────────────────────────────────────────────────


def test_identity_show(project_home, runner):
    result = runner.invoke(identity, [])
    assert result.exit_code == 0
    assert "Van Nguyen" in result.output


def test_identity_set_via_flag(project_home, runner):
    result = runner.invoke(identity, ["set", "--owner", "New Name"])
    assert result.exit_code == 0
    assert "Updated" in result.output

    # Verify
    result2 = runner.invoke(identity, [])
    assert "New Name" in result2.output


def test_identity_set_multiple_fields(project_home, runner):
    result = runner.invoke(identity, [
        "set", "--role", "VP", "--team", "Eng", "--timezone", "ET",
    ])
    assert result.exit_code == 0
    # Verify
    import yaml
    raw = (project_home / "identity.md").read_text()
    fm_text = raw.split("---\n")[1]
    fm = yaml.safe_load(fm_text)
    assert fm["role"] == "VP"
    assert fm["team"] == "Eng"
    assert fm["timezone"] == "ET"


def test_identity_set_preserves_body(project_home, runner):
    """The markdown body below frontmatter shouldn't be lost on update."""
    result = runner.invoke(identity, ["set", "--owner", "Different"])
    assert result.exit_code == 0
    raw = (project_home / "identity.md").read_text()
    assert "# Identity" in raw  # body preserved
