"""CLI for direct work_item manipulation: add, done, rm, show.

For when you don't want to open a Cowork session just to mark something done.
"""

from __future__ import annotations

import click
from rich.console import Console
from rich.panel import Panel

from secondbrain.core import work_items
from secondbrain.core.workspace import Workspace, WorkspaceError

console = Console()


def _ws_or_die(project_home: str | None) -> Workspace:
    try:
        return Workspace(project_home)
    except WorkspaceError as e:
        console.print(f"[red]✗[/red] {e}")
        raise click.Abort()


# ── sb add ──────────────────────────────────────────────────────────────


@click.command(help="Add a work item (commitment, risk, issue, decision) directly.")
@click.argument("title")
@click.option("--type", "item_type", type=click.Choice(["action", "risk", "issue", "decision"]),
              default="action", help="Item type. Default: action.")
@click.option("--owner", default="", help="Who owes this (a person's name).")
@click.option("--requester", default="", help="Who's expecting this (you, by default — set with `sb identity set --owner`).")
@click.option("--due", "due_date", default="", help="Due date (ISO: YYYY-MM-DD).")
@click.option("--stakes", default="", help="Why this matters / consequence of slipping.")
@click.option("--source", default="manual://cli", help="Provenance URI.")
@click.option(
    "--project-home",
    type=click.Path(file_okay=False),
    help="Override project home (default: from config or env)",
)
def add(
    title: str,
    item_type: str,
    owner: str,
    requester: str,
    due_date: str,
    stakes: str,
    source: str,
    project_home: str | None,
) -> None:
    """Insert a work item from the command line."""
    ws = _ws_or_die(project_home)
    conn = ws.open_db()

    # Default requester to the identity owner if not provided
    if not requester:
        try:
            import yaml
            ident_path = ws.project_home / "identity.md"
            if ident_path.exists():
                raw = ident_path.read_text(encoding="utf-8")
                if raw.startswith("---\n"):
                    end = raw.find("\n---\n", 4)
                    if end > 0:
                        fm = yaml.safe_load(raw[4:end]) or {}
                        requester = fm.get("owner", "") or ""
        except Exception:
            pass

    result = work_items.upsert(
        conn,
        item_type=item_type,
        title=title,
        owner=owner,
        requester=requester,
        due_date=due_date,
        stakes=stakes,
        source=source,
    )

    if result["created"]:
        console.print(f"[green]✓[/green] added [bold]work_item #{result['work_item_id']}[/bold]")
    else:
        console.print(f"[yellow]≈[/yellow] updated existing [bold]work_item #{result['work_item_id']}[/bold] (Latest-Wins)")

    # Show the row back
    row = work_items.by_id(conn, result["work_item_id"])
    if row:
        details = [
            f"[bold]{row['title']}[/bold]",
            f"  type:      {row['item_type']}",
            f"  status:    {row['status']}",
            f"  owner:     {row.get('owner') or '[dim]—[/dim]'}",
            f"  requester: {row.get('requester') or '[dim]—[/dim]'}",
            f"  due:       {row.get('due_date') or '[dim]—[/dim]'}",
            f"  stakes:    {row.get('stakes') or '[dim]—[/dim]'}",
        ]
        console.print(Panel("\n".join(details), border_style="green", expand=False))


# ── sb done ─────────────────────────────────────────────────────────────


@click.command(help="Mark a work item as done.")
@click.argument("work_item_id", type=int)
@click.option(
    "--project-home",
    type=click.Path(file_okay=False),
    help="Override project home (default: from config or env)",
)
def done(work_item_id: int, project_home: str | None) -> None:
    ws = _ws_or_die(project_home)
    conn = ws.open_db()
    work_items.ensure_schema(conn)

    row = work_items.by_id(conn, work_item_id)
    if row is None:
        console.print(f"[red]✗[/red] no work_item with id {work_item_id}")
        raise click.Abort()
    if row.get("status") == "done":
        console.print(f"[dim]≈ work_item #{work_item_id} is already done.[/dim]")
        return

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE work_items SET status='done', last_updated_at=?, updated_by='cli' WHERE id=?",
        (now, work_item_id),
    )
    conn.commit()
    console.print(f"[green]✓[/green] marked [bold]#{work_item_id}[/bold] done: {row['title']}")


# ── sb rm ───────────────────────────────────────────────────────────────


@click.command(help="Delete a work item (use --force to skip the confirmation).")
@click.argument("work_item_id", type=int)
@click.option("-f", "--force", is_flag=True, help="Skip the confirmation prompt.")
@click.option(
    "--project-home",
    type=click.Path(file_okay=False),
    help="Override project home (default: from config or env)",
)
def rm(work_item_id: int, force: bool, project_home: str | None) -> None:
    from rich.prompt import Confirm

    ws = _ws_or_die(project_home)
    conn = ws.open_db()
    work_items.ensure_schema(conn)

    row = work_items.by_id(conn, work_item_id)
    if row is None:
        console.print(f"[red]✗[/red] no work_item with id {work_item_id}")
        raise click.Abort()

    if not force:
        console.print(
            f"\nAbout to delete: [bold]#{work_item_id}[/bold] — {row['title']}\n"
            f"  type={row['item_type']}, status={row['status']}, "
            f"owner={row.get('owner') or '—'}"
        )
        if not Confirm.ask("Delete?", default=False):
            console.print("[dim]aborted.[/dim]")
            return

    conn.execute("DELETE FROM work_items WHERE id=?", (work_item_id,))
    conn.commit()
    console.print(f"[green]✓[/green] deleted #{work_item_id}")


# ── sb show ─────────────────────────────────────────────────────────────


@click.command(help="Show a work item by id.")
@click.argument("work_item_id", type=int)
@click.option(
    "--project-home",
    type=click.Path(file_okay=False),
    help="Override project home (default: from config or env)",
)
def show(work_item_id: int, project_home: str | None) -> None:
    ws = _ws_or_die(project_home)
    conn = ws.open_db()
    work_items.ensure_schema(conn)

    row = work_items.by_id(conn, work_item_id)
    if row is None:
        console.print(f"[red]✗[/red] no work_item with id {work_item_id}")
        raise click.Abort()

    lines = [
        f"[bold]#{row['id']}: {row['title']}[/bold]",
        "",
        f"  type:       {row['item_type']}",
        f"  status:     {row['status']}",
        f"  priority:   {row['priority']}",
        f"  owner:      {row.get('owner') or '[dim]—[/dim]'}",
        f"  requester:  {row.get('requester') or '[dim]—[/dim]'}",
        f"  due_date:   {row.get('due_date') or '[dim]—[/dim]'}",
        f"  stakes:     {row.get('stakes') or '[dim]—[/dim]'}",
        f"  source:     {row.get('source') or '[dim]—[/dim]'}",
        f"  wiki_path:  {row.get('wiki_path') or '[dim]—[/dim]'}",
        f"  created:    {row['first_seen_at']}",
        f"  updated:    {row['last_updated_at']}",
    ]
    if row.get("kind") == "promise":
        lines.insert(2, f"  [yellow]PROMISE[/yellow]")
        lines.append(f"  made_to:    {row.get('made_to') or '[dim]—[/dim]'}")
        lines.append(f"  topic:      {row.get('topic') or '[dim]—[/dim]'}")
    console.print(Panel("\n".join(lines), border_style="cyan", expand=False))
