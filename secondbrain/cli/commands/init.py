"""`sb init` — create a new project home with the standard layout."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.prompt import Confirm, Prompt

from secondbrain.core.workspace import CONFIG_PATH

console = Console()


@click.command(help="Initialize a new secondbrain project home.")
@click.option(
    "--path",
    "project_path",
    type=click.Path(file_okay=False),
    help="Where to create the project home (default: ~/Documents/secondbrain/<slug>)",
)
@click.option("--name", help="Human-readable project name (default: prompt)")
@click.option("--owner", help="Your name as you want it stored (default: prompt; non-interactive falls back to $USER)")
@click.option("--non-interactive", is_flag=True, help="Use defaults, no prompts")
@click.option(
    "--set-default",
    is_flag=True,
    help="Write this project home to ~/.config/secondbrain/config.yaml as the default",
)
def init(
    project_path: str | None,
    name: str | None,
    owner: str | None,
    non_interactive: bool,
    set_default: bool,
) -> None:
    """Create a new project home directory with the standard layout."""

    # Resolve name + path
    if not name:
        if non_interactive:
            name = "my-pilot"
        else:
            name = Prompt.ask("Project name", default="my-pilot")
    slug = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")

    # Resolve owner (your name as it'll appear in work_items, briefings, etc.)
    import os as _os
    if not owner:
        if non_interactive:
            owner = _os.environ.get("USER", "owner")
        else:
            default_owner = _os.environ.get("USER", "")
            console.print(
                "\n[dim]Your name as you'd like it stored — this becomes the "
                "'owner' on work_items you're responsible for, and is used by the "
                "aging-commitment escalator to filter what you owe vs. what's owed "
                "to you. Use your real name (e.g. 'Van Nguyen'), not a username.[/dim]"
            )
            owner = Prompt.ask("Your name", default=default_owner if default_owner else None)

    default_path = Path.home() / "Documents" / "secondbrain" / slug
    if not project_path:
        if non_interactive:
            project_path = str(default_path)
        else:
            project_path = Prompt.ask("Project home directory", default=str(default_path))

    home = Path(project_path).expanduser().resolve()

    if home.exists() and any(home.iterdir()):
        if not non_interactive and not Confirm.ask(
            f"[yellow]{home} exists and is not empty. Continue?[/yellow]",
            default=False,
        ):
            console.print("[red]aborted[/red]")
            raise click.Abort()

    console.print(f"\nCreating project home at [cyan]{home}[/cyan]...")

    # Standard layout
    for sub in (
        "config/canonical",
        "schema/platform",
        "schema/formats",
        "wiki/projects",
        "wiki/people",
        "wiki/concepts",
        "wiki/ideas",
        "wiki/patterns",
        "wiki/context",
        "plans",
        "drafts",
        "final",
        "archive",
        "synthesis",
        "sources/raw",
        "state",
        "memory",
        "logs",
        "innovation-created-by-claude",
        "my-custom-rules",
    ):
        (home / sub).mkdir(parents=True, exist_ok=True)
    console.print("  [green]✓[/green] folder scaffold")

    # identity.md — owner captured above, rest can be filled later via `sb identity set`
    today = date.today().isoformat()
    safe_owner = (owner or "").replace('"', '\\"')
    (home / "identity.md").write_text(
        f"""---
owner: "{safe_owner}"
role: ""
slack_user_id: ""
team: ""
timezone: PT
created: {today}
---

# Identity

**Owner:** {safe_owner}

Other fields (role, team, slack_user_id) can be edited directly above, or set
interactively with `sb identity set`. The owner field above is read by the
aging-commitment escalator and the Executor (when drafting in your voice).
""",
        encoding="utf-8",
    )

    # version.json
    (home / "version.json").write_text(
        '{"schema_version": "v3.0.0", "created_on": "' + today + '"}\n',
        encoding="utf-8",
    )

    # Empty state file
    state = home / "state" / "scan-state.json"
    if not state.exists():
        state.write_text('{"last_scan": null, "cursors": {}}\n', encoding="utf-8")

    # Seed empty pattern + context pages so future scans don't fail
    seed_files = {
        "wiki/context/commitments.md": "Commitments",
        "wiki/context/weekly-digest.md": "Weekly Digest",
        "wiki/patterns/thinking.md": "Thinking Patterns",
        "wiki/patterns/voice-profiles.md": "Voice Profiles",
    }
    for rel, title in seed_files.items():
        p = home / rel
        if not p.exists():
            p.write_text(
                f"---\ntitle: {title}\nlast_updated: {today}\n---\n\n# {title}\n\n*(empty — first scan has not yet run)*\n",
                encoding="utf-8",
            )
    console.print("  [green]✓[/green] seed files")

    # In --non-interactive mode, never prompt. Only honor --set-default.
    should_set_default = set_default
    if not non_interactive and not set_default:
        should_set_default = Confirm.ask(
            f"\nSet this as your default project home? "
            f"(writes to [cyan]{CONFIG_PATH}[/cyan])",
            default=True,
        )

    if should_set_default:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if CONFIG_PATH.exists():
            try:
                existing = yaml.safe_load(CONFIG_PATH.read_text()) or {}
            except yaml.YAMLError:
                existing = {}
        existing["project_home"] = str(home)
        CONFIG_PATH.write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")
        console.print(f"  [green]✓[/green] default set in {CONFIG_PATH}")

    console.print(f"\n[green]✓ project home ready[/green]: {home}\n")
    console.print("Next steps:")
    console.print("  • Edit [cyan]identity.md[/cyan] with your name, role, Slack ID")
    console.print("  • Run [cyan]sb info[/cyan] to inspect the workspace")
    console.print("  • Run [cyan]sb status[/cyan] to see what needs your attention")
