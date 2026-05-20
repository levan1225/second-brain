"""`sb identity` — view + edit identity.md without manually editing YAML.

The owner field is critical:
  - The aging-commitment escalator filters items where owner = identity.owner
  - The Executor (when drafting in your voice) reads identity.owner

Subcommands:
  sb identity              show current identity
  sb identity set          interactive prompt to update fields
  sb identity set --field value (e.g. --owner "Van Nguyen")
"""

from __future__ import annotations

import click
import yaml
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from secondbrain.core.workspace import Workspace, WorkspaceError

console = Console()


def _read_identity(ws: Workspace) -> dict:
    """Parse identity.md frontmatter. Returns empty dict if missing."""
    path = ws.project_home / "identity.md"
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        return {}
    end = raw.find("\n---\n", 4)
    if end < 0:
        return {}
    try:
        return yaml.safe_load(raw[4:end]) or {}
    except yaml.YAMLError:
        return {}


def _write_identity(ws: Workspace, fm: dict) -> None:
    """Rewrite identity.md preserving the body below frontmatter."""
    path = ws.project_home / "identity.md"
    body = ""
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        if raw.startswith("---\n"):
            end = raw.find("\n---\n", 4)
            if end > 0:
                body = raw[end + 5:]
    # Coerce date-like values to strings for clean YAML
    fm_clean = {k: (str(v) if hasattr(v, "isoformat") else v) for k, v in fm.items()}
    yaml_text = yaml.safe_dump(fm_clean, sort_keys=False, default_flow_style=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{yaml_text}---\n{body}", encoding="utf-8")


@click.group(invoke_without_command=True, help="View or update your identity.md.")
@click.option(
    "--project-home",
    type=click.Path(file_okay=False),
    help="Override project home (default: from config or env)",
)
@click.pass_context
def identity(ctx: click.Context, project_home: str | None) -> None:
    ctx.obj = {"project_home": project_home}
    if ctx.invoked_subcommand is None:
        _show(ctx)


def _show(ctx: click.Context) -> None:
    try:
        ws = Workspace(ctx.obj.get("project_home"))
    except WorkspaceError as e:
        console.print(f"[red]✗[/red] {e}")
        raise click.Abort()

    fm = _read_identity(ws)
    if not fm:
        console.print("[yellow]No identity.md found.[/yellow] Run `sb identity set` to create one.")
        return

    t = Table(title="Identity", show_header=False, box=None, padding=(0, 2))
    t.add_column(style="dim")
    t.add_column()
    for key in ("owner", "role", "team", "slack_user_id", "self_dm_channel_id",
                "timezone", "vault_path", "created"):
        val = fm.get(key)
        if val:
            t.add_row(key, str(val))
    console.print(t)

    # Warn about common pitfall
    if fm.get("owner") and fm["owner"].islower() and "@" not in fm["owner"] and len(fm["owner"]) < 12:
        console.print(
            "\n[yellow]⚠[/yellow] Your owner looks like a username, not a real name. "
            "The aging-commitment escalator matches against work_items.owner — "
            "if your work_items use full names like 'Van Nguyen', set this to match. "
            "Update with: [cyan]sb identity set --owner \"Van Nguyen\"[/cyan]"
        )


@identity.command(name="show", help="Print the current identity.")
@click.pass_context
def show_cmd(ctx: click.Context) -> None:
    _show(ctx)


@identity.command(name="set", help="Update identity fields. Without flags, prompts interactively.")
@click.option("--owner", help="Your name as you want it stored (matches work_items.owner)")
@click.option("--role", help="Your role / title")
@click.option("--team", help="Your team / org")
@click.option("--slack-user-id", help="Your Slack user ID (e.g. U02ABC123)")
@click.option("--self-dm-channel-id", help="Your Slack self-DM channel ID")
@click.option("--timezone", help="Your timezone (default: PT)")
@click.option("--non-interactive", is_flag=True, help="Only update the flags passed; don't prompt for missing")
@click.pass_context
def set_cmd(
    ctx: click.Context,
    owner: str | None,
    role: str | None,
    team: str | None,
    slack_user_id: str | None,
    self_dm_channel_id: str | None,
    timezone: str | None,
    non_interactive: bool,
) -> None:
    try:
        ws = Workspace(ctx.obj.get("project_home"))
    except WorkspaceError as e:
        console.print(f"[red]✗[/red] {e}")
        raise click.Abort()

    fm = _read_identity(ws)

    updates: dict[str, str] = {}
    if owner is not None:
        updates["owner"] = owner
    if role is not None:
        updates["role"] = role
    if team is not None:
        updates["team"] = team
    if slack_user_id is not None:
        updates["slack_user_id"] = slack_user_id
    if self_dm_channel_id is not None:
        updates["self_dm_channel_id"] = self_dm_channel_id
    if timezone is not None:
        updates["timezone"] = timezone

    # If no flags AND interactive, prompt for each
    if not updates and not non_interactive:
        console.print("\n[bold]Update your identity[/bold] (press Enter to keep current value)\n")
        for key, label, hint in [
            ("owner", "Your name", "as it should appear in work_items, e.g. 'Van Nguyen'"),
            ("role", "Your role", "e.g. 'Senior TPM, PDX'"),
            ("team", "Team / org", "e.g. 'PDX, Intuit'"),
            ("slack_user_id", "Slack user ID", "U... (find in Slack profile)"),
            ("self_dm_channel_id", "Self-DM channel ID", "D..."),
            ("timezone", "Timezone", "e.g. 'PT', 'America/Los_Angeles'"),
        ]:
            current = str(fm.get(key, ""))
            new_val = Prompt.ask(f"  [cyan]{label}[/cyan] [dim]({hint})[/dim]", default=current)
            if new_val != current:
                updates[key] = new_val

    if not updates:
        console.print("[dim]No changes.[/dim]")
        return

    fm.update(updates)
    _write_identity(ws, fm)
    console.print(f"[green]✓[/green] Updated {len(updates)} field(s) in identity.md")
    for k, v in updates.items():
        console.print(f"    [dim]{k}:[/dim] {v}")
