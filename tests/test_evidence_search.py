"""Tests for the qmd-backed evidence_search MCP tool.

Strategy: mock subprocess so the tests don't need qmd installed in CI.
We verify: dispatch picks the right qmd subcommand, scope filtering works,
JSON parsing handles real qmd output shapes, and the fallback path engages
correctly when qmd is missing or errors.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from secondbrain.core import work_items


@pytest.fixture
def project_home(tmp_path, monkeypatch):
    """Workspace with some wiki content + sources for the fallback path."""
    for sub in (
        "state", "wiki/people", "wiki/projects", "wiki/context",
        "sources/transcripts",
    ):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)

    (tmp_path / "wiki" / "people" / "tom.md").write_text(
        "---\ntitle: Tom\n---\n\nTom is on the Kafka migration project.\n",
        encoding="utf-8",
    )
    (tmp_path / "wiki" / "projects" / "kafka.md").write_text(
        "---\ntitle: Kafka Migration\n---\n\nWe picked Kafka over RabbitMQ in Q1.\n",
        encoding="utf-8",
    )
    (tmp_path / "sources" / "transcripts" / "2026-05-19.md").write_text(
        "Sarah: any update on the migration?\nTom: I'll ship the migration by Friday.\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("SECONDBRAIN_HOME", str(tmp_path))
    return tmp_path


# ── Mode validation ────────────────────────────────────────────────────


def test_evidence_search_rejects_empty_query(project_home):
    from secondbrain.mcp.server import _handle_evidence_search
    result = _handle_evidence_search({"query": ""})
    assert "error" in result


def test_evidence_search_rejects_bad_mode(project_home):
    from secondbrain.mcp.server import _handle_evidence_search
    result = _handle_evidence_search({"query": "migration", "mode": "fuzzy"})
    assert "error" in result
    assert "invalid mode" in result["error"]


# ── Fallback path: no qmd installed ───────────────────────────────────


def test_evidence_search_keyword_fallback_when_qmd_missing(project_home):
    from secondbrain.mcp.server import _handle_evidence_search
    with patch("secondbrain.mcp.server._qmd_available", return_value=False):
        result = _handle_evidence_search({"query": "Kafka"})
    assert result["backend"].startswith("keyword (fallback")
    assert result["qmd_available"] is False
    # Should find the "Kafka Migration" project page + the Tom mention
    paths = [h["path"] for h in result["hits"]]
    assert any("kafka" in p.lower() for p in paths)


def test_evidence_search_fallback_respects_scope(project_home):
    from secondbrain.mcp.server import _handle_evidence_search
    with patch("secondbrain.mcp.server._qmd_available", return_value=False):
        result_wiki = _handle_evidence_search({"query": "migration", "scope": "wiki"})
        result_src = _handle_evidence_search({"query": "migration", "scope": "sources"})

    wiki_paths = [h["path"] for h in result_wiki["hits"]]
    assert all(p.startswith("wiki/") for p in wiki_paths)
    src_paths = [h["path"] for h in result_src["hits"]]
    assert all(p.startswith("sources/") for p in src_paths)


# ── qmd happy path: mock the subprocess ───────────────────────────────


def _fake_qmd_run(stdout: str, returncode: int = 0):
    """Build a fake subprocess.CompletedProcess."""
    return MagicMock(stdout=stdout, returncode=returncode)


def test_evidence_search_uses_qmd_when_available(project_home):
    """When qmd is on PATH, dispatch should call qmd's BM25 `search` by default."""
    from secondbrain.mcp.server import _handle_evidence_search

    fake_qmd_output = json.dumps([
        {
            "docid": "#abc123",
            "score": 0.8,
            "file": "qmd://wiki/projects/kafka.md",
            "title": "Kafka Migration",
            "snippet": "We picked Kafka over RabbitMQ in Q1.",
        },
        {
            "docid": "#def456",
            "score": 0.5,
            "file": "qmd://wiki/people/tom.md",
            "title": "Tom",
            "snippet": "Tom is on the Kafka migration project.",
        },
    ])

    with patch("secondbrain.mcp.server._qmd_available", return_value=True), \
         patch("secondbrain.mcp.server._qmd_ensure_collections", return_value=[]), \
         patch("subprocess.run", return_value=_fake_qmd_run(fake_qmd_output)):
        result = _handle_evidence_search({"query": "Kafka"})

    # Default is keyword (BM25) — no model download required
    assert result["backend"] == "qmd:keyword"
    assert result["count"] == 2
    # qmd:// prefix stripped
    assert result["hits"][0]["path"] == "wiki/projects/kafka.md"
    assert result["hits"][0]["score"] == 0.8
    assert "snippet" in result["hits"][0]


def test_evidence_search_mode_keyword_uses_qmd_search(project_home):
    """mode=keyword should call `qmd search` not `qmd query`."""
    from secondbrain.mcp.server import _handle_evidence_search

    captured_args: list = []

    def fake_run(args, **kwargs):
        captured_args.append(args)
        return _fake_qmd_run("[]")

    with patch("secondbrain.mcp.server._qmd_available", return_value=True), \
         patch("secondbrain.mcp.server._qmd_command_prefix", return_value=[]), \
         patch("secondbrain.mcp.server._qmd_ensure_collections", return_value=[]), \
         patch("subprocess.run", side_effect=fake_run):
        result = _handle_evidence_search({"query": "X", "mode": "keyword"})

    assert result["backend"] == "qmd:keyword"
    # With prefix mocked to [], qmd appears at args[0]
    qmd_calls = [a for a in captured_args if a and a[0] == "qmd"]
    assert len(qmd_calls) == 1
    assert qmd_calls[0][1] == "search"  # `qmd search`, not `qmd query`


def test_evidence_search_mode_semantic_uses_vsearch(project_home):
    from secondbrain.mcp.server import _handle_evidence_search

    captured_args: list = []

    def fake_run(args, **kwargs):
        captured_args.append(args)
        return _fake_qmd_run("[]")

    with patch("secondbrain.mcp.server._qmd_available", return_value=True), \
         patch("secondbrain.mcp.server._qmd_command_prefix", return_value=[]), \
         patch("secondbrain.mcp.server._qmd_ensure_collections", return_value=[]), \
         patch("subprocess.run", side_effect=fake_run):
        result = _handle_evidence_search({"query": "X", "mode": "semantic"})

    qmd_calls = [a for a in captured_args if a and a[0] == "qmd"]
    assert qmd_calls[0][1] == "vsearch"


def test_qmd_command_prefix_matches_arch(project_home):
    """When proc arch matches host arch, no prefix should be added."""
    from secondbrain.mcp.server import _qmd_command_prefix
    import platform
    # In CI / on the dev box, Python and host should match → empty prefix
    prefix = _qmd_command_prefix()
    if platform.system() == "Darwin":
        # On Darwin, prefix is either empty (matched) or ['/usr/bin/arch', '-arm64'|'-x86_64']
        assert prefix == [] or (len(prefix) == 2 and prefix[1].startswith("-"))
    else:
        # Non-Darwin always returns empty
        assert prefix == []


def test_evidence_search_qmd_error_falls_back_to_keyword(project_home):
    """If qmd errors (timeout, non-zero exit, bad JSON), fall back to keyword search."""
    from secondbrain.mcp.server import _handle_evidence_search

    # qmd returns non-zero exit code
    with patch("secondbrain.mcp.server._qmd_available", return_value=True), \
         patch("secondbrain.mcp.server._qmd_ensure_collections", return_value=[]), \
         patch("subprocess.run", return_value=_fake_qmd_run("error", returncode=1)):
        result = _handle_evidence_search({"query": "Kafka"})

    # Should fall back — backend reflects the fallback path
    assert "fallback" in result["backend"].lower()


def test_evidence_search_qmd_bad_json_falls_back(project_home):
    from secondbrain.mcp.server import _handle_evidence_search

    with patch("secondbrain.mcp.server._qmd_available", return_value=True), \
         patch("secondbrain.mcp.server._qmd_ensure_collections", return_value=[]), \
         patch("subprocess.run", return_value=_fake_qmd_run("not json {")):
        result = _handle_evidence_search({"query": "Kafka"})

    assert "fallback" in result["backend"].lower()


def test_evidence_search_qmd_scope_filter(project_home):
    """When qmd returns mixed wiki+sources hits, scope=wiki should drop sources hits."""
    from secondbrain.mcp.server import _handle_evidence_search

    fake_output = json.dumps([
        {"docid": "1", "score": 0.9, "file": "qmd://wiki/projects/kafka.md",
         "title": "Kafka", "snippet": "..."},
        {"docid": "2", "score": 0.8, "file": "qmd://sources/transcripts/2026.md",
         "title": "Transcript", "snippet": "..."},
        {"docid": "3", "score": 0.7, "file": "qmd://wiki/people/tom.md",
         "title": "Tom", "snippet": "..."},
    ])

    with patch("secondbrain.mcp.server._qmd_available", return_value=True), \
         patch("secondbrain.mcp.server._qmd_ensure_collections", return_value=[]), \
         patch("subprocess.run", return_value=_fake_qmd_run(fake_output)):
        result = _handle_evidence_search({"query": "X", "scope": "wiki"})

    assert all(h["path"].startswith("wiki/") for h in result["hits"])
    assert result["count"] == 2  # the two wiki hits, sources hit filtered out
