"""`sb people` — list and inspect people in the workspace.

Reads from wiki/people/{slug}.md frontmatter. The canonical `people.yaml`
(if present) is checked for cross-reference but the wiki is the display surface.
"""

from __future__ import annotations

import click
import yaml
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from secondbrain.core import work_items
from secondbrain.core.wiki import list_pages, read_page
from secondbrain.core.workspace import Workspace, WorkspaceError

console = Console()


@click.command(help="List people in the workspace, or inspect one by slug.")
@click.argument("slug", required=False)
@click.option(
    "--project-home",
    type=click.Path(file_okay=False),
    help="Override project home (default: from config or env)",
)
@click.option("--with-commitments", is_flag=True, help="Include open commitments per person")
def people(slug: str | None, project_home: str | None, with_commitments: bool) -> None:
    try:
        ws = Workspace(project_home)
    except WorkspaceError as e:
        console.print(f"[red]✗[/red] {e}")
        raise click.Abort()

    if slug:
        _show_one(ws, slug, with_commitments)
    else:
        _show_list(ws, with_commitments)


def _show_list(ws: Workspace, with_commitments: bool) -> None:
    pages = list_pages(ws.wiki_root, "people")
    if not pages:
        console.print("[yellow]No people pages yet.[/yellow] "
                      "Run [cyan]sb scan[/cyan] to populate.")
        return

    table = Table(title=f"People ({len(pages)})", show_header=True, header_style="bold")
    table.add_column("slug", style="cyan")
    table.add_column("name")
    table.add_column("role", style="dim")
    table.add_column("relationship")
    if with_commitments and ws.db_path.exists():
        table.add_column("open", justify="right")

    for page in pages:
        fm = page.frontmatter
        row = [
            page.slug,
            fm.get("title", "[dim]—[/dim]"),
            (fm.get("role") or "")[:50],
            fm.get("relationship", "[dim]—[/dim]"),
        ]
        if with_commitments and ws.db_path.exists():
            try:
                name = fm.get("title", page.slug)
                rows = work_items.query(ws.conn, item_type="action", status="open", owner=name, limit=100)
                row.append(str(len(rows)) if rows else "[dim]0[/dim]")
            except Exception:
                row.append("[dim]?[/dim]")
        table.add_row(*row)
    console.print(table)


def _show_one(ws: Workspace, slug: str, with_commitments: bool) -> None:
    page = read_page(ws.wiki_root, f"people/{slug}.md")
    if not page.frontmatter and not page.body.strip():
        console.print(f"[red]✗[/red] No person page found at "
                      f"[cyan]wiki/people/{slug}.md[/cyan]")
        console.print("\nAvailable people:")
        for p in list_pages(ws.wiki_root, "people"):
            console.print(f"  • [cyan]{p.slug}[/cyan]")
        raise click.Abort()

    fm = page.frontmatter
    header_lines = [f"[bold cyan]{fm.get('title', slug)}[/bold cyan]"]
    if fm.get("role"):
        header_lines.append(f"[dim]{fm['role']}[/dim]")
    console.print(Panel.fit("\n".join(header_lines), border_style="cyan"))

    # Frontmatter facts table
    facts = Table(show_header=False, box=None, padding=(0, 1))
    facts.add_column(style="dim")
    facts.add_column()
    for key in ("slug", "relationship", "trust_tier", "team", "slack_user_id",
                "email", "canonical_id", "last_updated", "staleness"):
        if fm.get(key):
            facts.add_row(key, str(fm[key]))
    if fm.get("aliases"):
        facts.add_row("aliases", ", ".join(fm["aliases"]))
    if fm.get("collaboration_domains"):
        facts.add_row("domains", ", ".join(fm["collaboration_domains"]))
    console.print(facts)

    # Open commitments (where they're owner)
    if with_commitments and ws.db_path.exists():
        try:
            name = fm.get("title", slug)
            rows = work_items.query(ws.conn, item_type="action", status="open", owner=name, limit=20)
            if rows:
                console.print()
                t = Table(title=f"[bold]Open commitments owned by {name}[/bold]",
                          show_header=True, header_style="bold")
                t.add_column("id", style="dim", width=4)
                t.add_column("title")
                t.add_column("due", style="yellow")
                for r in rows:
                    title = r["title"][:70] + ("..." if len(r["title"]) > 70 else "")
                    t.add_row(str(r["id"]), title, r.get("due_date") or "[dim]—[/dim]")
                console.print(t)
        except Exception:
            pass

    # Body excerpt
    if page.body.strip():
        console.print()
        # Show first 30 lines of body
        body_excerpt = "\n".join(page.body.strip().splitlines()[:30])
        console.print(Markdown(body_excerpt))
