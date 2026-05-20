"""`sb mcp` — run the MCP stdio server.

Typically invoked by an LLM client (Claude Desktop, Cursor, Continue) via
its mcpServers config:

    "second-brain": {
        "command": "sb",
        "args": ["mcp"]
    }

Add `"env": { "SECONDBRAIN_HOME": "/path/to/project" }` if you want to pin
the project home rather than relying on the default in ~/.config/secondbrain/.
"""

from __future__ import annotations

import sys

import click


@click.command(help="Run the MCP stdio server (for Claude Desktop / Cursor / etc).")
@click.option(
    "--check",
    is_flag=True,
    help="Don't start the server — just verify the MCP SDK is installed and tools list cleanly.",
)
def mcp(check: bool) -> None:
    try:
        from secondbrain.mcp.server import TOOL_DEFS, main as server_main
    except ImportError as e:
        click.echo(
            "✗ MCP SDK not installed.\n"
            "  Install with: pip install 'secondbrain[mcp]'\n"
            f"  (Underlying error: {e})",
            err=True,
        )
        sys.exit(1)

    if check:
        click.echo(f"✓ MCP SDK loaded")
        click.echo(f"✓ {len(TOOL_DEFS)} tools registered:")
        for tool in TOOL_DEFS:
            click.echo(f"    • {tool.name}")
        return

    # Start the server (blocks until stdin closes)
    server_main()
