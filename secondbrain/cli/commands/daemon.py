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


@daemon.command(help="Install a per-user launchd LaunchAgent (macOS) or systemd timer (Linux) "
                     "so jobs fire on schedule + survive sleep/wake without a long-running daemon. "
                     "Recommended over `sb daemon start --bg` on real machines.")
@click.option("--uninstall", is_flag=True, help="Remove instead of install.")
@click.option("--dry-run", is_flag=True, help="Show what would be written without writing it.")
def install(uninstall: bool, dry_run: bool) -> None:
    import platform
    sys_name = platform.system()
    if sys_name == "Darwin":
        _install_launchd(uninstall=uninstall, dry_run=dry_run)
    elif sys_name == "Linux":
        _install_systemd(uninstall=uninstall, dry_run=dry_run)
    else:
        console.print(f"[red]✗[/red] No installer for {sys_name} yet. "
                      "Use `sb daemon start --bg` and reschedule on boot manually.")
        raise click.Abort()


# ── launchd integration (macOS) ─────────────────────────────────────────


def _launchd_agents_dir() -> "Path":
    from pathlib import Path
    return Path.home() / "Library" / "LaunchAgents"


def _launchd_label(job_id: str) -> str:
    return f"com.secondbrain.{job_id}"


def _resolve_sb_binary() -> str:
    """Locate the absolute path to `sb` for the LaunchAgent.

    LaunchAgents run with a minimal PATH so a bare `sb` won't resolve.
    Prefer the pipx-installed binary, then a venv binary, then sys.argv[0].
    """
    import shutil
    sb = shutil.which("sb")
    if sb:
        return sb
    # Fallback: re-derive from the Python interpreter running this process
    return sys.executable + " -m secondbrain.cli"


def _job_cron_to_calendar_interval(schedule: dict) -> list[dict]:
    """Convert a Job.schedule dict into one or more launchd StartCalendarInterval entries.

    launchd's StartCalendarInterval is dict-of-int. To express "weekdays at 7:00 AM"
    we need 5 entries (Mon=1, Tue=2, Wed=3, Thu=4, Fri=5), each with the same hour/min.
    """
    cron = schedule.get("cron")
    if cron is None:
        return []

    # Parse either string ("45 16 * * mon-fri") or dict ({hour:7, minute:0, day_of_week:'mon-fri'})
    if isinstance(cron, str):
        # 5-field crontab: minute hour dom month dow
        parts = cron.split()
        if len(parts) != 5:
            raise ValueError(f"crontab must have 5 fields, got: {cron!r}")
        minute_s, hour_s, dom_s, month_s, dow_s = parts
        minute = int(minute_s)
        hour = int(hour_s)
        dow_spec = dow_s
    elif isinstance(cron, dict):
        minute = int(cron.get("minute", 0))
        hour = int(cron.get("hour", 0))
        dow_spec = str(cron.get("day_of_week", "*"))
    else:
        return []

    # Expand dow_spec ("mon-fri" or "*" or "0,3,5") into launchd Weekday ints
    weekdays = _parse_dow(dow_spec)
    return [{"Hour": hour, "Minute": minute, "Weekday": wd} for wd in weekdays]


def _parse_dow(spec: str) -> list[int]:
    """Convert cron day-of-week spec to launchd Weekday ints (Sun=0..Sat=6).

    Supports: '*', 'mon-fri', 'sat,sun', '1-5', '0,3,5', or a single token.
    """
    name_map = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
    spec = spec.strip().lower()
    if spec in ("*", "?"):
        return [0, 1, 2, 3, 4, 5, 6]

    result: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        if "-" in token:
            a, b = token.split("-", 1)
            a_int = name_map.get(a, int(a) if a.isdigit() else None)
            b_int = name_map.get(b, int(b) if b.isdigit() else None)
            if a_int is None or b_int is None:
                continue
            # Handle ranges including wraparound (rare but cron-legal)
            if a_int <= b_int:
                result.update(range(a_int, b_int + 1))
            else:
                result.update(list(range(a_int, 7)) + list(range(0, b_int + 1)))
        else:
            v = name_map.get(token, int(token) if token.isdigit() else None)
            if v is not None:
                result.add(v)
    return sorted(result)


def _make_launchd_plist(job_id: str, schedule: dict, sb_bin: str,
                       project_home: str, log_dir: str) -> str:
    """Render the launchd plist XML for one job."""
    label = _launchd_label(job_id)
    intervals = _job_cron_to_calendar_interval(schedule)
    if not intervals:
        # Fall back to interval-seconds for jobs without cron
        interval_s = schedule.get("interval_seconds")
        if interval_s:
            sci_xml = f"  <key>StartInterval</key>\n  <integer>{int(interval_s)}</integer>"
        else:
            sci_xml = "  <key>RunAtLoad</key>\n  <true/>"
    else:
        # StartCalendarInterval is an array of dicts when there are multiple slots
        items_xml = "\n".join([
            "    <dict>\n"
            + "\n".join(f"      <key>{k}</key>\n      <integer>{v}</integer>" for k, v in i.items())
            + "\n    </dict>"
            for i in intervals
        ])
        sci_xml = f"  <key>StartCalendarInterval</key>\n  <array>\n{items_xml}\n  </array>"

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sb_bin}</string>
    <string>daemon</string>
    <string>run-once</string>
    <string>{job_id}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SECONDBRAIN_HOME</key>
    <string>{project_home}</string>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
  </dict>
{sci_xml}
  <key>StandardOutPath</key>
  <string>{log_dir}/{job_id}.out.log</string>
  <key>StandardErrorPath</key>
  <string>{log_dir}/{job_id}.err.log</string>
  <key>WorkingDirectory</key>
  <string>{project_home}</string>
</dict>
</plist>
"""


def _install_launchd(*, uninstall: bool, dry_run: bool) -> None:
    """Install one LaunchAgent per registered job. macOS only."""
    from pathlib import Path
    from secondbrain.daemon.registry import JOBS, load_builtin_jobs
    from secondbrain.daemon import state as daemon_state

    try:
        ws = Workspace()
    except WorkspaceError as e:
        console.print(f"[red]✗[/red] {e}")
        raise click.Abort()

    load_builtin_jobs()
    if not JOBS:
        console.print("[red]✗[/red] no jobs registered")
        raise click.Abort()

    agents_dir = _launchd_agents_dir()
    log_dir = daemon_state.CONFIG_DIR / "logs"
    sb_bin = _resolve_sb_binary()

    if uninstall:
        if dry_run:
            console.print("[cyan]dry-run: would unload + remove[/cyan]")
        for job_id in JOBS:
            label = _launchd_label(job_id)
            plist = agents_dir / f"{label}.plist"
            if plist.exists():
                if not dry_run:
                    subprocess.run(["launchctl", "unload", str(plist)],
                                   capture_output=True, text=True)
                    plist.unlink()
                console.print(f"  [yellow]−[/yellow] removed {label}")
            else:
                console.print(f"  [dim]·[/dim] {label} not installed, skipping")
        if not dry_run:
            console.print("\n[green]✓[/green] uninstalled. The `sb daemon` process can still be used "
                          "manually via `sb daemon start --bg` or `sb daemon run-once`.")
        return

    # Install
    agents_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    if dry_run:
        console.print(f"[cyan]dry-run: would write {len(JOBS)} plists to {agents_dir}[/cyan]\n")
    else:
        console.print(f"\nInstalling {len(JOBS)} LaunchAgent(s) into [cyan]{agents_dir}[/cyan]\n")

    for job_id, job in JOBS.items():
        label = _launchd_label(job_id)
        plist_path = agents_dir / f"{label}.plist"
        try:
            xml = _make_launchd_plist(
                job_id, job.schedule, sb_bin,
                project_home=str(ws.project_home),
                log_dir=str(log_dir),
            )
        except ValueError as e:
            console.print(f"  [red]✗[/red] {job_id}: {e}")
            continue

        if dry_run:
            console.print(f"[cyan]── {plist_path} ──[/cyan]")
            console.print(xml)
            continue

        # If already loaded, unload first (so updates take effect)
        if plist_path.exists():
            subprocess.run(["launchctl", "unload", str(plist_path)],
                           capture_output=True, text=True)

        plist_path.write_text(xml, encoding="utf-8")
        # Set permissions to user-only (launchd is strict)
        plist_path.chmod(0o644)

        result = subprocess.run(["launchctl", "load", str(plist_path)],
                                capture_output=True, text=True)
        if result.returncode == 0:
            console.print(f"  [green]✓[/green] {label}")
            console.print(f"    [dim]→ {plist_path}[/dim]")
            # Show next scheduled time if launchd will tell us
            list_out = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True, text=True,
            )
            if list_out.returncode == 0 and "PID" in list_out.stdout:
                # extract last exit + run state
                pass
        else:
            console.print(f"  [red]✗[/red] {label}: launchctl load failed")
            console.print(f"    [dim]{result.stderr.strip()}[/dim]")

    if not dry_run:
        console.print(f"\n[green]✓[/green] installed. Jobs will fire on schedule + survive sleep/wake.")
        console.print(f"\nVerify with: [cyan]launchctl list | grep com.secondbrain[/cyan]")
        console.print(f"Per-job logs: [cyan]{log_dir}/<job_id>.out.log[/cyan]")
        console.print(f"Uninstall:    [cyan]sb daemon install --uninstall[/cyan]")


# ── systemd integration (Linux) ─────────────────────────────────────────


def _install_systemd(*, uninstall: bool, dry_run: bool) -> None:
    """Install a systemd user timer per job. Linux only.

    Each job gets two files in ~/.config/systemd/user/:
      secondbrain-<job_id>.service   — what to run
      secondbrain-<job_id>.timer     — when to run
    """
    console.print(
        "[yellow]systemd integration is not yet implemented[/yellow]\n"
        "  For now, on Linux use a cron entry pointing at `sb daemon run-once <job_id>`.\n"
        "  PRs welcome at https://github.com/levan1225/second-brain"
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
