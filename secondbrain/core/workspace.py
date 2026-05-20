"""Workspace — resolves project home, opens DB, dispatches to subsystems.

The Workspace is the v3 equivalent of v2's AnalysisWorkbench + canonical_sync
helpers, but unified in one place and without the venv-relative path tangles.

Resolution order for project_home (matches v2's _resolve_project_home):
    1. Explicit --project-home / project_home arg
    2. SECONDBRAIN_HOME env var
    3. TPM_PROJECT_HOME env var (v2 backward compat)
    4. ~/.config/secondbrain/config.yaml `project_home` field
    5. error
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import yaml


CONFIG_PATH = Path.home() / ".config" / "secondbrain" / "config.yaml"


class WorkspaceError(RuntimeError):
    """Raised when the workspace can't be resolved or opened."""


class Workspace:
    """A single user's secondbrain project home.

    Holds:
        project_home  — absolute Path to the project root
        db_path       — Path to state/workbench.db
        wiki_root     — Path to wiki/
        config_root   — Path to config/

    Lazily opens the SQLite connection on first .conn access.
    """

    def __init__(self, project_home: str | Path | None = None):
        self.project_home = self._resolve(project_home)
        self._conn: sqlite3.Connection | None = None

    @staticmethod
    def _resolve(explicit: str | Path | None) -> Path:
        candidate: str | None = None
        if explicit:
            candidate = str(explicit)
        elif os.environ.get("SECONDBRAIN_HOME"):
            candidate = os.environ["SECONDBRAIN_HOME"]
        elif os.environ.get("TPM_PROJECT_HOME"):
            candidate = os.environ["TPM_PROJECT_HOME"]
        elif CONFIG_PATH.exists():
            try:
                cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
                candidate = cfg.get("project_home")
            except Exception:
                pass

        if not candidate:
            raise WorkspaceError(
                "Cannot resolve project home.\n"
                "  Either pass --project-home, set SECONDBRAIN_HOME, "
                "or run `sb init` to create one."
            )

        p = Path(candidate).expanduser().resolve()
        if not p.exists():
            raise WorkspaceError(f"Project home does not exist: {p}")
        if not p.is_dir():
            raise WorkspaceError(f"Project home is not a directory: {p}")
        return p

    # ── Path accessors ─────────────────────────────────────────────────

    @property
    def db_path(self) -> Path:
        return self.project_home / "state" / "workbench.db"

    @property
    def wiki_root(self) -> Path:
        return self.project_home / "wiki"

    @property
    def config_root(self) -> Path:
        return self.project_home / "config"

    @property
    def conn(self) -> sqlite3.Connection:
        """Lazy SQLite connection (read-only-ish — errors if DB missing).

        Use this when you expect the DB to already exist (e.g. `sb status`).
        For writes that should create the DB on first use, call `open_db()`.
        """
        if self._conn is None:
            if not self.db_path.exists():
                raise WorkspaceError(
                    f"workbench.db not found at {self.db_path}.\n"
                    "  Run `sb scan` to create it, or check your project home."
                )
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def open_db(self) -> sqlite3.Connection:
        """Open (or create) the SQLite database. Use for writes that should
        bootstrap the DB on first scan.
        """
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ── Summary helpers (used by `sb status`, `sb info`) ───────────────

    def info(self) -> dict[str, Any]:
        """Return a flat dict of workspace facts for display."""
        out: dict[str, Any] = {
            "project_home": str(self.project_home),
            "db_exists": self.db_path.exists(),
            "wiki_exists": self.wiki_root.exists(),
        }

        # Wiki page counts by category
        if self.wiki_root.exists():
            for category in ("projects", "people", "concepts", "ideas", "patterns", "context"):
                subdir = self.wiki_root / category
                if subdir.exists():
                    out[f"wiki_{category}"] = len(list(subdir.glob("*.md")))
                else:
                    out[f"wiki_{category}"] = 0

        # Canonical row counts
        if self.db_path.exists():
            try:
                rows = self.conn.execute(
                    "SELECT item_type, status, COUNT(*) as n "
                    "FROM work_items GROUP BY item_type, status"
                ).fetchall()
                out["work_items"] = [dict(r) for r in rows]
                out["work_items_total"] = sum(r["n"] for r in rows)
            except sqlite3.OperationalError:
                out["work_items_total"] = 0
                out["work_items"] = []

        return out
