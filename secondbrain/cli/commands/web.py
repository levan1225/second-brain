"""`sb web` — run the localhost dashboard."""

from __future__ import annotations

import sys
import webbrowser

import click
from rich.console import Console

console = Console()


@click.command(help="Run the localhost web dashboard.")
@click.option("--port", type=int, default=8765, help="Port (default 8765, auto-fallback if busy)")
@click.option("--host", default="127.0.0.1", help="Host (default 127.0.0.1 — localhost only)")
@click.option("--no-browser", is_flag=True, help="Don't auto-open browser")
@click.option("--reload", is_flag=True, help="Auto-reload on code changes (development)")
def web(port: int, host: str, no_browser: bool, reload: bool) -> None:
    try:
        import uvicorn
        from secondbrain.web.app import find_free_port
    except ImportError as e:
        console.print("[red]✗[/red] web deps not installed.")
        console.print("  Install with: [cyan]pip install 'secondbrain[web]'[/cyan]")
        console.print(f"  ({e})")
        sys.exit(1)

    actual_port = find_free_port(port)
    url = f"http://{host}:{actual_port}"

    if actual_port != port:
        console.print(f"[yellow]port {port} busy[/yellow] — using {actual_port}")

    console.print(f"[green]●[/green] secondbrain web dashboard at [cyan]{url}[/cyan]")
    console.print("[dim]Ctrl+C to stop[/dim]\n")

    if not no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    uvicorn.run(
        "secondbrain.web.app:app",
        host=host,
        port=actual_port,
        log_level="warning",
        reload=reload,
        # When reload=True, factory-style is needed; we use module:attr above
    )
