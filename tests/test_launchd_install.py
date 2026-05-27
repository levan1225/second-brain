"""Tests for `sb daemon install` — launchd plist generation + cron translation.

The actual `launchctl load` step is not exercised here (we'd need a real launchd
session). We verify:
  - cron strings translate correctly to StartCalendarInterval slots
  - dow expansion handles 'mon-fri', '*', '1,3,5', etc.
  - plist XML is well-formed and includes all the right keys
  - dry-run mode prints without writing
"""

from __future__ import annotations

from pathlib import Path

import pytest

from secondbrain.cli.commands.daemon import (
    _job_cron_to_calendar_interval,
    _make_launchd_plist,
    _parse_dow,
)


# ── _parse_dow ─────────────────────────────────────────────────────────


def test_parse_dow_star():
    assert _parse_dow("*") == [0, 1, 2, 3, 4, 5, 6]


def test_parse_dow_mon_fri():
    assert _parse_dow("mon-fri") == [1, 2, 3, 4, 5]


def test_parse_dow_weekend_named():
    assert _parse_dow("sat,sun") == [0, 6]  # Sun=0, Sat=6


def test_parse_dow_numeric_range():
    assert _parse_dow("1-5") == [1, 2, 3, 4, 5]


def test_parse_dow_numeric_list():
    assert _parse_dow("0,3,5") == [0, 3, 5]


def test_parse_dow_wraparound():
    # Cron-legal: 'fri-mon' means Fri,Sat,Sun,Mon
    result = _parse_dow("fri-mon")
    assert set(result) == {1, 5, 6, 0}


def test_parse_dow_mixed():
    assert _parse_dow("mon-wed,fri") == [1, 2, 3, 5]


# ── _job_cron_to_calendar_interval ─────────────────────────────────────


def test_cron_string_weekdays_morning():
    """'45 16 * * mon-fri' → 5 slots, each at 16:45 on weekday 1-5"""
    slots = _job_cron_to_calendar_interval({"cron": "45 16 * * mon-fri"})
    assert len(slots) == 5
    weekdays = {s["Weekday"] for s in slots}
    assert weekdays == {1, 2, 3, 4, 5}
    for s in slots:
        assert s["Hour"] == 16
        assert s["Minute"] == 45


def test_cron_dict_form():
    slots = _job_cron_to_calendar_interval({"cron": {"hour": 7, "minute": 0, "day_of_week": "mon-fri"}})
    assert len(slots) == 5
    assert all(s["Hour"] == 7 and s["Minute"] == 0 for s in slots)


def test_cron_every_day():
    slots = _job_cron_to_calendar_interval({"cron": "0 7 * * *"})
    assert len(slots) == 7
    assert {s["Weekday"] for s in slots} == {0, 1, 2, 3, 4, 5, 6}


def test_cron_returns_empty_on_no_cron():
    assert _job_cron_to_calendar_interval({}) == []
    assert _job_cron_to_calendar_interval({"interval_seconds": 300}) == []


def test_cron_invalid_string_raises():
    with pytest.raises(ValueError):
        _job_cron_to_calendar_interval({"cron": "not a real cron"})


# ── _make_launchd_plist ────────────────────────────────────────────────


def test_plist_contains_required_keys():
    xml = _make_launchd_plist(
        "morning_brief",
        {"cron": "0 7 * * mon-fri"},
        sb_bin="/usr/local/bin/sb",
        project_home="/Users/x/Documents/Second Brain/test",
        log_dir="/Users/x/.config/secondbrain/logs",
    )
    assert "com.secondbrain.morning_brief" in xml
    assert "<key>Label</key>" in xml
    assert "<key>ProgramArguments</key>" in xml
    assert "<string>/usr/local/bin/sb</string>" in xml
    assert "<string>daemon</string>" in xml
    assert "<string>run-once</string>" in xml
    assert "<string>morning_brief</string>" in xml
    assert "<key>SECONDBRAIN_HOME</key>" in xml
    assert "<key>StandardOutPath</key>" in xml


def test_plist_includes_all_weekday_slots():
    xml = _make_launchd_plist(
        "test_job",
        {"cron": "30 9 * * mon-fri"},
        sb_bin="/x/sb",
        project_home="/x",
        log_dir="/x/logs",
    )
    # Should have 5 <dict> entries inside StartCalendarInterval — one per weekday
    sci_section = xml.split("StartCalendarInterval")[1].split("</array>")[0]
    assert sci_section.count("<dict>") == 5


def test_plist_handles_interval_schedule():
    """interval_seconds → StartInterval (not StartCalendarInterval)"""
    xml = _make_launchd_plist(
        "pre_meeting",
        {"interval_seconds": 300},
        sb_bin="/x/sb",
        project_home="/x",
        log_dir="/x/logs",
    )
    assert "<key>StartInterval</key>" in xml
    assert "<integer>300</integer>" in xml
    assert "StartCalendarInterval" not in xml


def test_plist_is_well_formed_xml():
    import xml.etree.ElementTree as ET
    xml = _make_launchd_plist(
        "morning_brief",
        {"cron": "0 7 * * mon-fri"},
        sb_bin="/usr/local/bin/sb",
        project_home="/Users/x",
        log_dir="/Users/x/logs",
    )
    # Should parse without exception
    root = ET.fromstring(xml)
    assert root.tag == "plist"
    # Top-level is <dict>
    assert root[0].tag == "dict"


def test_install_command_exists_and_help():
    """The CLI surface should expose `sb daemon install`."""
    from secondbrain.cli.commands.daemon import daemon
    from click.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(daemon, ["install", "--help"])
    assert result.exit_code == 0
    assert "launchd" in result.output.lower() or "systemd" in result.output.lower()
    assert "--uninstall" in result.output
    assert "--dry-run" in result.output


def test_install_dry_run_does_not_write(tmp_path, monkeypatch):
    """--dry-run should print the plist but not touch the filesystem."""
    from secondbrain.cli.commands.daemon import daemon
    from click.testing import CliRunner

    # Point HOME at an isolated tmp_path so we don't pollute the real LaunchAgents
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SECONDBRAIN_HOME", str(tmp_path))
    # Need a project_home to exist for Workspace() to succeed
    (tmp_path / "state").mkdir()

    runner = CliRunner()
    result = runner.invoke(daemon, ["install", "--dry-run"])
    # Should succeed
    assert result.exit_code == 0
    # No plist files written
    agents_dir = tmp_path / "Library" / "LaunchAgents"
    assert not agents_dir.exists() or not list(agents_dir.glob("*.plist"))
    # Should print something
    assert "dry-run" in result.output.lower() or "would" in result.output.lower()
