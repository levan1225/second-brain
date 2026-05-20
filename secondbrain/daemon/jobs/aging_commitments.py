"""Aging-commitment escalator — daily 4:45pm fire.

Surfaces work_items where:
  - status = open
  - the user (owner_self) is the requester (i.e. someone else owes them)
  - days since first_seen_at > tier threshold
  - the tier hasn't been fired before (avoid spam)

Tiers:
  gentle    (3+ days)  — friendly nudge prompt
  draft     (5+ days)  — system would draft a chaser
  escalate  (8+ days)  — escalate-or-drop decision

Writes the brief to output/daemon/briefings/{date}-aging.md. The user reads it
and either: asks Claude to send a chase (via Claude Desktop's Slack connector),
runs `sb chase <person>` from the CLI, marks the work_item done, or dismisses
by leaving it alone (the tier flag prevents re-firing tomorrow at the same tier).

Delivery: file only. Same philosophy as morning_brief — secondbrain holds
the data, Claude does the action.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from secondbrain.core import work_items
from secondbrain.core.workspace import Workspace
from secondbrain.daemon.registry import Job, register_job


# Tier thresholds — days aged since the work_item was first seen
TIER_THRESHOLDS = {
    "gentle": 3,
    "draft": 5,
    "escalate": 8,
}
# Ordered highest-urgency-first so we pick the strongest tier each item qualifies for
TIER_ORDER = ["escalate", "draft", "gentle"]


def _identify_owner(ws: Workspace) -> str | None:
    """Read identity.md to find out who the workspace owner is.

    Returns the name string used as `owner` on work_items the user owns. We use
    this to filter OUT items the user owns themselves (we only want items
    OTHERS owe them).
    """
    identity_md = ws.project_home / "identity.md"
    if not identity_md.exists():
        return None
    try:
        import yaml
        raw = identity_md.read_text(encoding="utf-8")
        if raw.startswith("---\n"):
            end = raw.find("\n---\n", 4)
            if end > 0:
                fm = yaml.safe_load(raw[4:end]) or {}
                return fm.get("owner") or None
    except Exception:
        pass
    return None


def _tier_for_age(days: int, already_fired: str) -> str | None:
    """Return the highest tier this item qualifies for that hasn't been fired yet.

    Returns None if the item is too young OR if its current tier has already
    been fired (so we don't spam the same urgency repeatedly).
    """
    for tier in TIER_ORDER:
        if days >= TIER_THRESHOLDS[tier]:
            # Once an item has been escalated, gentle/draft re-fires are pointless
            already_fired_rank = TIER_ORDER.index(already_fired) if already_fired in TIER_ORDER else len(TIER_ORDER)
            this_rank = TIER_ORDER.index(tier)
            if this_rank < already_fired_rank:
                return tier
            return None
    return None


def _build_aging_payload(ws: Workspace) -> dict[str, Any]:
    """Query work_items and bucket into tiers."""
    if not ws.db_path.exists():
        return {"by_tier": {"escalate": [], "draft": [], "gentle": []}, "fired": []}

    owner_self = _identify_owner(ws)
    now = datetime.now(timezone.utc)

    by_tier: dict[str, list[dict]] = {"escalate": [], "draft": [], "gentle": []}
    fired: list[dict] = []

    try:
        conn = ws.open_db()
        work_items.ensure_schema(conn)
        # Pull all open actions — we'll filter in Python so we can read both
        # owner and requester easily
        rows = work_items.query(conn, item_type="action", status="open", limit=500)
    except Exception:
        return {"by_tier": by_tier, "fired": []}

    for r in rows:
        owner = (r.get("owner") or "").strip()
        requester = (r.get("requester") or "").strip()

        # Filter rules for "someone else owes the user":
        #   1. owner must be set
        #   2. owner must NOT be the user themselves
        #   3. If requester is set and is NOT the user, skip (it's not their chase)
        #   4. If requester is empty, include but mark as "unconfirmed requester"
        if not owner:
            continue
        if owner_self and owner.lower() == owner_self.lower():
            continue
        requester_unknown = not requester
        if owner_self and requester and owner_self.lower() not in requester.lower():
            continue

        # Compute age
        first_seen = r.get("first_seen_at") or ""
        if not first_seen:
            continue
        try:
            # Be tolerant of trailing Z
            ts = first_seen.replace("Z", "+00:00")
            seen_dt = datetime.fromisoformat(ts)
            if seen_dt.tzinfo is None:
                seen_dt = seen_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        days_aged = (now - seen_dt).days

        last_tier = r.get("chase_tier_last") or ""
        tier = _tier_for_age(days_aged, last_tier)
        if tier is None:
            continue

        item = {
            "id": r["id"],
            "title": r["title"],
            "owner": owner,
            "days_aged": days_aged,
            "due_date": r.get("due_date") or "",
            "stakes": r.get("stakes") or "",
            "tier": tier,
            "last_chase_at": r.get("last_chase_at") or "",
            "requester_unknown": requester_unknown,
        }
        by_tier[tier].append(item)
        fired.append(item)

    # Sort each bucket by days_aged descending
    for t in by_tier:
        by_tier[t].sort(key=lambda x: x["days_aged"], reverse=True)

    return {"by_tier": by_tier, "fired": fired, "owner_self": owner_self}


def _render_markdown(payload: dict[str, Any]) -> str:
    as_of = datetime.now().date().isoformat()
    lines = [
        "---",
        f"title: Aging commitments — {as_of}",
        f"as_of: {as_of}",
        "kind: aging-escalator",
        "---",
        "",
        f"# Aging commitments — {as_of}",
        "",
        f"Items where someone owes _you_ something and the clock has been ticking. "
        f"Tiered by how long ago the commitment was made.",
        "",
    ]

    total = sum(len(v) for v in payload["by_tier"].values())
    if total == 0:
        lines.append("_Nothing to chase. Either nobody owes you anything, "
                     "or you're staying current. Either way: enjoy._")
        return "\n".join(lines)

    suggestions = {
        "escalate": "Decide: escalate to their manager, drop it, or do one final nudge.",
        "draft": "Time for a chaser. Ask Claude to draft one in your voice.",
        "gentle": "Friendly nudge in the next 1:1 or a quick Slack message.",
    }
    tier_labels = {
        "escalate": "🚨 Escalate or drop (8+ days)",
        "draft": "✉️ Draft a chaser (5+ days)",
        "gentle": "💬 Gentle nudge (3+ days)",
    }

    for tier in TIER_ORDER:  # escalate / draft / gentle (most urgent first)
        items = payload["by_tier"][tier]
        if not items:
            continue
        lines.append(f"## {tier_labels[tier]}")
        lines.append("")
        lines.append(f"_{suggestions[tier]}_")
        lines.append("")
        for item in items:
            lines.append(f"- **{item['title']}**")
            lines.append(f"  - owner: {item['owner']}")
            lines.append(f"  - days aged: {item['days_aged']}")
            if item["due_date"]:
                lines.append(f"  - due: {item['due_date']}")
            if item["stakes"]:
                lines.append(f"  - stakes: {item['stakes']}")
            if item.get("requester_unknown"):
                lines.append(f"  - ⚠️ _requester unknown — confirm this is owed to you before chasing_")
            lines.append(f"  - `work_item_id: {item['id']}`")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("To act on any of these: ask Claude — *\"send the chase for work_item 16\"* — "
                 "and Claude will draft + post via its Slack connector. "
                 "Or run `sb chase <person>` from the CLI.")

    return "\n".join(lines)


def run(ws: Workspace) -> dict[str, Any]:
    """Job entry point. Writes the aging brief and stamps each fired item."""
    payload = _build_aging_payload(ws)
    md = _render_markdown(payload)

    today = datetime.now().date().isoformat()
    out_dir = ws.project_home / "output" / "daemon" / "briefings"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{today}-aging.md"
    out_file.write_text(md, encoding="utf-8")

    # Stamp each fired item so we don't re-fire the same tier tomorrow.
    # We only stamp if the brief HAS content (don't lock items out on no-op runs).
    if payload["fired"] and ws.db_path.exists():
        try:
            conn = ws.open_db()
            for item in payload["fired"]:
                work_items.mark_chased(conn, item["id"], tier=item["tier"])
        except Exception:
            pass

    return {
        "path": str(out_file.relative_to(ws.project_home)),
        "fired_count": len(payload["fired"]),
        "by_tier": {t: len(v) for t, v in payload["by_tier"].items()},
        "owner_self": payload.get("owner_self"),
    }


register_job(Job(
    id="aging_commitments",
    description="Daily aging-commitment escalator — surfaces items others owe you that are getting stale",
    schedule={"cron": "45 16 * * mon-fri"},  # 4:45pm weekdays local
    run=run,
    enabled_default=True,
))
