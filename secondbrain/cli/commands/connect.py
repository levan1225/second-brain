"""`sb connect` — manage connectors.

Examples:
  sb connect list                  Show available + configured connectors
  sb connect slack                 Walk through auth for Slack
  sb connect status                Health-check every configured connector
  sb connect remove slack          Revoke stored credentials
  sb connect test slack            Send a test message to self
"""

from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from secondbrain.connectors import (
    Connector,
    SendAction,
    get_connector,
    list_connectors,
)

console = Console()


@click.group(invoke_without_command=True,
             help="Manage third-party connectors (for Slack/Outlook, use Claude Desktop instead).")
@click.pass_context
def connect(ctx: click.Context) -> None:
    """Default behavior when called with no subcommand: list connectors."""
    if ctx.invoked_subcommand is None:
        _list_connectors()


def _list_connectors() -> None:
    available = list_connectors()
    if not available:
        console.print()
        console.print("[bold]No third-party connectors installed.[/bold]")
        console.print()
        console.print("For Slack + Outlook integration:")
        console.print("  → Use Claude Desktop's built-in connectors:")
        console.print("    [cyan]Claude Desktop → Settings → Connectors → Add Slack / Outlook[/cyan]")
        console.print("  → Then Claude sessions can post to Slack / read your calendar")
        console.print("    using its own authenticated connection.")
        console.print()
        console.print("For other systems (Jira, Smartsheet, internal tools):")
        console.print("  → Install third-party connectors from PyPI, e.g.:")
        console.print("    [cyan]pip install secondbrain-jira-connector[/cyan]")
        console.print()
        return

    t = Table(title="Connectors", show_header=True, header_style="bold")
    t.add_column("name", style="cyan")
    t.add_column("source")
    t.add_column("available")
    t.add_column("status")
    t.add_column("identity", style="dim")
    for name, info in available.items():
        if not info["available"]:
            t.add_row(name, info["source"], "[red]no[/red]",
                      info.get("missing") or info.get("error", "—"), "")
            continue
        # Check status
        c = get_connector(name)
        if c is None:
            t.add_row(name, info["source"], "[yellow]?[/yellow]", "load failed", "")
            continue
        try:
            s = c.status()
        except Exception as e:
            t.add_row(name, info["source"], "[red]err[/red]", f"{type(e).__name__}: {e}", "")
            continue
        if s.connected:
            t.add_row(name, info["source"], "[green]yes[/green]",
                      "[green]connected[/green]", s.identity or "—")
        else:
            t.add_row(name, info["source"], "[green]yes[/green]",
                      f"[dim]{s.error or 'not configured'}[/dim]", "—")
    console.print(t)
    console.print()
    console.print("[dim]To configure: [cyan]sb connect <name>[/cyan][/dim]")


@connect.command(name="list", help="List available + configured connectors.")
def list_cmd() -> None:
    _list_connectors()


@connect.command(name="status", help="Show health for all configured connectors.")
def status() -> None:
    _list_connectors()


@connect.command(name="remove", help="Disconnect and remove stored credentials.")
@click.argument("name")
def remove(name: str) -> None:
    c = get_connector(name)
    if c is None:
        console.print(f"[red]✗[/red] no connector named [cyan]{name}[/cyan]")
        raise click.Abort()
    c.disconnect()
    console.print(f"[green]✓[/green] {name} disconnected (credentials removed)")


@connect.command(name="test", help="Send a test message via a connector.")
@click.argument("name")
@click.option("--target", help="Channel id / user id / 'self' (default: self)")
@click.option("--text", default="🧪 secondbrain test message — connector is working.")
def test(name: str, target: str | None, text: str) -> None:
    c = get_connector(name)
    if c is None:
        console.print(f"[red]✗[/red] no connector named [cyan]{name}[/cyan]")
        raise click.Abort()
    s = c.status()
    if not s.connected:
        console.print(f"[red]✗[/red] {name} not connected: {s.error}")
        console.print(f"  configure with: [cyan]sb connect {name}[/cyan]")
        raise click.Abort()

    action = SendAction(
        target=target or "self",
        content=text,
        kind="dm" if (target or "self") == "self" else "message",
    )
    result = c.send(action)
    if result.success:
        console.print(f"[green]✓[/green] sent — message_id={result.message_id}")
    else:
        console.print(f"[red]✗[/red] {result.error}")


def _make_connect_command(name: str) -> click.Command:
    """Dynamic per-connector command: `sb connect slack`, `sb connect outlook`, etc."""
    @click.command(name=name, help=f"Configure the {name} connector.")
    def _cmd() -> None:
        c = get_connector(name)
        if c is None:
            console.print(f"[red]✗[/red] connector [cyan]{name}[/cyan] not available")
            console.print(f"  Install with: [cyan]pip install 'secondbrain[{name}]'[/cyan]")
            raise click.Abort()
        result = c.authenticate()
        if result.connected:
            console.print(f"[green]✓[/green] {name} connected as [cyan]{result.identity}[/cyan]")
            console.print(f"  test with: [cyan]sb connect test {name}[/cyan]")
        else:
            console.print(f"[red]✗[/red] {name} not connected: {result.error}")
            raise click.Abort()
    return _cmd


# Dynamically register a subcommand for every known connector
for _name in list_connectors():
    connect.add_command(_make_connect_command(_name))
