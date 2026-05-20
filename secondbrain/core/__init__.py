"""secondbrain.core — the shared library.

Pure Python: no LLM, no HTTP, no MCP dependencies. This is what every plane
(cli, daemon, web, mcp) imports. Talks SQLite + markdown + YAML.

Public surface:
    Workspace       — resolves project home, opens DB, exposes helpers
    db              — low-level connection + migrations
    wiki            — markdown read/write with frontmatter
    work_items      — canonical store reads + upserts
    people          — config/canonical/people.yaml reads + upserts
"""

from .workspace import Workspace

__all__ = ["Workspace"]
