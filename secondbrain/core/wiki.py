"""Markdown read/write with YAML frontmatter.

Obsidian-compatible. No fancy parser — the file format is documented as
`---\\n{yaml}\\n---\\n{body}` and we follow it literally.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class WikiPage:
    """A single markdown page with parsed frontmatter."""

    def __init__(self, path: Path, frontmatter: dict[str, Any], body: str):
        self.path = path
        self.frontmatter = frontmatter
        self.body = body

    @classmethod
    def read(cls, path: Path) -> "WikiPage":
        if not path.exists():
            return cls(path, {}, "")
        raw = path.read_text(encoding="utf-8")
        fm: dict[str, Any] = {}
        body = raw
        if raw.startswith("---\n"):
            end = raw.find("\n---\n", 4)
            if end > 0:
                try:
                    fm = yaml.safe_load(raw[4:end]) or {}
                except yaml.YAMLError:
                    fm = {}
                body = raw[end + 5 :]
        return cls(path, fm, body)

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fm_yaml = yaml.safe_dump(self.frontmatter, sort_keys=False, default_flow_style=False)
        out = f"---\n{fm_yaml}---\n{self.body}"
        self.path.write_text(out, encoding="utf-8")

    @property
    def slug(self) -> str:
        return self.path.stem


def read_page(wiki_root: Path, relative: str) -> WikiPage:
    """Read a page by relative path under wiki/, e.g. 'people/tom.md'."""
    return WikiPage.read(wiki_root / relative)


def list_pages(wiki_root: Path, category: str) -> list[WikiPage]:
    """List all .md pages under wiki/{category}/."""
    subdir = wiki_root / category
    if not subdir.exists():
        return []
    return [WikiPage.read(p) for p in sorted(subdir.glob("*.md"))]
