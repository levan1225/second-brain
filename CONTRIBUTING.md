# Contributing to secondbrain

Thanks for thinking about contributing. This is alpha software shipped from a single user's setup, so PRs are especially welcome where you've found rough edges in real use.

## Development setup

```bash
git clone https://github.com/vnguyen8/second-brain
cd second-brain
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,mcp,daemon,web]'
.venv/bin/pytest -q
```

You should see ~115 tests pass in under 3 seconds.

To run the CLI from your dev checkout: `.venv/bin/sb --help`. The `pyproject.toml` registers `sb` as the entry point and `pip install -e .` makes it reload your edits live.

## Code layout

```
secondbrain/
├── core/                Shared library — pure Python, no LLM, no HTTP
│   ├── workspace.py     Workspace class, project home resolution
│   ├── work_items.py    Canonical store (commitments + promises)
│   └── wiki.py          Markdown + frontmatter helpers
├── cli/                 Plane 1 — `sb` user-facing commands
├── mcp/                 Plane 4 — LLM-facing MCP tool server
├── daemon/              Plane 2 — background scheduler + jobs
├── web/                 Plane 3 — localhost dashboard
└── connectors/          Plugin framework (Slack/Outlook are NOT shipped —
                         use Claude Desktop's; this layer is for things
                         Claude Desktop can't do, like Jira / internal tools)
```

**Hard rule:** `core/` imports nothing from CLI, MCP, web, or daemon. Everything else imports from `core/`. This is what lets the four planes ship independently.

## How to contribute

### Adding a new daemon job

Daemon jobs run on a schedule and write to `output/daemon/`. They never deliver to external systems directly — that's done by asking Claude in a Cowork session (which uses Claude Desktop's connectors).

Create `secondbrain/daemon/jobs/your_job.py`:

```python
from secondbrain.core.workspace import Workspace
from secondbrain.daemon.registry import Job, register_job

def run(ws: Workspace) -> dict:
    # Read from ws.conn (SQLite), wiki, or files
    # Write your output to ws.project_home / "output" / "daemon" / ...
    # Return a metadata dict (logged to schema/log.md by the daemon)
    return {"items_processed": 42}

register_job(Job(
    id="your_job",
    description="What it does, in one line",
    schedule={"cron": "30 8 * * mon-fri"},  # or {"interval_seconds": 300}
    run=run,
    enabled_default=True,
))
```

Then add `from secondbrain.daemon.jobs import your_job` to `daemon/registry.py`'s `load_builtin_jobs()`. The daemon picks it up on next restart.

### Adding a new MCP tool

```python
# In secondbrain/mcp/server.py:

TOOL_DEFS.append(Tool(
    name="your_tool",
    description="Concrete what + when to use",
    inputSchema={...},
))

def _handle_your_tool(args: dict) -> Any:
    ws = Workspace()
    # do stuff
    return {"result": ...}

HANDLERS["your_tool"] = _handle_your_tool
```

Add a test in `tests/test_mcp_tools.py`. The pattern: call `_handle_your_tool({...})` directly — skip the JSON-RPC layer, since we trust the SDK.

### Adding a new CLI command

```python
# secondbrain/cli/commands/your_cmd.py
import click
from rich.console import Console
from secondbrain.core.workspace import Workspace, WorkspaceError

console = Console()

@click.command(help="One-line description.")
@click.option("--project-home", type=click.Path(file_okay=False),
              help="Override project home (default: from config or env)")
def your_cmd(project_home: str | None) -> None:
    ...
```

Wire it into `secondbrain/cli/__main__.py`:

```python
from secondbrain.cli.commands import your_cmd as cmd_your
main.add_command(cmd_your.your_cmd)
```

Click tests use `from click.testing import CliRunner`. See `tests/test_cli_items.py` for the pattern.

### Adding a third-party connector

You publish a separate PyPI package — secondbrain finds it via entry-points. Your `pyproject.toml`:

```toml
[project]
name = "secondbrain-jira-connector"
dependencies = ["second-brain", "jira>=3.0"]

[project.entry-points."secondbrain.connectors"]
jira = "secondbrain_jira:JiraConnector"
```

Your connector class implements `secondbrain.connectors.Connector` (see `secondbrain/connectors/base.py`). Once published to PyPI and the user runs `pip install secondbrain-jira-connector`, your connector auto-registers — `sb connect list` shows it, `sb connect jira` walks them through auth.

The contract:

```python
from secondbrain.connectors import Connector, ConnectorStatus, SendAction, SendResult, Signal

class JiraConnector(Connector):
    name = "jira"

    def authenticate(self) -> ConnectorStatus: ...
    def status(self) -> ConnectorStatus: ...
    def scan(self, since=None) -> list[Signal]: ...
    def send(self, action: SendAction) -> SendResult: ...
    def disconnect(self) -> None: ...
```

Store credentials via `secondbrain.connectors.secrets` (wraps OS keychain on each platform).

## Style + testing

- **No `# type: ignore` unless commented why.**
- **`from __future__ import annotations`** at the top of every module.
- **Type hints required** on public functions; encouraged on private.
- **Test files are `tests/test_*.py`**, pytest auto-discovers. CliRunner for CLI tests, direct handler calls for MCP tests, monkeypatch for env-var-based isolation.
- **No emojis in code.** OK in user-facing output (Rich rendering), not in source.
- **Lines ≤ 100 chars** (ruff configured).

## Filing issues

When in doubt, file an issue with:

- What you ran (`sb status` etc.)
- What you expected
- What happened
- Output of `sb info` so we see your workspace state
- Output of `sb --version`

Don't paste your wiki content unless it's directly relevant — it's local-first for a reason.

## License

MIT. By contributing, you agree your contributions are under the same.
