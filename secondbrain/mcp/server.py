"""secondbrain MCP server — exposes core helpers as MCP tools.

Tools exposed:
  query_work_items       — read open/overdue/all actions from the canonical store
  upsert_work_item       — idempotent commitment write with Latest-Wins
  list_people            — list all wiki/people/*.md
  get_person             — read one person's full wiki page
  read_wiki_page         — read any wiki page by relative path
  workspace_info         — paths + counts (mirror of `sb info`)

Run via:
  python -m secondbrain.mcp.server     # direct entrypoint
  sb mcp                                # CLI entry point (Phase 4 complete)
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from secondbrain import __version__
from secondbrain.core import work_items
from secondbrain.core.wiki import list_pages, read_page
from secondbrain.core.workspace import Workspace, WorkspaceError


# ── Tool definitions ─────────────────────────────────────────────────────

TOOL_DEFS: list[Tool] = [
    Tool(
        name="query_work_items",
        description=(
            "Query the canonical work_items store (commitments, risks, issues, decisions). "
            "Returns rows ordered by due date. Use this for 'what's overdue', 'what's due "
            "this week', 'what does Tom owe me'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "item_type": {
                    "type": "string",
                    "enum": ["action", "risk", "issue", "decision"],
                    "description": "Default: action",
                },
                "status": {
                    "type": "string",
                    "description": "open, done, blocked, cancelled",
                },
                "owner": {
                    "type": "string",
                    "description": "Substring match against owner name",
                },
                "overdue": {
                    "type": "boolean",
                    "description": "If true, only items with due_date < today",
                },
                "limit": {"type": "integer", "description": "Default 50"},
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="upsert_work_item",
        description=(
            "Idempotently create or update a work item. Dedup by (source, content_hash). "
            "If a row already matches, Latest-Wins: the new title replaces the old and the "
            "prior version goes into history. Returns work_item_id and `created` flag."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "item_type": {
                    "type": "string",
                    "enum": ["action", "risk", "issue", "decision"],
                },
                "title": {"type": "string"},
                "owner": {"type": "string"},
                "requester": {"type": "string"},
                "status": {"type": "string", "description": "Default: open"},
                "priority": {"type": "string"},
                "due_date": {"type": "string", "description": "ISO-8601, e.g. 2026-05-25"},
                "stakes": {"type": "string", "description": "Why this matters"},
                "source": {
                    "type": "string",
                    "description": "Provenance URI: slack://..., file://..., gdrive://..., self-dm://...",
                },
                "wiki_path": {
                    "type": "string",
                    "description": "Wiki bullet back-ref, e.g. wiki/context/commitments.md",
                },
            },
            "required": ["item_type", "title"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="list_people",
        description="List every person with a wiki page. Returns slug, name, role, relationship.",
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_person",
        description=(
            "Read a single person's wiki page (frontmatter + body) plus their open work_items. "
            "Use this before drafting a chase or prepping for a 1:1."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Person slug, e.g. 'tom-chen'"},
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="read_wiki_page",
        description=(
            "Read any wiki page by relative path (e.g. wiki/patterns/voice-profiles.md). "
            "Returns parsed frontmatter and body. Path-bounded — only reads under the project home."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "relative_path": {
                    "type": "string",
                    "description": "e.g. wiki/patterns/voice-profiles.md",
                },
            },
            "required": ["relative_path"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="workspace_info",
        description=(
            "Get workspace summary: project_home path, wiki page counts by category, "
            "work_items totals. Mirror of `sb info`."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_workspace_status",
        description=(
            "Get a one-shot status snapshot of the workspace for skills that need "
            "to decide whether to proceed. Returns hasData flag, freshness, "
            "wiki page counts, overdue/open/done counts, and any health watchouts. "
            "Used by :health, :steerco-prep, and any skill that needs to assess "
            "workspace readiness before acting."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_groups_and_people",
        description=(
            "Read config/canonical/people.yaml (the declared roster) joined with "
            "wiki/people/*.md frontmatter. Returns canonical_id → name → role → "
            "slack_user_id → relationship for every known person. Used by :chase "
            "(to resolve owners), :onboard-pilot (to check membership), :declare "
            "(to find duplicates), and any skill that needs the authoritative people list."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="evidence_search",
        description=(
            "Search across sources/ (transcripts, meeting notes, docs) AND wiki/ pages.\n\n"
            "Uses semantic search via qmd if installed (recommended — finds 'calendar "
            "nightmare' when query is 'scheduling pain'); falls back to keyword search "
            "otherwise. Returns hits with snippets, file paths, and relevance scores.\n\n"
            "Use for: 'what did Tom say about X', 'find the decision where we picked Kafka', "
            "'where is the migration plan', 'who's worried about the Q3 burn'.\n\n"
            "The `mode` arg lets you force a specific search type: 'hybrid' (semantic + "
            "BM25, qmd's `query` command — default), 'keyword' (qmd's `search` BM25 only, "
            "or built-in regex fallback if no qmd), 'semantic' (qmd's `vsearch` only)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term, phrase, or question.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["hybrid", "keyword", "semantic"],
                    "description": "Default: hybrid. Use 'keyword' for exact-match needs.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["all", "sources", "wiki"],
                    "description": "Default: all. Restrict to sources/ files or wiki/ pages.",
                },
                "limit": {"type": "integer", "description": "Default 10"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="prepare_meeting",
        description=(
            "Generate a pre-meeting brief. Three input modes:\n"
            "  • person — 1:1 prep for a single person\n"
            "  • project — project review prep, returns project wiki + open items + key stakeholders\n"
            "  • cadence — recurring meeting prep using the cadence_registry (weekly_team, "
            "biweekly_steerco, monthly_ceo_staff, etc.), returns meeting series info + recent "
            "occurrences + relevant work_items\n"
            "Pick the mode that matches what you're prepping for. Use before every 1:1, project "
            "review, or recurring meeting. The CLI equivalent is `sb prep <person>`."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": "Person slug or name. e.g. 'suneet-nandwani' or 'Suneet'.",
                },
                "project": {
                    "type": "string",
                    "description": "Project slug. e.g. 'fy27-annual-planning'.",
                },
                "cadence": {
                    "type": "string",
                    "description": "Cadence key from cadence_registry. e.g. 'weekly_team', 'biweekly_steerco'.",
                },
                "topic": {
                    "type": "string",
                    "description": "Optional meeting topic to scope the prep (e.g. 'SteerCo').",
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="upsert_promise",
        description=(
            "Record a promise YOU made to someone. Distinct from a work_item — this is "
            "what you SAID in a 1:1 / SteerCo / email, not a tracked deliverable.\n\n"
            "Use this when the user says things like:\n"
            "  'I told Aleks the IG draft would be done by next Friday'\n"
            "  'I committed to the CEO that Project Atlas ships by Q3'\n"
            "  'Promised the team we'd hire 3 more this quarter'\n\n"
            "Dedup: same content to same audience updates; same content to a different "
            "audience creates a separate row (multi-audience inconsistency tracking).\n\n"
            "audience_type values: 'executive', 'peer', 'team', 'external', 'self', 'unknown'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "What you promised, in your words."},
                "made_to": {"type": "string", "description": "Who you said it to. e.g. 'Aleksandar Yordanov' or 'CEO Staff'."},
                "audience_type": {
                    "type": "string",
                    "enum": ["executive", "peer", "team", "external", "self", "unknown"],
                    "description": "Default: unknown. Used for cross-audience inconsistency detection.",
                },
                "topic": {"type": "string", "description": "Free-form topic / initiative slug for grouping (e.g. 'project-atlas')."},
                "due_date": {"type": "string", "description": "ISO-8601 if a date was named."},
                "stakes": {"type": "string", "description": "Why this matters / consequence of slipping."},
                "source": {"type": "string", "description": "Provenance URI (slack://, transcript://, email://)."},
                "wiki_path": {"type": "string", "description": "Back-reference to wiki bullet, if logged narratively."},
                "status": {"type": "string", "description": "Default: open."},
            },
            "required": ["title", "made_to"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="check_promises",
        description=(
            "Query the Promise Ledger. Supports two modes:\n"
            "  • filter — list promises by made_to / topic / audience_type\n"
            "  • inconsistencies — find promises on the same topic told to different "
            "audiences with different content or due dates\n\n"
            "Use 'inconsistencies' before high-stakes meetings to catch 'I told the CEO Q3 "
            "but told the CFO end-of-year' situations before someone else does.\n\n"
            "Use 'filter' to answer 'what have I promised Aleks lately?' or 'what's open for "
            "Project Atlas?'"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["filter", "inconsistencies"],
                    "description": "Default: filter. Use 'inconsistencies' to detect contradictions.",
                },
                "made_to": {"type": "string", "description": "Filter mode: who you promised."},
                "topic": {"type": "string", "description": "Filter or inconsistencies: scope to a topic."},
                "audience_type": {"type": "string", "description": "Filter mode: by audience class."},
                "status": {"type": "string", "description": "Filter mode: default 'open'. Use 'all' for everything."},
                "limit": {"type": "integer", "description": "Default 50"},
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="query_aging_commitments",
        description=(
            "Return open work_items where someone OWES you something that's been "
            "sitting for N+ days. Bucketed by tier:\n"
            "  • gentle  (3+ days)  — friendly nudge\n"
            "  • draft   (5+ days)  — time for a chaser\n"
            "  • escalate (8+ days) — escalate, drop, or final nudge\n"
            "Use this when the user asks 'who's late', 'what am I waiting on', "
            "'who haven't I heard from'. Mirrors what the daemon writes to "
            "output/daemon/briefings/{date}-aging.md."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "min_days_aged": {
                    "type": "integer",
                    "description": "Override the lowest tier threshold (default 3).",
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="list_cadences",
        description=(
            "List all known meeting cadences from cadence_registry. Each cadence (e.g. "
            "'weekly_team', 'biweekly_steerco') represents a recurring meeting series. "
            "Used by prepare_meeting(cadence=...) and by skills that need to enumerate "
            "what kinds of meetings are tracked."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="generate_person_context",
        description=(
            "Rich person briefing — like get_person but enriched with:\n"
            "  • all open work_items where they're owner OR requester\n"
            "  • all decisions in wiki/patterns/decisions.md where they're mentioned\n"
            "  • their voice profile excerpt from wiki/patterns/voice-profiles.md\n"
            "  • recent canonical messages_canonical mentions (if Slack ingestion is configured)\n"
            "  • projects where they're a stakeholder (from wiki/projects/ scan)\n"
            "Use for deep prep before a high-stakes meeting or when you need to draft "
            "something IN their voice or anticipate how they'll react."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": "Person slug, e.g. 'suneet-nandwani'.",
                },
                "include_messages": {
                    "type": "boolean",
                    "description": "Include messages_canonical mentions (default true). Set false to skip if Slack ingestion empty.",
                },
            },
            "required": ["person"],
            "additionalProperties": False,
        },
    ),
]


# ── Tool handlers ─────────────────────────────────────────────────────────

def _result(payload: Any) -> list[TextContent]:
    """Serialize a Python value as a single TextContent with JSON body."""
    return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


def _handle_query_work_items(args: dict) -> Any:
    ws = Workspace()
    rows = work_items.query(
        ws.open_db(),
        item_type=args.get("item_type") or "action",
        status=args.get("status"),
        owner=args.get("owner"),
        overdue=bool(args.get("overdue")),
        limit=int(args.get("limit") or 50),
    )
    return {"count": len(rows), "rows": rows}


def _handle_upsert_work_item(args: dict) -> Any:
    ws = Workspace()
    return work_items.upsert(
        ws.open_db(),
        item_type=str(args["item_type"]),
        title=str(args["title"]),
        owner=str(args.get("owner") or ""),
        requester=str(args.get("requester") or ""),
        status=str(args.get("status") or "open"),
        priority=str(args.get("priority") or "medium"),
        due_date=str(args.get("due_date") or ""),
        stakes=str(args.get("stakes") or ""),
        source=str(args.get("source") or ""),
        wiki_path=str(args.get("wiki_path") or ""),
    )


def _handle_list_people(_args: dict) -> Any:
    ws = Workspace()
    pages = list_pages(ws.wiki_root, "people")
    return {
        "count": len(pages),
        "people": [
            {
                "slug": p.slug,
                "name": p.frontmatter.get("title"),
                "role": p.frontmatter.get("role"),
                "relationship": p.frontmatter.get("relationship"),
                "trust_tier": p.frontmatter.get("trust_tier"),
                "wiki_path": str(p.path.relative_to(ws.project_home)),
            }
            for p in pages
        ],
    }


def _handle_get_person(args: dict) -> Any:
    ws = Workspace()
    slug = str(args["slug"])
    page = read_page(ws.wiki_root, f"people/{slug}.md")
    if not page.frontmatter and not page.body.strip():
        return {"error": f"no wiki page at wiki/people/{slug}.md"}
    name = page.frontmatter.get("title", slug)
    open_items: list[dict] = []
    if ws.db_path.exists():
        try:
            open_items = work_items.query(
                ws.open_db(), item_type="action", status="open", owner=name, limit=50
            )
        except Exception:
            open_items = []
    return {
        "slug": page.slug,
        "frontmatter": page.frontmatter,
        "body": page.body,
        "open_work_items": open_items,
    }


def _handle_read_wiki_page(args: dict) -> Any:
    ws = Workspace()
    rel = str(args["relative_path"])
    # Path-bound: only read under wiki/
    if not rel.startswith("wiki/"):
        return {"error": "path must start with 'wiki/'"}
    page = read_page(ws.project_home, rel)
    return {
        "exists": page.path.exists(),
        "frontmatter": page.frontmatter,
        "body": page.body,
        "path": rel,
    }


def _handle_workspace_info(_args: dict) -> Any:
    return Workspace().info()


def _handle_get_workspace_status(_args: dict) -> Any:
    """v3-native workspace status. Returns hasData + freshness + counts + watchouts.

    Replaces v2's 200-line get_workspace_status with a clean snapshot the LLM
    can use to decide whether to proceed.
    """
    ws = Workspace()
    info = ws.info()
    # Local date — UTC would prematurely classify today's items as overdue after ~5pm Pacific
    today = datetime.now().date()
    today_iso = today.isoformat()
    week_cutoff = (today + timedelta(days=7)).isoformat()

    watchouts: list[str] = []
    has_db = info.get("db_exists", False)

    open_count = 0
    overdue_count = 0
    due_this_week = 0
    done_count = 0

    if has_db:
        try:
            conn = ws.open_db()
            work_items.ensure_schema(conn)
            for r in conn.execute(
                "SELECT status, due_date FROM work_items WHERE item_type='action'"
            ).fetchall():
                status = (r["status"] or "").lower()
                due = r["due_date"] or ""
                if status in ("done", "closed", "cancelled"):
                    done_count += 1
                else:
                    open_count += 1
                    if due and due < today_iso:
                        overdue_count += 1
                    elif due and today_iso <= due <= week_cutoff:
                        due_this_week += 1
        except Exception as e:
            watchouts.append(f"work_items query failed: {e}")

    # Wiki freshness — count pages older than 30 days as stale.
    # Falls back to file mtime if the frontmatter date is missing or unparseable
    # (covers cases where last_updated is missing entirely, or in a non-ISO format
    # like 'May 14, 2026', which would otherwise either be silently ignored or
    # compared incorrectly as strings).
    stale_pages = 0
    cutoff_dt = today - timedelta(days=30)
    if ws.wiki_root.exists():
        from datetime import date as _date_cls
        for category in ("projects", "people"):
            for p in (ws.wiki_root / category).glob("*.md"):
                try:
                    page = read_page(ws.wiki_root, f"{category}/{p.name}")
                    raw = page.frontmatter.get("last_updated")
                    last_updated_dt = None

                    # YAML may parse '2026-04-04' as a date object
                    if isinstance(raw, _date_cls):
                        last_updated_dt = raw
                    elif isinstance(raw, str) and raw:
                        # Try ISO first; fall back to file mtime if unparseable
                        try:
                            last_updated_dt = datetime.fromisoformat(raw[:10]).date()
                        except (ValueError, TypeError):
                            last_updated_dt = None

                    if last_updated_dt is None:
                        # Fall back to file modification time
                        try:
                            mtime = p.stat().st_mtime
                            last_updated_dt = datetime.fromtimestamp(mtime).date()
                        except OSError:
                            continue

                    if last_updated_dt < cutoff_dt:
                        stale_pages += 1
                except Exception:
                    pass
    if stale_pages > 0:
        watchouts.append(f"{stale_pages} wiki pages haven't been touched in 30+ days")

    if overdue_count > 0:
        watchouts.append(f"{overdue_count} commitments are overdue")

    has_data = (
        has_db
        and (open_count + done_count) > 0
    ) or any(info.get(f"wiki_{c}", 0) > 0 for c in ("projects", "people"))

    return {
        "hasData": has_data,
        "asOf": today_iso,
        "projectHome": info["project_home"],
        "counts": {
            "open": open_count,
            "overdue": overdue_count,
            "dueThisWeek": due_this_week,
            "done": done_count,
            "wikiPeople": info.get("wiki_people", 0),
            "wikiProjects": info.get("wiki_projects", 0),
            "wikiPatterns": info.get("wiki_patterns", 0),
            "stalePages": stale_pages,
        },
        "watchouts": watchouts,
    }


def _handle_get_groups_and_people(_args: dict) -> Any:
    """Read config/canonical/people.yaml joined with wiki/people/*.md frontmatter."""
    ws = Workspace()
    people_yaml = ws.config_root / "canonical" / "people.yaml"

    yaml_entries: list[dict] = []
    if people_yaml.exists():
        try:
            data = yaml.safe_load(people_yaml.read_text(encoding="utf-8")) or []
            yaml_entries = data if isinstance(data, list) else []
        except yaml.YAMLError as e:
            return {"error": f"failed to parse people.yaml: {e}"}

    # Join with wiki pages by slug
    wiki_by_slug: dict[str, dict] = {}
    for page in list_pages(ws.wiki_root, "people"):
        wiki_by_slug[page.slug] = page.frontmatter

    people: list[dict] = []
    seen_slugs: set[str] = set()

    # Start from yaml (canonical), enrich with wiki
    for entry in yaml_entries:
        slug = entry.get("slug")
        if not slug:
            continue
        seen_slugs.add(slug)
        wiki_fm = wiki_by_slug.get(slug, {})
        people.append({
            "canonical_id": entry.get("id"),
            "slug": slug,
            "name": entry.get("name") or wiki_fm.get("title"),
            "slack_user_id": entry.get("slack_user_id") or wiki_fm.get("slack_user_id"),
            "email": entry.get("email") or wiki_fm.get("email"),
            "role": entry.get("role") or wiki_fm.get("role"),
            "team": entry.get("team") or wiki_fm.get("team"),
            "relationship": wiki_fm.get("relationship"),
            "trust_tier": wiki_fm.get("trust_tier"),
            "aliases": entry.get("aliases") or wiki_fm.get("aliases") or [],
            "declared": entry.get("declared", False),
            "wiki_path": entry.get("wiki_path") or f"wiki/people/{slug}.md",
        })

    # Add wiki-only people (no canonical row)
    for slug, fm in wiki_by_slug.items():
        if slug in seen_slugs:
            continue
        people.append({
            "canonical_id": None,
            "slug": slug,
            "name": fm.get("title"),
            "slack_user_id": fm.get("slack_user_id"),
            "email": fm.get("email"),
            "role": fm.get("role"),
            "team": fm.get("team"),
            "relationship": fm.get("relationship"),
            "trust_tier": fm.get("trust_tier"),
            "aliases": fm.get("aliases") or [],
            "declared": False,
            "wiki_path": f"wiki/people/{slug}.md",
        })

    return {
        "count": len(people),
        "people": people,
        "canonical_rows": len(yaml_entries),
        "wiki_pages": len(wiki_by_slug),
    }


# ── evidence_search helpers ──

_SNIPPET_BEFORE = 60
_SNIPPET_AFTER = 100


def _iter_searchable_files(ws: Workspace, scope: str) -> list[Path]:
    """Return all files we want to search, scoped to sources/ or wiki/ or both."""
    out: list[Path] = []
    if scope in ("all", "sources"):
        sources = ws.project_home / "sources"
        if sources.exists():
            for sub in ("transcripts", "meeting-notes", "docs", "slack"):
                p = sources / sub
                if p.exists():
                    for f in p.rglob("*"):
                        if f.is_file() and f.suffix.lower() in (".md", ".txt", ".vtt"):
                            out.append(f)
    if scope in ("all", "wiki"):
        if ws.wiki_root.exists():
            for f in ws.wiki_root.rglob("*.md"):
                if f.is_file():
                    out.append(f)
    return out


def _make_snippet(text: str, match_start: int, match_end: int) -> str:
    """Return a short context window around a match position."""
    start = max(0, match_start - _SNIPPET_BEFORE)
    end = min(len(text), match_end + _SNIPPET_AFTER)
    snippet = text[start:end].strip().replace("\n", " ")
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


# ── qmd integration ──────────────────────────────────────────────────────


def _qmd_available() -> bool:
    """Cheap check: is the qmd binary on PATH?"""
    import shutil
    return shutil.which("qmd") is not None


def _qmd_command_prefix() -> list[str]:
    """Return a list of args to prefix `qmd ...` calls with.

    On macOS, the qmd npm package's native `better-sqlite3` module is compiled
    for the *host* CPU architecture. If our Python process is running under
    Rosetta (x86_64 on Apple Silicon hardware), spawned subprocesses inherit
    x86_64 and `node` fails to load the arm64 native module. Workaround:
    prefix `arch -arm64` so the child runs natively.

    Detection: ask `sysctl` for the actual hardware (it always reports the
    host's true arch, ignoring Rosetta translation) and compare to our own
    Python arch. If they differ, prefix.
    """
    import platform
    import shutil
    import subprocess

    if platform.system() != "Darwin":
        return []

    # Our Python's runtime arch
    proc_arch = platform.machine()  # 'x86_64' under Rosetta, 'arm64' if native

    # True host arch — sysctl reads the hardware register, immune to Rosetta
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.optional.arm64"],
            capture_output=True, text=True, timeout=2,
        )
        is_arm_host = result.stdout.strip() == "1"
    except Exception:
        return []

    host_arch = "arm64" if is_arm_host else "x86_64"

    if proc_arch == host_arch:
        return []  # match — no prefix needed

    # Mismatch (likely Rosetta x86_64 Python on arm64 hardware) — force host arch
    arch_bin = shutil.which("arch") or "/usr/bin/arch"
    return [arch_bin, f"-{host_arch}"]


def _qmd_ensure_collections(ws: Workspace) -> list[str]:
    """Make sure this workspace's wiki + sources are indexed by qmd.

    Idempotent — qmd's `collection add` is a no-op for existing paths. Returns
    the list of collection roots we tried to add. Best-effort: failures don't
    raise; we just log to stderr and continue with whatever's indexed.
    """
    import subprocess
    indexed: list[str] = []
    prefix = _qmd_command_prefix()
    for sub in ("wiki", "sources"):
        path = ws.project_home / sub
        if not path.exists():
            continue
        try:
            # `qmd collection add` returns 0 even if already indexed
            subprocess.run(
                prefix + ["qmd", "collection", "add", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            indexed.append(str(path))
        except Exception:
            pass
    return indexed


def _qmd_search(query: str, *, mode: str, limit: int) -> list[dict] | None:
    """Call qmd. Returns parsed hits or None on any error (so caller can fall back).

    mode → qmd command:
      keyword  → qmd search       (BM25 only — fastest, no model download required)
      hybrid   → qmd query        (semantic + BM25 + rerank; requires LLM model on disk)
      semantic → qmd vsearch      (vector only; requires embeddings: `qmd embed`)

    qmd writes progress / spinner output to stdout when running interactively.
    We add `--no-interactive` where supported and tolerate noise by extracting
    the trailing JSON array from the output rather than blindly parsing the
    whole stdout buffer.
    """
    import json as _json
    import re as _re
    import subprocess

    cmd_map = {"hybrid": "query", "keyword": "search", "semantic": "vsearch"}
    subcmd = cmd_map.get(mode, "search")

    # `--limit` flag is supported for all three; we also set TERM=dumb to
    # suppress most of qmd's ANSI/spinner output.
    env = dict(os.environ)
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"

    prefix = _qmd_command_prefix()
    try:
        result = subprocess.run(
            prefix + ["qmd", subcmd, query, "--json", "--limit", str(limit)],
            capture_output=True, text=True, timeout=30,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return None
    except FileNotFoundError:
        return None
    except Exception:
        return None

    if result.returncode != 0:
        return None

    # qmd may emit progress/spinner output to stdout before the JSON.
    # Strategy: try parsing the whole buffer first; on failure, extract from
    # the first '[' to the end (qmd always emits a JSON array).
    stdout = result.stdout
    try:
        return _json.loads(stdout)
    except (_json.JSONDecodeError, ValueError):
        pass

    # Strip ANSI escapes
    ansi_re = _re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
    stripped = ansi_re.sub("", stdout)
    # Find the JSON array — start from the LAST '[' on a line that looks like JSON start
    # (qmd's final output begins with `[\n` after progress chatter).
    start = stripped.rfind("\n[")
    if start < 0:
        start = stripped.find("[")
    if start < 0:
        return None
    candidate = stripped[start:].strip()
    try:
        return _json.loads(candidate)
    except (_json.JSONDecodeError, ValueError):
        return None


def _qmd_path_to_relative(qmd_file: str, ws: Workspace) -> str:
    """qmd returns paths like 'qmd://wiki/people/foo.md' — strip the prefix."""
    if qmd_file.startswith("qmd://"):
        return qmd_file[len("qmd://"):]
    # Or absolute path under project_home — make relative
    try:
        return str(Path(qmd_file).resolve().relative_to(ws.project_home))
    except Exception:
        return qmd_file


def _filter_by_scope(hits: list[dict], scope: str) -> list[dict]:
    """Drop qmd hits that don't belong to the requested scope."""
    if scope == "all":
        return hits
    if scope == "wiki":
        return [h for h in hits if h.get("path", "").startswith("wiki/")]
    if scope == "sources":
        return [h for h in hits if h.get("path", "").startswith("sources/")]
    return hits


# ── Built-in keyword fallback (used when qmd unavailable) ───────────────


def _keyword_search(ws: Workspace, query: str, scope: str, limit: int) -> dict:
    """Original regex-based search. Used as fallback when qmd is absent or fails."""
    files = _iter_searchable_files(ws, scope)
    pattern = re.compile(r"\b" + re.escape(query) + r"\b", re.IGNORECASE)

    hits: list[dict] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in pattern.finditer(text):
            rel_path = str(f.relative_to(ws.project_home))
            hits.append({
                "path": rel_path,
                "snippet": _make_snippet(text, m.start(), m.end()),
                "position": m.start(),
            })
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break

    return {
        "count": len(hits),
        "files_searched": len(files),
        "hits": hits,
    }


def _handle_evidence_search(args: dict) -> Any:
    """Hybrid semantic search via qmd, falling back to keyword if qmd is unavailable.

    Always returns a `backend` field so callers know what they got:
      backend = "qmd:hybrid" | "qmd:keyword" | "qmd:semantic" | "keyword (fallback)"
    """
    query = str(args["query"]).strip()
    if not query:
        return {"error": "empty query"}

    # Default mode = keyword (qmd's BM25 `search` subcommand). Fast, no model
    # download. Hybrid + semantic require the user to have run `qmd embed`
    # (semantic) and downloaded the query-expansion LLM model (hybrid) ahead
    # of time. Treating them as opt-ins keeps the first-call experience snappy.
    mode = str(args.get("mode") or "keyword").lower()
    if mode not in ("hybrid", "keyword", "semantic"):
        return {"error": f"invalid mode '{mode}', expected hybrid/keyword/semantic"}

    scope = str(args.get("scope") or "all")
    limit = int(args.get("limit") or 10)

    ws = Workspace()

    # Try qmd first
    qmd_present = _qmd_available()
    if qmd_present:
        # Ensure indexing (idempotent + cheap)
        _qmd_ensure_collections(ws)
        raw = _qmd_search(query, mode=mode, limit=limit * 2)  # over-fetch for scope filter
        if raw is not None:
            hits = []
            for r in raw:
                hits.append({
                    "path": _qmd_path_to_relative(r.get("file", ""), ws),
                    "title": r.get("title", ""),
                    "snippet": r.get("snippet", ""),
                    "score": r.get("score", 0),
                    "docid": r.get("docid", ""),
                })
            hits = _filter_by_scope(hits, scope)[:limit]
            return {
                "query": query,
                "mode": mode,
                "scope": scope,
                "backend": f"qmd:{mode}",
                "count": len(hits),
                "hits": hits,
            }

    # Fallback: built-in keyword search
    result = _keyword_search(ws, query, scope, limit)
    return {
        "query": query,
        "mode": "keyword",
        "scope": scope,
        "backend": "keyword (fallback — install qmd for semantic search)",
        "qmd_available": qmd_present,
        **result,
    }


# ── prepare_meeting helpers ────────────────────────────────────────────


def _resolve_person_page(ws: Workspace, person_input: str):
    """Resolve a person input (slug, name, partial) to a WikiPage. Returns None if no match."""
    pages = list_pages(ws.wiki_root, "people")
    person_lower = person_input.lower()
    for p in pages:
        if p.slug == person_lower:
            return p
    for p in pages:
        name = (p.frontmatter.get("title") or "").lower()
        if person_lower in name or name in person_lower:
            return p
    return None


def _decisions_mentioning(ws: Workspace, name: str, limit: int = 5) -> list[dict]:
    """Scan wiki/patterns/decisions.md for bullets mentioning a name."""
    out: list[dict] = []
    decisions_md = ws.wiki_root / "patterns" / "decisions.md"
    if not decisions_md.exists():
        return out
    try:
        txt = decisions_md.read_text(encoding="utf-8")
        for line in txt.splitlines():
            if name.lower() in line.lower() and line.strip().startswith("-"):
                out.append({"line": line.strip()})
                if len(out) >= limit:
                    break
    except Exception:
        pass
    return out


def _voice_excerpt(ws: Workspace, name: str) -> str:
    """Pull a voice-profiles.md section mentioning a name. Empty string if none."""
    voice_md = ws.wiki_root / "patterns" / "voice-profiles.md"
    if not voice_md.exists():
        return ""
    try:
        txt = voice_md.read_text(encoding="utf-8")
        for chunk in txt.split("##"):
            if name.lower() in chunk.lower():
                return ("##" + chunk).strip()[:600]
    except Exception:
        pass
    return ""


def _items_for_person(ws: Workspace, name: str) -> dict[str, list[dict]]:
    """Return {they_owe_you, you_owe_them} for a given person name (case-insensitive)."""
    they_owe_you: list[dict] = []
    you_owe_them: list[dict] = []
    if not ws.db_path.exists():
        return {"they_owe_you": they_owe_you, "you_owe_them": you_owe_them}
    try:
        conn = ws.open_db()
        work_items.ensure_schema(conn)
        for r in work_items.query(conn, item_type="action", status="open", owner=name, limit=50):
            they_owe_you.append({
                "id": r["id"],
                "title": r["title"],
                "due_date": r.get("due_date"),
                "wiki_path": r.get("wiki_path"),
            })
        for r in work_items.query(conn, item_type="action", status="open", limit=200):
            requester = (r.get("requester") or "").lower()
            if name.lower() in requester:
                you_owe_them.append({
                    "id": r["id"],
                    "title": r["title"],
                    "due_date": r.get("due_date"),
                    "wiki_path": r.get("wiki_path"),
                })
    except Exception:
        pass
    return {"they_owe_you": they_owe_you, "you_owe_them": you_owe_them}


def _person_brief(ws: Workspace, page, topic: str | None = None) -> dict[str, Any]:
    """Compose the standard person-prep payload from a resolved page."""
    fm = page.frontmatter
    name = fm.get("title", page.slug)
    items = _items_for_person(ws, name)
    return {
        "mode": "person",
        "person": {
            "slug": page.slug,
            "name": name,
            "role": fm.get("role"),
            "relationship": fm.get("relationship"),
            "trust_tier": fm.get("trust_tier"),
            "wiki_path": f"wiki/people/{page.slug}.md",
        },
        "topic": topic or None,
        "open_items": items,
        "recent_decisions": _decisions_mentioning(ws, name),
        "voice_excerpt": _voice_excerpt(ws, name),
        "page_summary": page.body[:800] if page.body else "",
    }


def _project_brief(ws: Workspace, project_input: str, topic: str | None = None) -> dict[str, Any]:
    """Build a project-review prep payload from wiki/projects/{slug}.md + work_items."""
    pages = list_pages(ws.wiki_root, "projects")
    project_lower = project_input.lower()
    matched = None
    for p in pages:
        if p.slug == project_lower:
            matched = p
            break
    if not matched:
        for p in pages:
            title = (p.frontmatter.get("title") or "").lower()
            if project_lower in title or project_lower in p.slug:
                matched = p
                break
    if not matched:
        return {
            "error": f"no project matched '{project_input}'",
            "available_slugs": [p.slug for p in pages],
        }

    # Find work_items mentioning this project in title/stakes (proxy for "related to X")
    # Match on slug, full title, AND each significant token (3+ chars, not common words).
    # E.g. "fy27-plan" matches work_items mentioning "FY27", "plan", or "fy27-plan".
    related_items: list[dict] = []
    if ws.db_path.exists():
        try:
            conn = ws.open_db()
            work_items.ensure_schema(conn)
            slug = matched.slug.lower()
            title = (matched.frontmatter.get("title") or "").lower()
            # Tokens from slug + title, filtering out short / common words
            stop_words = {"the", "and", "for", "with", "into", "from", "this", "that",
                          "annual", "plan", "review", "project"}
            tokens = set()
            for s in (slug, title):
                for tok in s.replace("-", " ").replace("_", " ").split():
                    if len(tok) >= 3 and tok not in stop_words:
                        tokens.add(tok)
            project_terms = [slug, title] + sorted(tokens)
            for r in work_items.query(conn, item_type="action", status="open", limit=300):
                hay = f"{r.get('title','')} {r.get('stakes','')}".lower()
                if any(t and t in hay for t in project_terms):
                    related_items.append({
                        "id": r["id"],
                        "title": r["title"],
                        "owner": r.get("owner"),
                        "due_date": r.get("due_date"),
                    })
        except Exception:
            pass

    return {
        "mode": "project",
        "project": {
            "slug": matched.slug,
            "title": matched.frontmatter.get("title", matched.slug),
            "status": matched.frontmatter.get("status"),
            "wiki_path": f"wiki/projects/{matched.slug}.md",
        },
        "topic": topic or None,
        "page_summary": matched.body[:1500] if matched.body else "",
        "related_open_items": related_items[:20],
        "open_item_count": len(related_items),
    }


def _cadence_brief(ws: Workspace, cadence_key: str, topic: str | None = None) -> dict[str, Any]:
    """Build a cadence-based prep using cadence_registry + meeting_series + program_meetings.

    Gracefully handles the empty-ingestion case: if no meeting data exists, returns the
    cadence definition + relevant work_items + a note that meeting ingestion isn't configured.
    """
    if not ws.db_path.exists():
        return {"error": "no workbench.db — run `sb scan` first"}
    try:
        conn = ws.open_db()
    except Exception as e:
        return {"error": f"db open failed: {e}"}

    # Verify the cadence exists
    row = conn.execute(
        "SELECT cadence_key, timezone, boundary_rule, interval_weeks, description, enabled "
        "FROM cadence_registry WHERE cadence_key = ? LIMIT 1",
        (cadence_key,),
    ).fetchone()
    if not row:
        all_cadences = [r["cadence_key"] for r in conn.execute(
            "SELECT cadence_key FROM cadence_registry ORDER BY cadence_key"
        ).fetchall()]
        return {
            "error": f"unknown cadence '{cadence_key}'",
            "available_cadences": all_cadences,
        }
    cadence_info = dict(row)

    # Find any meeting series bound to this cadence
    bound_series: list[dict] = []
    try:
        series_rows = conn.execute(
            """
            SELECT ms.id, ms.canonical_title, ms.cadence_kind, ms.status,
                   cmb.status as binding_status, cmb.effective_from, cmb.effective_to
            FROM cadence_meeting_bindings cmb
            JOIN meeting_series ms ON ms.id = cmb.meeting_series_id
            WHERE cmb.cadence_key = ?
            """,
            (cadence_key,),
        ).fetchall()
        bound_series = [dict(r) for r in series_rows]
    except Exception:
        pass

    # Find recent program_meetings tagged with this cadence (via series binding)
    recent_occurrences: list[dict] = []
    if bound_series:
        series_ids = [str(s["id"]) for s in bound_series]
        try:
            placeholders = ",".join("?" * len(series_ids))
            occ_rows = conn.execute(
                f"""
                SELECT pm.id, pm.title, pm.meeting_date, pm.starts_at, pm.status, pm.summary
                FROM program_meetings pm
                JOIN meeting_source_refs msr ON msr.program_meeting_id = pm.id
                WHERE msr.meeting_series_id IN ({placeholders})
                ORDER BY pm.starts_at DESC
                LIMIT 5
                """,
                series_ids,
            ).fetchall()
            recent_occurrences = [dict(r) for r in occ_rows]
        except Exception:
            pass

    # Pull recent open work_items — broad cast, the LLM can filter
    recent_items: list[dict] = []
    try:
        work_items.ensure_schema(conn)
        for r in work_items.query(conn, item_type="action", status="open", limit=20):
            recent_items.append({
                "id": r["id"],
                "title": r["title"],
                "owner": r.get("owner"),
                "due_date": r.get("due_date"),
            })
    except Exception:
        pass

    # Honest note if ingestion is empty
    note = None
    if not bound_series and not recent_occurrences:
        note = (
            "No meeting series or occurrences are bound to this cadence yet — "
            "meeting ingestion (Outlook/Zoom) has not run against this project home. "
            "The cadence definition is still useful for context (timezone, interval), "
            "and recent open work_items are included for general prep."
        )

    return {
        "mode": "cadence",
        "cadence": cadence_info,
        "topic": topic or None,
        "bound_series": bound_series,
        "recent_occurrences": recent_occurrences,
        "recent_open_items": recent_items,
        "note": note,
    }


def _handle_prepare_meeting(args: dict) -> Any:
    """Dispatcher: routes to person/project/cadence mode based on which arg is set."""
    person = (args.get("person") or "").strip()
    project = (args.get("project") or "").strip()
    cadence = (args.get("cadence") or "").strip()
    topic = (args.get("topic") or "").strip() or None

    # Validate: exactly one of person/project/cadence must be set
    modes_set = sum(1 for v in (person, project, cadence) if v)
    if modes_set == 0:
        return {
            "error": "provide one of: person, project, or cadence",
            "examples": [
                "prepare_meeting(person='suneet-nandwani')",
                "prepare_meeting(project='fy27-annual-planning')",
                "prepare_meeting(cadence='weekly_team')",
            ],
        }
    if modes_set > 1:
        return {"error": "provide only ONE of person/project/cadence"}

    ws = Workspace()

    if person:
        page = _resolve_person_page(ws, person)
        if not page:
            return {
                "error": f"no person matched '{person}'",
                "available_slugs": [p.slug for p in list_pages(ws.wiki_root, "people")],
            }
        return _person_brief(ws, page, topic)

    if project:
        return _project_brief(ws, project, topic)

    if cadence:
        return _cadence_brief(ws, cadence, topic)


def _handle_upsert_promise(args: dict) -> Any:
    ws = Workspace()
    audience = str(args.get("audience_type") or "unknown")
    if audience not in ("executive", "peer", "team", "external", "self", "unknown"):
        return {"error": f"invalid audience_type '{audience}'"}
    return work_items.upsert_promise(
        ws.open_db(),
        title=str(args["title"]),
        made_to=str(args["made_to"]),
        audience_type=audience,
        topic=str(args.get("topic") or ""),
        due_date=str(args.get("due_date") or ""),
        stakes=str(args.get("stakes") or ""),
        source=str(args.get("source") or ""),
        wiki_path=str(args.get("wiki_path") or ""),
        status=str(args.get("status") or "open"),
    )


def _handle_check_promises(args: dict) -> Any:
    ws = Workspace()
    if not ws.db_path.exists():
        return {"mode": args.get("mode", "filter"), "count": 0, "promises": []}

    mode = str(args.get("mode") or "filter").lower()
    conn = ws.open_db()

    if mode == "inconsistencies":
        topic = str(args.get("topic") or "") or None
        conflicts = work_items.find_promise_inconsistencies(conn, topic=topic)
        return {
            "mode": "inconsistencies",
            "count": len(conflicts),
            "conflicts": conflicts,
        }

    # mode == filter (default)
    status_arg = str(args.get("status") or "open")
    status = None if status_arg == "all" else status_arg
    rows = work_items.query_promises(
        conn,
        made_to=str(args.get("made_to") or "") or None,
        topic=str(args.get("topic") or "") or None,
        audience_type=str(args.get("audience_type") or "") or None,
        status=status,
        limit=int(args.get("limit") or 50),
    )
    return {
        "mode": "filter",
        "filters": {
            "made_to": args.get("made_to"),
            "topic": args.get("topic"),
            "audience_type": args.get("audience_type"),
            "status": status_arg,
        },
        "count": len(rows),
        "promises": rows,
    }


def _handle_query_aging_commitments(args: dict) -> Any:
    """Reuse the daemon job's payload builder so CLI/daemon/MCP agree on logic."""
    from secondbrain.daemon.jobs.aging_commitments import _build_aging_payload, TIER_THRESHOLDS

    min_days = args.get("min_days_aged")
    if min_days is not None:
        try:
            min_days = int(min_days)
        except (ValueError, TypeError):
            return {"error": "min_days_aged must be an integer"}

    ws = Workspace()
    payload = _build_aging_payload(ws)

    # Optional override: filter fired list by min_days
    fired = payload["fired"]
    if min_days is not None:
        fired = [item for item in fired if item["days_aged"] >= min_days]

    return {
        "owner_self": payload.get("owner_self"),
        "thresholds": dict(TIER_THRESHOLDS),
        "by_tier": payload["by_tier"],
        "fired_count": len(fired),
        "fired": fired,
    }


def _handle_list_cadences(_args: dict) -> Any:
    """Return all rows from cadence_registry."""
    ws = Workspace()
    if not ws.db_path.exists():
        return {"cadences": [], "error": "no workbench.db"}
    try:
        conn = ws.open_db()
        rows = conn.execute(
            "SELECT cadence_key, timezone, boundary_rule, interval_weeks, enabled, description "
            "FROM cadence_registry ORDER BY cadence_key"
        ).fetchall()
        return {"count": len(rows), "cadences": [dict(r) for r in rows]}
    except Exception as e:
        return {"cadences": [], "error": f"cadence_registry query failed: {e}"}


def _handle_generate_person_context(args: dict) -> Any:
    """Rich person briefing — superset of get_person.

    Returns: wiki page + open items (both directions) + decisions mentioning them +
    voice excerpt + recent messages_canonical mentions + projects where they appear.
    """
    person_input = str(args["person"]).strip()
    include_messages = args.get("include_messages")
    if include_messages is None:
        include_messages = True

    ws = Workspace()
    page = _resolve_person_page(ws, person_input)
    if not page:
        return {
            "error": f"no person matched '{person_input}'",
            "available_slugs": [p.slug for p in list_pages(ws.wiki_root, "people")],
        }

    fm = page.frontmatter
    name = fm.get("title", page.slug)

    # Open items in both directions
    items = _items_for_person(ws, name)

    # Decisions mentioning them
    decisions = _decisions_mentioning(ws, name, limit=10)

    # Voice excerpt
    voice = _voice_excerpt(ws, name)

    # Projects where they appear (scan wiki/projects/ pages for name mentions)
    related_projects: list[dict] = []
    for project_page in list_pages(ws.wiki_root, "projects"):
        body = project_page.body or ""
        fm_p = project_page.frontmatter
        if name.lower() in body.lower() or name.lower() in (fm_p.get("title") or "").lower():
            related_projects.append({
                "slug": project_page.slug,
                "title": fm_p.get("title", project_page.slug),
                "status": fm_p.get("status"),
                "wiki_path": f"wiki/projects/{project_page.slug}.md",
            })

    # Recent canonical message mentions (optional — gracefully handle empty)
    recent_messages: list[dict] = []
    messages_status = "not_queried"
    if include_messages and ws.db_path.exists():
        try:
            conn = ws.open_db()
            # Check if the table even exists + has rows
            count_row = conn.execute(
                "SELECT COUNT(*) as n FROM messages_canonical"
            ).fetchone()
            total_msgs = count_row["n"] if count_row else 0
            if total_msgs > 0:
                # Search the body field (case-insensitive)
                msg_rows = conn.execute(
                    "SELECT id, ts, conversation_id, user_id, substr(body, 1, 200) as snippet "
                    "FROM messages_canonical "
                    "WHERE LOWER(body) LIKE ? OR user_id = ? "
                    "ORDER BY ts DESC LIMIT 10",
                    (f"%{name.lower()}%", fm.get("slack_user_id") or ""),
                ).fetchall()
                recent_messages = [dict(r) for r in msg_rows]
                messages_status = f"queried (corpus has {total_msgs} messages)"
            else:
                messages_status = "corpus empty (no Slack ingestion yet)"
        except Exception as e:
            messages_status = f"error: {type(e).__name__}"

    return {
        "person": {
            "slug": page.slug,
            "name": name,
            "role": fm.get("role"),
            "relationship": fm.get("relationship"),
            "trust_tier": fm.get("trust_tier"),
            "slack_user_id": fm.get("slack_user_id"),
            "team": fm.get("team"),
            "wiki_path": f"wiki/people/{page.slug}.md",
        },
        "open_items": items,
        "decisions_mentioning": decisions,
        "voice_excerpt": voice,
        "related_projects": related_projects,
        "recent_messages": recent_messages,
        "messages_status": messages_status,
        "page_summary": page.body[:1500] if page.body else "",
    }


HANDLERS = {
    "query_work_items": _handle_query_work_items,
    "upsert_work_item": _handle_upsert_work_item,
    "list_people": _handle_list_people,
    "get_person": _handle_get_person,
    "read_wiki_page": _handle_read_wiki_page,
    "workspace_info": _handle_workspace_info,
    "get_workspace_status": _handle_get_workspace_status,
    "get_groups_and_people": _handle_get_groups_and_people,
    "evidence_search": _handle_evidence_search,
    "prepare_meeting": _handle_prepare_meeting,
    "list_cadences": _handle_list_cadences,
    "generate_person_context": _handle_generate_person_context,
    "query_aging_commitments": _handle_query_aging_commitments,
    "upsert_promise": _handle_upsert_promise,
    "check_promises": _handle_check_promises,
}


# ── Server wiring ─────────────────────────────────────────────────────────

server = Server("secondbrain", version=__version__)


@server.list_tools()
async def _list_tools() -> list[Tool]:
    return TOOL_DEFS


@server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    handler = HANDLERS.get(name)
    if handler is None:
        return _result({"error": f"unknown tool: {name}"})
    try:
        return _result(handler(arguments))
    except WorkspaceError as e:
        return _result({"error": str(e)})
    except Exception as e:  # noqa: BLE001
        return _result({"error": f"{type(e).__name__}: {e}"})


async def _async_main() -> None:
    """Run the MCP server over stdio."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    """Entry point for `python -m secondbrain.mcp.server` and `sb mcp`."""
    import asyncio
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
