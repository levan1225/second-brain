"""`sb info` — show what the workspace contains."""

from __future__ import annotations

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from secondbrain.core.workspace import Workspace, WorkspaceError

console = Console()


@click.command(help="Show workspace summary: paths, wiki counts, canonical row counts.")
@click.option(
    "--project-home",
    type=click.Path(file_okay=False),
    help="Override project home (default: from config or env)",
)
def info(project_home: str | None) -> None:
    try:
        with Workspace(project_home) as ws:
            data = ws.info()
    except WorkspaceError as e:
        console.print(f"[red]✗[/red] {e}")
        raise click.Abort()

    # Header
    console.print(
        Panel.fit(
            f"[bold cyan]{data['project_home']}[/bold cyan]",
            title="secondbrain workspace",
            border_style="cyan",
        )
    )

    # Wiki counts
    wiki_table = Table(title="wiki/", show_header=True, header_style="bold")
    wiki_table.add_column("category", style="cyan")
    wiki_table.add_column("pages", justify="right")
    for category in ("projects", "people", "concepts", "ideas", "patterns", "context"):
        wiki_table.add_row(category, str(data.get(f"wiki_{category}", 0)))
    console.print(wiki_table)

    # Canonical counts
    if data.get("work_items_total") is not None:
        wi_table = Table(title=f"work_items ({data['work_items_total']} total)", show_header=True, header_style="bold")
        wi_table.add_column("item_type", style="cyan")
        wi_table.add_column("status")
        wi_table.add_column("count", justify="right")
        for row in data.get("work_items", []):
            wi_table.add_row(row["item_type"], row["status"], str(row["n"]))
        console.print(wi_table)
    else:
        console.print("[yellow]work_items table not present yet[/yellow]")

    # Hints
    if not data.get("db_exists"):
        console.print("\n[dim]Note: state/workbench.db doesn't exist yet. "
                      "Run `sb scan` to populate.[/dim]")
