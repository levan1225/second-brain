"""sb CLI entry point.

The `sb` command is the user-facing surface. All real work happens in
secondbrain.core; this file is just argument parsing + display.
"""

from __future__ import annotations

import sys

import click
from rich.console import Console

from secondbrain import __version__
from secondbrain.cli.commands import init as cmd_init
from secondbrain.cli.commands import status as cmd_status
from secondbrain.cli.commands import info as cmd_info
from secondbrain.cli.commands import scan as cmd_scan
from secondbrain.cli.commands import people as cmd_people
from secondbrain.cli.commands import projects as cmd_projects
from secondbrain.cli.commands import migrate_from_v2 as cmd_migrate
from secondbrain.cli.commands import mcp as cmd_mcp
from secondbrain.cli.commands import daemon as cmd_daemon
from secondbrain.cli.commands import connect as cmd_connect
from secondbrain.cli.commands import web as cmd_web
from secondbrain.cli.commands import identity as cmd_identity
from secondbrain.cli.commands import items as cmd_items

console = Console()


@click.group(
    help="secondbrain — your AI-augmented operating system for programs, people, and commitments.\n\n"
    "Quick start:\n"
    "  sb init                          Create a new project home\n"
    "  sb status                        What needs your attention\n"
    "  sb info                          Show what the workspace contains\n"
)
@click.version_option(version=__version__, prog_name="sb")
def main() -> None:
    pass


# Register subcommands
main.add_command(cmd_init.init)
main.add_command(cmd_status.status)
main.add_command(cmd_info.info)
main.add_command(cmd_scan.scan)
main.add_command(cmd_people.people)
main.add_command(cmd_projects.projects)
main.add_command(cmd_migrate.migrate_from_v2)
main.add_command(cmd_mcp.mcp)
main.add_command(cmd_daemon.daemon)
main.add_command(cmd_connect.connect)
main.add_command(cmd_web.web)
main.add_command(cmd_identity.identity)
main.add_command(cmd_items.add)
main.add_command(cmd_items.done)
main.add_command(cmd_items.rm)
main.add_command(cmd_items.show)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]aborted[/yellow]")
        sys.exit(130)
