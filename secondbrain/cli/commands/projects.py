"""`sb projects` — list and inspect project pages.

Reads from wiki/projects/{slug}.md frontmatter. Symmetric with `sb people`.
"""

from __future__ import annotations

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from secondbrain.core import work_items
from secondbrain.core.wiki import list_pages, read_page
from secondbrain.core.workspace import Workspace, WorkspaceError

console = Console()


@click.command(help="List projects, or inspect one by slug.")
@click.argument("slug", required=False)
@click.option(
    "--project-home",
    type=click.Path(file_okay=False),
    help="Override project home (default: from config or env)",
)
@click.option("--with-items", is_flag=True, help="Include related open work_items per project")
def projects(slug: str | None, project_home: str | None, with_items: bool) -> None:
    try:
        ws = Workspace(project_home)
    except WorkspaceError as e:
        console.print(f"[red]✗[/red] {e}")
        raise click.Abort()

    if slug:
        _show_one(ws, slug, with_items)
    else:
        _show_list(ws, with_items)


def _show_list(ws: Workspace, with_items: bool) -> None:
    pages = list_pages(ws.wiki_root, "projects")
    if not pages:
        console.print("[yellow]No project pages yet.[/yellow] Drop a markdown file in wiki/projects/ "
                      "or run [cyan]sb scan[/cyan] to populate from sources/.")
        return

    table = Table(title=f"Projects ({len(pages)})", show_header=True, header_style="bold")
    table.add_column("slug", style="cyan")
    table.add_column("title")
    table.add_column("status", style="dim")
    table.add_column("last_updated", style="dim")
    if with_items and ws.db_path.exists():
        table.add_column("open items", justify="right")

    for page in pages:
        fm = page.frontmatter
        row = [
            page.slug,
            str(fm.get("title", "[dim]—[/dim]")),
            str(fm.get("status", "[dim]—[/dim]")),
            str(fm.get("last_updated", "[dim]—[/dim]")),
        ]
        if with_items and ws.db_path.exists():
            try:
                # Count work_items mentioning this project (loose token match)
                slug_lower = page.slug.lower()
                title_lower = (fm.get("title") or "").lower()
                all_open = work_items.query(ws.conn, item_type="action", status="open", limit=500)
                count = 0
                for r in all_open:
                    hay = f"{r.get('title','')} {r.get('stakes','')}".lower()
                    if slug_lower in hay or (title_lower and title_lower in hay):
                        count += 1
                row.append(str(count) if count else "[dim]0[/dim]")
            except Exception:
                row.append("[dim]?[/dim]")
        table.add_row(*row)
    console.print(table)


def _show_one(ws: Workspace, slug: str, with_items: bool) -> None:
    page = read_page(ws.wiki_root, f"projects/{slug}.md")
    if not page.frontmatter and not page.body.strip():
        console.print(f"[red]✗[/red] No project page found at "
                      f"[cyan]wiki/projects/{slug}.md[/cyan]")
        console.print("\nAvailable projects:")
        for p in list_pages(ws.wiki_root, "projects"):
            console.print(f"  • [cyan]{p.slug}[/cyan]")
        raise click.Abort()

    fm = page.frontmatter
    title = fm.get("title", slug)

    header_lines = [f"[bold cyan]{title}[/bold cyan]"]
    if fm.get("status"):
        header_lines.append(f"[dim]status: {fm['status']}[/dim]")
    console.print(Panel.fit("\n".join(header_lines), border_style="cyan"))

    # Frontmatter facts
    facts = Table(show_header=False, box=None, padding=(0, 1))
    facts.add_column(style="dim")
    facts.add_column()
    for key in ("slug", "status", "owner", "sponsor", "canonical_id",
                "last_updated", "created"):
        if fm.get(key):
            facts.add_row(key, str(fm[key]))
    if fm.get("aliases"):
        facts.add_row("aliases", ", ".join(fm["aliases"]))
    console.print(facts)

    # Related work_items
    if with_items and ws.db_path.exists():
        try:
            slug_lower = slug.lower()
            title_lower = title.lower()
            all_open = work_items.query(ws.conn, item_type="action", status="open", limit=500)
            related = [
                r for r in all_open
                if slug_lower in (r.get("title", "") + " " + (r.get("stakes") or "")).lower()
                or (title_lower and title_lower in (r.get("title", "") + " " + (r.get("stakes") or "")).lower())
            ]
            if related:
                console.print()
                t = Table(title=f"[bold]Related open items ({len(related)})[/bold]",
                          show_header=True, header_style="bold")
                t.add_column("id", style="dim", width=4)
                t.add_column("title")
                t.add_column("owner", style="cyan")
                t.add_column("due", style="yellow")
                for r in related[:20]:
                    title_disp = r["title"][:65] + ("..." if len(r["title"]) > 65 else "")
                    t.add_row(str(r["id"]), title_disp,
                              r.get("owner") or "[dim]—[/dim]",
                              r.get("due_date") or "[dim]—[/dim]")
                console.print(t)
        except Exception:
            pass

    # Body
    if page.body.strip():
        console.print()
        excerpt = "\n".join(page.body.strip().splitlines()[:40])
        console.print(Markdown(excerpt))
