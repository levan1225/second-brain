"""secondbrain.daemon — the background scheduler plane.

This is what makes the system feel proactive. While the CLI is reactive
(user asks → we answer) and MCP is reactive (LLM asks → we answer), the
daemon fires on its own schedule:

  • Morning briefing       (7am local)
  • Pre-meeting whisper    (25 min before calendar events) — Phase 3.5
  • End-of-day check       (4:45pm local) — Phase 3.6
  • Aging commitment chase (daily) — Phase 3.7
  • Watchlist alerts       (on signal) — Phase 3.8

The daemon owns no critical state of its own — it reads from the same
SQLite + wiki + config the CLI and MCP server use. State unique to the
daemon (last-fire times, dedup tokens) lives in a `daemon_jobs` table.
"""

from .registry import JOBS, register_job

__all__ = ["JOBS", "register_job"]
