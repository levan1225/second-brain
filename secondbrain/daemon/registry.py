"""Job registry — every daemon job declares itself here.

A job is a dataclass with: id, schedule (cron-like), description, and a run()
function that takes a Workspace and returns a result dict logged to daemon_jobs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from secondbrain.core.workspace import Workspace


@dataclass
class Job:
    id: str  # stable identifier, e.g. "morning_brief"
    description: str
    schedule: dict[str, Any]  # apscheduler kwargs: {"cron": {"hour": 7, "minute": 0}}
    run: Callable[[Workspace], dict[str, Any]]
    enabled_default: bool = True


JOBS: dict[str, Job] = {}


def register_job(job: Job) -> None:
    """Called by each job module at import time."""
    if job.id in JOBS:
        raise ValueError(f"duplicate job id: {job.id}")
    JOBS[job.id] = job


def load_builtin_jobs() -> None:
    """Import every built-in job module so they self-register."""
    # Imported for side-effect (register_job calls)
    from secondbrain.daemon.jobs import morning_brief       # noqa: F401
    from secondbrain.daemon.jobs import aging_commitments   # noqa: F401
