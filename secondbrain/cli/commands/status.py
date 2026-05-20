"""`sb status` — what needs my attention, ranked by urgency."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import click
from rich.console import Console
from rich.table import Table

from secondbrain.core import work_items
from secondbrain.core.workspace import Workspace, WorkspaceError

console = Console()


@click.command(help="Show what needs your attention.")
@click.option(
    "--project-home",
    type=click.Path(file_okay=False),
    help="Override project home (default: from config or env)",
)
@click.option("--overdue", is_flag=True, help="Show only overdue items")
@click.option("--owner", help="Filter by owner (substring match)")
@click.option("--limit", type=int, default=20, help="Max rows (default 20)")
def status(project_home: str | None, overdue: bool, owner: str | None, limit: int) -> None:
    try:
        ws = Workspace(project_home)
    except WorkspaceError as e:
        console.print(f"[red]✗[/red] {e}")
        raise click.Abort()

    if not ws.db_path.exists():
        console.print(
            "[yellow]No workbench.db yet.[/yellow] Run [cyan]sb scan[/cyan] "
            "or import some data first."
        )
        return

    # Local date — UTC would prematurely classify today's items as overdue after ~5pm Pacific
    today_date = datetime.now().date()
    today = today_date.isoformat()
    week_cutoff = (today_date + timedelta(days=7)).isoformat()

    rows = work_items.query(
        ws.conn,
        item_type="action",
        status="open",
        owner=owner,
        overdue=overdue,
        limit=limit,
    )

    if not rows:
        msg = "No overdue items" if overdue else "No open actions"
        if owner:
            msg += f" for [cyan]{owner}[/cyan]"
        console.print(f"[green]✓[/green] {msg}.")
        return

    # Bucket by urgency using real date math (today, today+7)
    overdue_rows = []
    today_rows = []
    week_rows = []
    later_rows = []
    for r in rows:
        due = r.get("due_date") or ""
        if due and due < today:
            overdue_rows.append(r)
        elif due == today:
            today_rows.append(r)
        elif due and today < due <= week_cutoff:
            week_rows.append(r)
        else:
            later_rows.append(r)

    def _add(table: Table, r: dict) -> None:
        wid = str(r["id"])
        title = r["title"]
        if len(title) > 72:
            title = title[:69] + "..."
        owner_disp = r.get("owner") or "[dim]—[/dim]"
        due_disp = r.get("due_date") or "[dim]—[/dim]"
        table.add_row(wid, title, owner_disp, due_disp)

    if overdue_rows:
        t = Table(title=f"[red]Overdue ({len(overdue_rows)})[/red]", show_header=True, header_style="bold red")
        t.add_column("id", style="dim", width=4)
        t.add_column("title")
        t.add_column("owner", style="cyan")
        t.add_column("due", style="red")
        for r in overdue_rows:
            _add(t, r)
        console.print(t)

    if today_rows:
        t = Table(title=f"[yellow]Due today ({len(today_rows)})[/yellow]", show_header=True, header_style="bold yellow")
        t.add_column("id", style="dim", width=4)
        t.add_column("title")
        t.add_column("owner", style="cyan")
        t.add_column("due", style="yellow")
        for r in today_rows:
            _add(t, r)
        console.print(t)

    if week_rows:
        t = Table(title=f"Due this week ({len(week_rows)})", show_header=True, header_style="bold")
        t.add_column("id", style="dim", width=4)
        t.add_column("title")
        t.add_column("owner", style="cyan")
        t.add_column("due")
        for r in week_rows:
            _add(t, r)
        console.print(t)

    if later_rows and not overdue:
        t = Table(title=f"Open ({len(later_rows)})", show_header=True, header_style="bold")
        t.add_column("id", style="dim", width=4)
        t.add_column("title")
        t.add_column("owner", style="cyan")
        t.add_column("due")
        for r in later_rows[:10]:  # cap "later" display
            _add(t, r)
        if len(later_rows) > 10:
            t.add_row("[dim]…[/dim]", f"[dim]+{len(later_rows)-10} more[/dim]", "", "")
        console.print(t)
