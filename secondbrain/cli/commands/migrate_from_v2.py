"""`sb migrate-from-v2` — adopt an existing v2 (Second Brain + PCE) project home.

This is NOT the v1→v2 migration (`/sb-pce-migrate-v2`). This is the v2→v3
adoption step: the user already has a working v2 project home (flat layout
with wiki/, state/workbench.db, config/canonical/) and wants to start using
the v3 `sb` CLI against it.

What it does:
  1. Validates the path looks like a v2 project home (state/workbench.db,
     wiki/, optionally config/canonical/people.yaml)
  2. Writes ~/.config/secondbrain/config.yaml with project_home set
  3. (Optional) rewrites the second-brain MCP entry in claude_desktop_config.json
     to point at this package's `sb mcp` (Phase 4 — not done yet, so off by default)
  4. Reports a summary of what's adoptable

No files in the project home are mutated. Fully reversible (just delete the
config.yaml).
"""

from __future__ import annotations

import json
import platform
from datetime import date
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from secondbrain.core.workspace import CONFIG_PATH, Workspace, WorkspaceError

console = Console()


def _claude_desktop_config_path() -> Path | None:
    """Best-effort location of Claude Desktop's config.json by OS."""
    sys = platform.system()
    if sys == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys == "Linux":
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
    if sys == "Windows":
        return Path.home() / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    return None


def _validate_v2_layout(project_home: Path) -> dict[str, bool]:
    """Return a checklist of what v2 artifacts are present."""
    return {
        "workbench.db": (project_home / "state" / "workbench.db").exists(),
        "wiki/": (project_home / "wiki").is_dir(),
        "wiki/people/": (project_home / "wiki" / "people").is_dir(),
        "wiki/context/commitments.md": (project_home / "wiki" / "context" / "commitments.md").exists(),
        "config/canonical/people.yaml": (project_home / "config" / "canonical" / "people.yaml").exists(),
        "CLAUDE.md": (project_home / "CLAUDE.md").exists(),
    }


@click.command(name="migrate-from-v2", help="Adopt an existing v2 project home as your v3 default.")
@click.option(
    "--project-home",
    required=True,
    type=click.Path(file_okay=False),
    help="Path to your existing v2 project home",
)
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Don't prompt; just do it",
)
@click.option(
    "--update-claude-desktop",
    is_flag=True,
    help="Rewrite the second-brain MCP entry to use `sb mcp` "
         "(WARNING: requires `sb mcp` to be implemented — Phase 4. Off by default.)",
)
def migrate_from_v2(project_home: str, non_interactive: bool, update_claude_desktop: bool) -> None:
    home = Path(project_home).expanduser().resolve()

    if not home.is_dir():
        console.print(f"[red]✗[/red] Not a directory: {home}")
        raise click.Abort()

    console.print(Panel.fit(
        f"[bold cyan]{home}[/bold cyan]",
        title="v2 → v3 adoption",
        border_style="cyan",
    ))

    # Step 1 — validate
    checks = _validate_v2_layout(home)
    table = Table(show_header=True, header_style="bold", title="v2 artifacts detected")
    table.add_column("artifact", style="cyan")
    table.add_column("status")
    for name, present in checks.items():
        mark = "[green]✓[/green]" if present else "[dim]—[/dim]"
        table.add_row(name, mark)
    console.print(table)

    if not checks["workbench.db"] and not checks["wiki/"]:
        console.print("\n[red]✗[/red] This doesn't look like a v2 project home. "
                      "Expected at least state/workbench.db or wiki/.")
        raise click.Abort()

    # Step 2 — show data summary using v3's Workspace
    try:
        ws = Workspace(home)
        info = ws.info()
    except WorkspaceError as e:
        console.print(f"[red]✗[/red] {e}")
        raise click.Abort()

    console.print()
    summary = Table(show_header=False, box=None, padding=(0, 2))
    summary.add_column(style="dim")
    summary.add_column()
    if info.get("wiki_exists"):
        for cat in ("projects", "people", "concepts", "ideas", "patterns", "context"):
            n = info.get(f"wiki_{cat}", 0)
            if n:
                summary.add_row(f"wiki/{cat}/", f"{n} pages")
    if info.get("work_items_total") is not None and info["work_items_total"] > 0:
        summary.add_row("work_items", f"{info['work_items_total']} total")
    console.print(summary)

    # Step 3 — confirm
    if not non_interactive:
        console.print()
        if not Confirm.ask(
            f"Set [cyan]{home}[/cyan] as your default secondbrain project home?",
            default=True,
        ):
            console.print("[yellow]aborted[/yellow]")
            raise click.Abort()

    # Step 4 — write config.yaml
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if CONFIG_PATH.exists():
        try:
            existing = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        except yaml.YAMLError:
            existing = {}
    existing["project_home"] = str(home)
    existing.setdefault("migrated_from_v2_on", date.today().isoformat())
    CONFIG_PATH.write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")
    console.print(f"\n[green]✓[/green] Wrote [cyan]{CONFIG_PATH}[/cyan]")

    # Step 5 — optionally rewrite Claude Desktop MCP entry
    if update_claude_desktop:
        cd_path = _claude_desktop_config_path()
        if cd_path is None or not cd_path.exists():
            console.print(f"[yellow]⚠[/yellow] Claude Desktop config not found "
                          f"(expected at {cd_path}); skipping MCP rewrite.")
        else:
            try:
                cfg = json.loads(cd_path.read_text())
            except json.JSONDecodeError as e:
                console.print(f"[yellow]⚠[/yellow] Could not parse {cd_path}: {e}")
                cfg = None
            if cfg is not None:
                cfg.setdefault("mcpServers", {})
                # Back up the prior entry, if any
                prior = cfg["mcpServers"].get("second-brain")
                if prior:
                    cfg["mcpServers"]["second-brain-v2-backup"] = prior
                cfg["mcpServers"]["second-brain"] = {
                    "command": "sb",
                    "args": ["mcp"],
                    "env": {
                        "SECONDBRAIN_HOME": str(home),
                    },
                }
                cd_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
                console.print(f"[green]✓[/green] Updated Claude Desktop MCP entry "
                              f"→ `sb mcp` (prior backed up as `second-brain-v2-backup`)")
                console.print(f"[dim]Restart Claude Desktop to pick up the change.[/dim]")

    # Step 6 — next-steps
    console.print()
    console.print("[bold green]✓ adopted[/bold green]\n")
    console.print("You can now run:")
    console.print("  [cyan]sb info[/cyan]            ← no more --project-home flag needed")
    console.print("  [cyan]sb status[/cyan]")
    console.print("  [cyan]sb people[/cyan]")
    console.print("  [cyan]sb scan[/cyan]")
    if not update_claude_desktop:
        console.print()
        console.print("[dim]Tip: when `sb mcp` is implemented (Phase 4), re-run with[/dim]")
        console.print("[dim]  sb migrate-from-v2 --project-home '...' --update-claude-desktop[/dim]")
        console.print("[dim]to point Claude Desktop at this package's MCP server.[/dim]")
