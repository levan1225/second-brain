"""`sb daemon` — manage the background process.

Subcommands:
  sb daemon start         Start the scheduler in foreground
  sb daemon start --bg    Spawn as a background process
  sb daemon stop          Send SIGTERM to running daemon
  sb daemon status        Show running state + next-fire times
  sb daemon run-once <id> Fire one job immediately (for testing)
  sb daemon logs          Show recent fire history
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import click
from rich.console import Console
from rich.table import Table

from secondbrain.core.workspace import Workspace, WorkspaceError

console = Console()


@click.group(help="Manage the secondbrain background daemon (proactive features).")
def daemon() -> None:
    pass


@daemon.command(help="Start the daemon (foreground by default).")
@click.option("--bg", is_flag=True, help="Spawn as a background process and return immediately")
def start(bg: bool) -> None:
    try:
        from secondbrain.daemon import state as daemon_state
        from secondbrain.daemon.server import run_forever
    except ImportError as e:
        console.print("[red]✗[/red] daemon deps not installed.")
        console.print("  Install with: [cyan]pip install 'secondbrain[daemon]'[/cyan]")
        console.print(f"  ({e})")
        raise click.Abort()

    if daemon_state.is_daemon_running():
        pid = daemon_state.read_pid()
        console.print(f"[yellow]daemon already running[/yellow] (pid {pid})")
        console.print("  use [cyan]sb daemon stop[/cyan] to stop it")
        raise click.Abort()

    if bg:
        # Spawn as a fully detached subprocess and return.
        # subprocess inherits PATH so `sb` resolves correctly.
        log_dir = daemon_state.CONFIG_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "daemon.log"

        # Use Popen with start_new_session so the daemon survives this shell exiting
        with open(log_path, "ab") as logf:
            proc = subprocess.Popen(
                [sys.executable, "-m", "secondbrain.daemon.server"],
                stdout=logf,
                stderr=logf,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        # Give the daemon a moment to write its PID file
        for _ in range(20):
            time.sleep(0.1)
            if daemon_state.is_daemon_running():
                break
        if daemon_state.is_daemon_running():
            console.print(f"[green]✓[/green] daemon started in background "
                          f"(pid {daemon_state.read_pid()})")
            console.print(f"  logs: [cyan]{log_path}[/cyan]")
            console.print(f"  state: [cyan]{daemon_state.STATE_FILE}[/cyan]")
            console.print(f"  stop: [cyan]sb daemon stop[/cyan]")
        else:
            console.print(f"[red]✗[/red] daemon did not start. Check logs at {log_path}")
            raise click.Abort()
    else:
        console.print("[cyan]starting daemon in foreground — Ctrl+C to stop[/cyan]\n")
        sys.exit(run_forever())


@daemon.command(help="Stop the running daemon.")
def stop() -> None:
    from secondbrain.daemon import state as daemon_state

    if not daemon_state.is_daemon_running():
        console.print("[yellow]daemon not running[/yellow]")
        return
    pid = daemon_state.read_pid()
    if daemon_state.stop_daemon():
        console.print(f"[green]✓[/green] sent SIGTERM to pid {pid}")
        # Wait briefly for clean exit
        for _ in range(30):
            time.sleep(0.2)
            if not daemon_state.is_daemon_running():
                break
        if daemon_state.is_daemon_running():
            console.print("[yellow]⚠[/yellow] daemon still running after 6s — "
                          "may need `kill -9`")
        else:
            console.print("[green]✓[/green] stopped")
    else:
        console.print("[red]✗[/red] could not send signal")


@daemon.command(help="Show daemon status and registered jobs.")
def status() -> None:
    from secondbrain.daemon import state as daemon_state

    running = daemon_state.is_daemon_running()
    pid = daemon_state.read_pid()
    state = daemon_state.read_state()

    if running:
        console.print(f"[green]●[/green] running (pid {pid})")
    elif pid:
        console.print(f"[yellow]●[/yellow] stale pid file (pid {pid} not alive)")
    else:
        console.print("[dim]●[/dim] not running")

    if state:
        if state.get("started_at"):
            console.print(f"  started: [dim]{state['started_at']}[/dim]")
        if state.get("project_home"):
            console.print(f"  project: [cyan]{state['project_home']}[/cyan]")
        jobs = state.get("jobs") or []
        if jobs:
            console.print()
            t = Table(title="Registered jobs", show_header=True, header_style="bold")
            t.add_column("id", style="cyan")
            t.add_column("description")
            t.add_column("next run", style="dim")
            for j in jobs:
                t.add_row(j["id"], j["description"], str(j.get("next_run") or "—"))
            console.print(t)

    # Recent fire history (from workbench.db)
    try:
        ws = Workspace()
        if ws.db_path.exists():
            hist = daemon_state.fire_history(ws.open_db(), limit=5)
            if hist:
                console.print()
                t = Table(title="Recent fires", show_header=True, header_style="bold")
                t.add_column("job", style="cyan")
                t.add_column("fired at", style="dim")
                t.add_column("status")
                t.add_column("duration", justify="right")
                for h in hist:
                    status_disp = ("[green]ok[/green]" if h["status"] == "ok"
                                   else "[red]" + h["status"] + "[/red]")
                    dur = f"{h['duration_ms']}ms" if h.get("duration_ms") else "—"
                    t.add_row(h["job_id"], h["fired_at"][:19], status_disp, dur)
                console.print(t)
    except WorkspaceError:
        pass


@daemon.command(name="run-once", help="Fire one job immediately without scheduling it.")
@click.argument("job_id")
def run_once(job_id: str) -> None:
    try:
        from secondbrain.daemon.registry import JOBS, load_builtin_jobs
        from secondbrain.daemon import state as daemon_state
    except ImportError as e:
        console.print("[red]✗[/red] daemon deps not installed.")
        console.print("  Install with: [cyan]pip install 'secondbrain[daemon]'[/cyan]")
        console.print(f"  ({e})")
        raise click.Abort()

    load_builtin_jobs()
    job = JOBS.get(job_id)
    if not job:
        console.print(f"[red]✗[/red] no such job: {job_id}")
        console.print("\nAvailable:")
        for jid in JOBS:
            console.print(f"  • [cyan]{jid}[/cyan]")
        raise click.Abort()

    try:
        ws = Workspace()
    except WorkspaceError as e:
        console.print(f"[red]✗[/red] {e}")
        raise click.Abort()

    console.print(f"[cyan]running {job_id}...[/cyan]")
    start = time.monotonic()
    try:
        result = job.run(ws)
        duration_ms = int((time.monotonic() - start) * 1000)
        console.print(f"[green]✓[/green] {job_id} done in {duration_ms}ms")
        for k, v in result.items():
            console.print(f"  {k}: {v}")
        # Record to fire history
        if ws.db_path.exists():
            daemon_state.record_fire(
                ws.open_db(), job_id,
                status="ok", duration_ms=duration_ms, result=result,
            )
    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        console.print(f"[red]✗[/red] {job_id} failed in {duration_ms}ms")
        console.print(f"  {type(e).__name__}: {e}")
        if ws.db_path.exists():
            daemon_state.record_fire(
                ws.open_db(), job_id,
                status="error", duration_ms=duration_ms, error=str(e),
            )
        raise click.Abort()


@daemon.command(help="Show recent daemon fire history from the DB.")
@click.option("--job", help="Filter to one job id")
@click.option("--limit", type=int, default=20)
def logs(job: str | None, limit: int) -> None:
    from secondbrain.daemon import state as daemon_state

    try:
        ws = Workspace()
    except WorkspaceError as e:
        console.print(f"[red]✗[/red] {e}")
        raise click.Abort()

    if not ws.db_path.exists():
        console.print("[yellow]no workbench.db yet[/yellow]")
        return

    hist = daemon_state.fire_history(ws.open_db(), job_id=job, limit=limit)
    if not hist:
        console.print("[dim]no fire history yet[/dim]")
        return

    t = Table(show_header=True, header_style="bold")
    t.add_column("job", style="cyan")
    t.add_column("fired at", style="dim")
    t.add_column("status")
    t.add_column("ms", justify="right")
    t.add_column("result/error")
    for h in hist:
        status_disp = ("[green]ok[/green]" if h["status"] == "ok"
                       else "[red]" + h["status"] + "[/red]")
        dur = str(h.get("duration_ms") or "—")
        result_disp = ""
        if h.get("error"):
            result_disp = f"[red]{h['error'][:60]}[/red]"
        elif h.get("result_json"):
            import json
            try:
                r = json.loads(h["result_json"])
                result_disp = ", ".join(f"{k}={v}" for k, v in list(r.items())[:3])[:60]
            except Exception:
                result_disp = h["result_json"][:60]
        t.add_row(h["job_id"], h["fired_at"][:19], status_disp, dur, result_disp)
    console.print(t)
