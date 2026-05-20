"""`sb scan` — ingest from sources/ folder (file drops) and live connectors.

This is the Phase-1 minimal scan: file-drop only. It reads files from
`sources/transcripts/` and `sources/meeting-notes/` and `sources/docs/`,
runs the privacy filter (placeholder), and upserts any commitments it finds
into the canonical store with `wiki_path` cross-references.

Live MCP connectors (Slack, Zoom, Drive, Outlook) come in Phase 4.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from secondbrain.core import work_items
from secondbrain.core.workspace import Workspace, WorkspaceError

console = Console()


# Crude commitment-detection regex. Real implementation would use the
# extraction-dimensions module + LLM. For Phase 1 this is enough to prove
# the ingestion path works end-to-end.
_COMMITMENT_PATTERNS = [
    # "I'll send the X by Friday" / "we'll have X ready next week"
    re.compile(
        r"(?:^|\b)(?:I['’]?ll|we['’]?ll|I will|we will|I can|let me)\s+(.{8,140}?)"
        r"(?:by\s+(\S+\s+\S+|\S+))",
        re.IGNORECASE,
    ),
    # "X owes Y" / "X to deliver Y"
    re.compile(
        r"\b(\w+)\s+(?:to\s+(?:deliver|send|share|present|provide)|owes)\s+(.{8,140}?)(?:[.!?\n])",
        re.IGNORECASE,
    ),
]

# Markdown bullet looking like a commitment: "- [ ] X by 2026-05-25"
_BULLET_COMMITMENT = re.compile(
    r"^[\s]*[-*]\s*\[\s\]\s*(.{8,200}?)(?:$|\s+\(due:\s*(\d{4}-\d{2}-\d{2})\))",
    re.IGNORECASE | re.MULTILINE,
)


def _extract_commitments(text: str) -> list[dict[str, str]]:
    """Phase-1 heuristic commitment extraction. Returns list of {title, due_date}.

    This is intentionally simple. A future :scan skill will replace this with
    an LLM-driven extractor that produces all 13 extraction dimensions.
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    # Pattern 1: TODO-style bullets with optional due date
    for m in _BULLET_COMMITMENT.finditer(text):
        title = m.group(1).strip()
        due = m.group(2) or ""
        key = title.lower()[:80]
        if key not in seen:
            seen.add(key)
            out.append({"title": title, "due_date": due})

    # Pattern 2: "I'll X by Friday"
    for pat in _COMMITMENT_PATTERNS:
        for m in pat.finditer(text):
            title = m.group(1).strip().rstrip(".,;:")
            if len(title) < 8:
                continue
            key = title.lower()[:80]
            if key in seen:
                continue
            seen.add(key)
            out.append({"title": title, "due_date": ""})

    return out


def _iter_source_files(sources_root: Path) -> Iterable[Path]:
    """Yield every text-ish file under sources/, excluding raw/ snapshots."""
    if not sources_root.exists():
        return
    for sub in ("transcripts", "meeting-notes", "docs", "slack"):
        sub_path = sources_root / sub
        if not sub_path.exists():
            continue
        for f in sub_path.rglob("*"):
            if f.is_file() and f.suffix.lower() in (".md", ".txt", ".vtt"):
                yield f


def _file_source_uri(path: Path, sources_root: Path) -> str:
    """Generate a stable source URI for a file path."""
    rel = path.relative_to(sources_root)
    return f"file://{rel}"


@click.command(help="Ingest data from sources/ (file drops) into the canonical store.")
@click.option(
    "--project-home",
    type=click.Path(file_okay=False),
    help="Override project home (default: from config or env)",
)
@click.option(
    "--files-only",
    is_flag=True,
    default=True,
    help="Phase 1: only file-drop ingestion (default). Live connectors come in Phase 4.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be ingested, but don't write to the DB",
)
def scan(project_home: str | None, files_only: bool, dry_run: bool) -> None:
    try:
        ws = Workspace(project_home)
    except WorkspaceError as e:
        console.print(f"[red]✗[/red] {e}")
        raise click.Abort()

    sources_root = ws.project_home / "sources"
    if not sources_root.exists():
        console.print(
            f"[yellow]No sources/ folder at[/yellow] {sources_root}. "
            "Drop files there and re-run."
        )
        return

    files = list(_iter_source_files(sources_root))
    if not files:
        console.print(
            f"[dim]No new files under sources/. Looked in: "
            f"transcripts/, meeting-notes/, docs/, slack/[/dim]"
        )
        return

    console.print(
        f"[bold]Scanning[/bold] {len(files)} file(s) under "
        f"[cyan]{sources_root.relative_to(ws.project_home)}/[/cyan]"
        + (" [dim](dry-run)[/dim]" if dry_run else "")
    )

    total_extracted = 0
    total_created = 0
    total_updated = 0
    today = date.today().isoformat()
    log_lines: list[str] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("scanning files...", total=len(files))

        for f in files:
            progress.update(task, description=f"reading {f.name}")
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                log_lines.append(f"[yellow]⚠[/yellow] {f.name}: {e}")
                progress.advance(task)
                continue

            commitments = _extract_commitments(text)
            total_extracted += len(commitments)
            source_uri = _file_source_uri(f, sources_root)

            for c in commitments:
                if dry_run:
                    log_lines.append(
                        f"  [dim]→[/dim] {c['title'][:70]}"
                        + (f" [dim](due {c['due_date']})[/dim]" if c["due_date"] else "")
                    )
                    continue
                result = work_items.upsert(
                    ws.open_db(),
                    item_type="action",
                    title=c["title"],
                    due_date=c["due_date"],
                    source=source_uri,
                    wiki_path="wiki/context/commitments.md",
                )
                if result["created"]:
                    total_created += 1
                else:
                    total_updated += 1

            progress.advance(task)

    # Append to a simple scan log
    if not dry_run:
        log_path = ws.project_home / "logs" / "scan.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with log_path.open("a", encoding="utf-8") as logf:
            logf.write(
                f"[{ts}] scanned {len(files)} files, "
                f"extracted {total_extracted}, "
                f"created {total_created}, updated {total_updated}\n"
            )

    # Summary
    console.print()
    if dry_run:
        console.print(f"[bold]Dry-run summary:[/bold]")
        console.print(f"  files scanned:     {len(files)}")
        console.print(f"  commitments found: {total_extracted}")
        if log_lines:
            console.print("\nWould upsert:")
            for line in log_lines[:20]:
                console.print(line)
            if len(log_lines) > 20:
                console.print(f"  [dim]... +{len(log_lines)-20} more[/dim]")
    else:
        console.print(f"[bold]Scan complete:[/bold]")
        console.print(f"  files scanned:    {len(files)}")
        console.print(f"  commitments seen: {total_extracted}")
        console.print(f"  [green]created:          {total_created}[/green]")
        console.print(f"  [cyan]updated:          {total_updated}[/cyan]")
        if log_lines:
            console.print()
            for line in log_lines:
                console.print(line)
        console.print(f"\n[dim]Log: {log_path}[/dim]")
