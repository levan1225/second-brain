"""Tests for `sb scan` extraction logic.

Tests the heuristic commitment extractor against realistic transcript and
meeting-note fixtures.
"""

from __future__ import annotations

from secondbrain.cli.commands.scan import _extract_commitments


def test_extract_bullet_commitments() -> None:
    text = """
# Meeting notes

- [ ] Send the Q3 review draft to Tom (due: 2026-05-21)
- [ ] Update the architecture doc
- [x] Already done
"""
    out = _extract_commitments(text)
    titles = [c["title"].lower() for c in out]
    assert any("q3 review" in t for t in titles)
    assert any("architecture doc" in t for t in titles)
    # checked ones should NOT match
    assert not any("already done" in t for t in titles)


def test_extract_bullet_with_due_date() -> None:
    text = "- [ ] Send Tom the design doc (due: 2026-05-25)"
    out = _extract_commitments(text)
    assert len(out) == 1
    assert out[0]["due_date"] == "2026-05-25"


def test_extract_natural_language_commitment() -> None:
    text = "Van: I'll send Tom the design doc by Friday."
    out = _extract_commitments(text)
    titles = [c["title"].lower() for c in out]
    assert any("send tom" in t or "design doc" in t for t in titles)


def test_extract_dedups_repeats() -> None:
    text = """
- [ ] Send Tom the design doc
- [ ] Send Tom the design doc
"""
    out = _extract_commitments(text)
    # case-insensitive prefix dedup → only one
    assert len(out) == 1


def test_extract_empty_text() -> None:
    assert _extract_commitments("") == []
    assert _extract_commitments("just prose with no commitments") == []
