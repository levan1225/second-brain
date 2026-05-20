"""Canonical work_items reads + idempotent upserts.

Port of the v0.11.0 canonical_sync logic into the v3 package. Same dedup
keys, same Latest-Wins, same column shape (wiki_path, content_hash, history,
sources, due_date, stakes).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def content_hash(content: str) -> str:
    return hashlib.sha256(_normalize(content).encode("utf-8")).hexdigest()[:16]


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, sql: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create work_items table + v3 columns. Idempotent.

    v3.1 additions (aging escalator):
      last_chase_at      TEXT — ISO timestamp the daemon last flagged this for chasing
      chase_tier_last    TEXT — which tier triggered last fire ('gentle'/'draft'/'escalate')
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS work_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type TEXT NOT NULL,
            item_key TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            owner TEXT DEFAULT '',
            requester TEXT DEFAULT '',
            status TEXT DEFAULT 'open',
            priority TEXT DEFAULT 'medium',
            source TEXT DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_updated_at TEXT NOT NULL,
            updated_by TEXT DEFAULT 'system'
        )
        """
    )
    # v0.11.0 / v3 columns (idempotent)
    _ensure_column(conn, "work_items", "wiki_path", "TEXT DEFAULT ''")
    _ensure_column(conn, "work_items", "content_hash", "TEXT DEFAULT ''")
    _ensure_column(conn, "work_items", "history", "TEXT DEFAULT '[]'")
    _ensure_column(conn, "work_items", "sources", "TEXT DEFAULT '[]'")
    _ensure_column(conn, "work_items", "due_date", "TEXT DEFAULT ''")
    _ensure_column(conn, "work_items", "stakes", "TEXT DEFAULT ''")
    # v3.1: aging escalator tracking
    _ensure_column(conn, "work_items", "last_chase_at", "TEXT DEFAULT ''")
    _ensure_column(conn, "work_items", "chase_tier_last", "TEXT DEFAULT ''")
    # v3.1: Promise Ledger — same table, kind='promise' discriminator
    #   kind          — 'action' (default; commitments + deliverables) or 'promise' (verbal "I told X that Y")
    #   made_to       — who you said it to (e.g. 'Aleksandar Yordanov', 'CEO Staff', 'CFO')
    #   audience_type — coarse audience class ('executive'|'peer'|'team'|'external'|'self'|'unknown')
    #   topic         — free-form topic / initiative slug, used for inconsistency detection
    _ensure_column(conn, "work_items", "kind", "TEXT DEFAULT 'action'")
    _ensure_column(conn, "work_items", "made_to", "TEXT DEFAULT ''")
    _ensure_column(conn, "work_items", "audience_type", "TEXT DEFAULT ''")
    _ensure_column(conn, "work_items", "topic", "TEXT DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wi_kind ON work_items(kind)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wi_made_to ON work_items(made_to)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wi_topic ON work_items(topic)")

    for idx_sql in (
        "CREATE INDEX IF NOT EXISTS idx_wi_item_type ON work_items(item_type)",
        "CREATE INDEX IF NOT EXISTS idx_wi_status ON work_items(status)",
        "CREATE INDEX IF NOT EXISTS idx_wi_owner ON work_items(owner)",
        "CREATE INDEX IF NOT EXISTS idx_wi_content_hash ON work_items(content_hash)",
        "CREATE INDEX IF NOT EXISTS idx_wi_wiki_path ON work_items(wiki_path)",
        "CREATE INDEX IF NOT EXISTS idx_wi_due_date ON work_items(due_date)",
    ):
        conn.execute(idx_sql)
    conn.commit()


def upsert(
    conn: sqlite3.Connection,
    *,
    item_type: str,
    title: str,
    owner: str = "",
    requester: str = "",
    status: str = "open",
    priority: str = "medium",
    due_date: str = "",
    stakes: str = "",
    source: str = "",
    wiki_path: str = "",
) -> dict[str, Any]:
    """Idempotent upsert by (item_type, source, content_hash). Latest-Wins on content."""
    ensure_schema(conn)
    now = _now_iso()
    ch = content_hash(title)
    item_key = f"{item_type}:{ch}:{source}"

    existing = None
    if source:
        existing = conn.execute(
            "SELECT * FROM work_items WHERE item_type=? AND source=? AND content_hash=? LIMIT 1",
            (item_type, source, ch),
        ).fetchone()
    if not existing:
        existing = conn.execute(
            "SELECT * FROM work_items WHERE item_type=? AND content_hash=? LIMIT 1",
            (item_type, ch),
        ).fetchone()

    if existing:
        ex = dict(existing)
        old_title = ex.get("title", "")
        history = json.loads(ex.get("history") or "[]")
        if old_title and old_title != title:
            history.append({
                "title": old_title,
                "due_date": ex.get("due_date") or "",
                "stakes": ex.get("stakes") or "",
                "updated_at": ex.get("last_updated_at"),
            })
        sources = json.loads(ex.get("sources") or "[]")
        if source and source not in sources:
            sources.append(source)

        conn.execute(
            """
            UPDATE work_items SET
                title=?, owner=COALESCE(NULLIF(?,''), owner),
                status=?, priority=?,
                wiki_path=COALESCE(NULLIF(?,''), wiki_path),
                due_date=COALESCE(NULLIF(?,''), due_date),
                stakes=COALESCE(NULLIF(?,''), stakes),
                history=?, sources=?,
                last_updated_at=?, updated_by='canonical_sync'
            WHERE id=?
            """,
            (
                title, owner, status, priority, wiki_path, due_date, stakes,
                json.dumps(history), json.dumps(sources), now, ex["id"],
            ),
        )
        conn.commit()
        return {
            "work_item_id": ex["id"],
            "created": False,
            "history_appended": bool(old_title and old_title != title),
            "content_hash": ch,
        }

    conn.execute(
        """
        INSERT INTO work_items (
            item_type, item_key, title, owner, requester, status, priority,
            source, wiki_path, content_hash, history, sources, due_date, stakes,
            first_seen_at, last_updated_at, updated_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, 'canonical_sync')
        """,
        (
            item_type, item_key, title, owner, requester, status, priority,
            source, wiki_path, ch, json.dumps([source] if source else []),
            due_date, stakes, now, now,
        ),
    )
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    return {
        "work_item_id": new_id,
        "created": True,
        "history_appended": False,
        "content_hash": ch,
    }


def query(
    conn: sqlite3.Connection,
    *,
    item_type: str | None = None,
    status: str | None = None,
    owner: str | None = None,
    overdue: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Query work_items with optional filters."""
    ensure_schema(conn)
    clauses: list[str] = []
    params: list[Any] = []
    if item_type:
        clauses.append("item_type = ?")
        params.append(item_type)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if owner:
        clauses.append("owner LIKE ?")
        params.append(f"%{owner}%")
    if overdue:
        # Use LOCAL date — what the user thinks "today" means.
        # UTC would prematurely classify today's items as overdue after ~5pm Pacific.
        today = datetime.now().date().isoformat()
        clauses.append("due_date != '' AND due_date < ?")
        clauses.append("status NOT IN ('done','closed','cancelled')")
        params.append(today)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        f"SELECT * FROM work_items {where} "
        f"ORDER BY CASE WHEN due_date='' OR due_date IS NULL THEN 1 ELSE 0 END, "
        f"         due_date ASC, last_updated_at DESC LIMIT ?"
    )
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def by_id(conn: sqlite3.Connection, wid: int) -> dict[str, Any] | None:
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM work_items WHERE id=?", (wid,)).fetchone()
    return dict(row) if row else None


def upsert_promise(
    conn: sqlite3.Connection,
    *,
    title: str,
    made_to: str,
    audience_type: str = "unknown",
    topic: str = "",
    due_date: str = "",
    stakes: str = "",
    source: str = "",
    wiki_path: str = "",
    status: str = "open",
) -> dict[str, Any]:
    """Upsert a promise (kind='promise') into work_items.

    Dedup key: (kind='promise', made_to, content_hash). Restating the same
    promise to the same audience updates the existing row; the same content
    to a different audience creates a separate row. That's deliberate — the
    whole point is multi-audience tracking.

    audience_type values: 'executive' | 'peer' | 'team' | 'external' | 'self' | 'unknown'
    """
    ensure_schema(conn)
    now = _now_iso()
    ch = content_hash(title)
    item_key = f"promise:{ch}:{made_to.lower()}"

    existing = conn.execute(
        "SELECT * FROM work_items WHERE kind='promise' AND made_to=? AND content_hash=? LIMIT 1",
        (made_to, ch),
    ).fetchone()

    if existing:
        ex = dict(existing)
        history = json.loads(ex.get("history") or "[]")
        old_title = ex.get("title", "")
        if old_title and old_title != title:
            history.append({
                "title": old_title,
                "due_date": ex.get("due_date") or "",
                "stakes": ex.get("stakes") or "",
                "updated_at": ex.get("last_updated_at"),
            })
        conn.execute(
            """
            UPDATE work_items SET
                title=?, status=?,
                wiki_path=COALESCE(NULLIF(?,''), wiki_path),
                due_date=COALESCE(NULLIF(?,''), due_date),
                stakes=COALESCE(NULLIF(?,''), stakes),
                audience_type=COALESCE(NULLIF(?,''), audience_type),
                topic=COALESCE(NULLIF(?,''), topic),
                history=?, last_updated_at=?, updated_by='promise_upsert'
            WHERE id=?
            """,
            (title, status, wiki_path, due_date, stakes, audience_type, topic,
             json.dumps(history), now, ex["id"]),
        )
        conn.commit()
        return {
            "promise_id": ex["id"],
            "created": False,
            "history_appended": bool(old_title and old_title != title),
        }

    conn.execute(
        """
        INSERT INTO work_items (
            item_type, item_key, kind, title, status,
            made_to, audience_type, topic,
            source, wiki_path, content_hash, history, sources,
            due_date, stakes,
            first_seen_at, last_updated_at, updated_by
        ) VALUES (?, ?, 'promise', ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, 'promise_upsert')
        """,
        (
            "action", item_key, title, status,
            made_to, audience_type, topic,
            source, wiki_path, ch, json.dumps([source] if source else []),
            due_date, stakes,
            now, now,
        ),
    )
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    return {"promise_id": new_id, "created": True, "history_appended": False}


def query_promises(
    conn: sqlite3.Connection,
    *,
    made_to: str | None = None,
    topic: str | None = None,
    audience_type: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Query promises (kind='promise')."""
    ensure_schema(conn)
    clauses: list[str] = ["kind = 'promise'"]
    params: list[Any] = []
    if made_to:
        clauses.append("made_to LIKE ?")
        params.append(f"%{made_to}%")
    if topic:
        clauses.append("topic LIKE ?")
        params.append(f"%{topic}%")
    if audience_type:
        clauses.append("audience_type = ?")
        params.append(audience_type)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = " WHERE " + " AND ".join(clauses)
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM work_items{where} ORDER BY last_updated_at DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def find_promise_inconsistencies(
    conn: sqlite3.Connection,
    *,
    topic: str | None = None,
) -> list[dict[str, Any]]:
    """Detect promises on the same topic told to different audiences with
    different content.

    Returns a list of conflict groups: { topic, promises: [...] } where each
    group has ≥2 promises sharing the topic but differing in title or due_date.
    """
    ensure_schema(conn)
    all_promises = query_promises(conn, topic=topic, status="open", limit=500)
    by_topic: dict[str, list[dict]] = {}
    for p in all_promises:
        t = (p.get("topic") or "").strip().lower()
        if not t:
            continue
        by_topic.setdefault(t, []).append(p)

    conflicts: list[dict] = []
    for t, items in by_topic.items():
        if len(items) < 2:
            continue
        # Group by content_hash — if all match, no real conflict (just restated)
        hashes = {p.get("content_hash") for p in items}
        due_dates = {p.get("due_date") or "" for p in items}
        if len(hashes) > 1 or len(due_dates) > 1:
            conflicts.append({
                "topic": t,
                "promise_count": len(items),
                "distinct_titles": len(hashes),
                "distinct_due_dates": len(due_dates),
                "promises": [
                    {
                        "id": p["id"],
                        "made_to": p.get("made_to"),
                        "audience_type": p.get("audience_type"),
                        "title": p.get("title"),
                        "due_date": p.get("due_date") or "",
                        "stakes": p.get("stakes") or "",
                        "last_updated_at": p.get("last_updated_at"),
                    }
                    for p in items
                ],
            })
    return conflicts


def mark_chased(
    conn: sqlite3.Connection,
    work_item_id: int,
    *,
    tier: str,
) -> None:
    """Stamp a work_item as chased at this tier. Used by the aging escalator
    so it doesn't re-fire the same tier on the same item the next day.

    Tiers (lowest to highest urgency):
      'gentle'    — 3+ days aged, low-friction nudge
      'draft'     — 5+ days, system drafted a chaser
      'escalate'  — 8+ days, time to escalate or drop
    """
    ensure_schema(conn)
    conn.execute(
        "UPDATE work_items SET last_chase_at=?, chase_tier_last=?, last_updated_at=? WHERE id=?",
        (_now_iso(), tier, _now_iso(), work_item_id),
    )
    conn.commit()
