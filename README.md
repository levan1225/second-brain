# second-brain

**Your local-first executive operating system.** Tracks programs, people, and commitments — so you walk into every meeting prepared and never lose a promise.

Works with Claude, Cursor, or any LLM that speaks MCP. Your data stays on your machine.

```bash
pip install 'second-brain[all]'
sb init
sb status
```

That's it. No SaaS. No login. No tokens to paste.

---

## What it gives you

```
$ sb status

                                  Overdue (3)
┏━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ id   ┃ title                                          ┃ owner    ┃ due        ┃
┡━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━┩
│ 16   │ Data Platform explicit dependency callouts...  │ Vamsee    │ 2026-05-12 │
│ 15   │ Identity lead-vs-support split: which 2 of 5   │ Gagan     │ 2026-05-14 │
│ 17   │ Complete Dragonboat UAT Exit Survey            │ Van       │ 2026-05-15 │
└──────┴────────────────────────────────────────────────┴──────────┴────────────┘
                                Due this week (6)
... (etc)
```

You also get:

- **A web dashboard** at `localhost:8765` — no terminal needed once it's running
- **A morning briefing file** auto-generated at 7am each weekday, surfacing what needs you
- **MCP tools** that Claude Desktop / Cursor / Continue can call: `query_work_items`, `prepare_meeting`, `get_person`, `read_wiki_page`, etc.
- **A canonical SQLite store** + **an Obsidian-compatible markdown wiki** — same data, two views, cross-referenced

---

## How it differs from yet-another-notes-app

| Feature | second-brain | Notion / Obsidian / Apple Notes |
|---|---|---|
| Where data lives | Your machine, single SQLite file + markdown wiki | Their cloud (Notion/Apple) or your machine (Obsidian) |
| LLM access | Native via MCP — Claude can query, draft in your voice, post to Slack | Generic — no structured tool surface |
| Commitments | First-class — overdue tracking, owner resolution, dedup, history | Plain bullets that drift |
| People as entities | Cross-referenced with `canonical_id`, voice profiles, trust tiers | Manual maintenance |
| Proactive daily brief | Yes — daemon writes it at 7am | No |
| Cost | Free / open-source | Subscription |

---

## Install

### Prerequisite

Python 3.11 or later. Check with `python3 --version`.

### One command

```bash
pip install 'second-brain[all]'
```

That gets you the CLI, the local web dashboard, the MCP server, and the background daemon.

Or pick & choose:

```bash
pip install second-brain              # CLI only
pip install 'second-brain[mcp]'       # + MCP server for LLM tools
pip install 'second-brain[web]'       # + localhost web dashboard
pip install 'second-brain[daemon]'    # + background scheduler
pip install 'second-brain[all]'       # everything
```

### First run

```bash
sb init                       # creates ~/Documents/second-brain/<your-name>/
sb status                     # nothing yet, but the workspace is ready
sb web                        # opens localhost dashboard in your browser
```

Then start dropping data into `~/Documents/second-brain/<slug>/sources/transcripts/` — meeting notes, transcripts, anything text — and run:

```bash
sb scan
```

Commitments get extracted, owners get resolved, the dashboard fills up.

---

## Connecting an LLM

second-brain ships an MCP server. Point your LLM tool at it once and Claude/Cursor/Continue can query your workspace.

### Claude Desktop

Add this to `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac):

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "sb",
      "args": ["mcp"]
    }
  }
}
```

Restart Claude Desktop. In a new session you can now say:

> What are my overdue commitments?
> Prep me for my 1:1 with Tom.
> What did Sarah say about the migration plan?

Claude calls the `query_work_items`, `prepare_meeting`, `evidence_search` tools directly against your local data.

### Cursor / Continue / others

Any MCP-compatible client works — the protocol is the same. Configure with `command: sb, args: ["mcp"]`.

### Slack / Outlook

**Don't install secondbrain-side Slack or Outlook connectors.** Use Claude Desktop's built-in ones (`Settings → Connectors → Add Slack / Outlook`). Then a session can do:

> Read today's morning briefing at `~/Documents/second-brain/.../output/daemon/briefings/...` and post the summary to my Slack DM.

Claude reads via second-brain's `read_wiki_page`, then sends via its own Slack connector. No tokens, no app registration, no IT approval needed.

---

## Daily flow (the actual value)

**Morning (7am):**
The daemon (if running) writes a fresh briefing to `output/daemon/briefings/{date}-morning.md`. Or run `sb daemon run-once morning_brief` on demand.

**Before each meeting:**
```bash
sb prep tom-chen            # one-shot prep brief for a person
```
Or ask Claude: *"Prep me for the 2pm Acme SteerCo — who's attending and what do they have open against me?"*

**During the day:**
```bash
sb status                    # what needs you, ranked
sb status --overdue          # only the things actually slipping
sb status --owner Sarah      # filter by person
```

**End of week:**
Drop meeting transcripts into `sources/transcripts/`, run `sb scan`, the canonical store updates.

---

## CLI reference

```
sb init                          Create a new project home
sb info                          Workspace summary
sb status                        What needs your attention (overdue / today / week)
sb scan                          Ingest data from sources/ into the canonical store
sb people [slug]                 List people, or show one with their open items
sb mcp                           Run the MCP stdio server (for Claude Desktop etc.)
sb web                           Run the localhost web dashboard
sb daemon start --bg             Start the background scheduler
sb daemon stop                   Stop it
sb daemon status                 Show what's running + next-fire times
sb daemon run-once <job>         Fire a job immediately (e.g. morning_brief)
sb daemon logs                   Recent fire history
sb connect                       List third-party connectors (Jira, etc.)
sb migrate-from-v2 --project-home <path>
                                 Adopt an existing v2 (Second Brain + PCE) project home
```

---

## Architecture (one paragraph)

second-brain is **four cooperating planes** sharing one core library:

- **CLI** (`sb`) — power-user surface; works without any LLM
- **MCP server** (`sb mcp`) — exposes tools to your LLM client
- **Daemon** (`sb daemon`) — background scheduler for proactive jobs
- **Web UI** (`sb web`) — localhost browser dashboard

All four read/write the same SQLite `workbench.db` + markdown wiki + YAML canonical config. Everything is local — your data never leaves your machine unless you explicitly send it somewhere (e.g. asking Claude to post to Slack).

---

## What's NOT in v3.0 (yet)

- **Auto Slack ingestion** — the daemon writes briefings to disk; you ask Claude to deliver them. Future versions may directly invoke Claude Desktop's MCP servers for delivery.
- **Outlook calendar polling** — same story. Ask Claude to prep you in a session; it pulls from its own calendar connector.
- **Pre-meeting whisper** — was built then removed in v3.0 because it required Outlook OAuth that adds setup friction. Easier to ask Claude when you need it.
- **Mobile clients** — local-first means no app store distribution. The web UI works on mobile via SSH tunnel or Tailscale.
- **Multi-user / team sharing** — single-user by design. Each person runs their own.

If any of those are blocking for you, open an issue.

---

## Contributing

This is alpha software shipped from a single developer's setup. Bugs are likely. PRs welcome — especially for:

- **Third-party connectors** for systems Claude Desktop doesn't cover (Jira, Smartsheet, Linear, ...). They register via PyPI entry-points; no second-brain changes needed.
- **New daemon jobs** (`secondbrain/daemon/jobs/{name}.py`) — they self-register via `register_job()`.
- **Web UI polish** — templates are HTMX + Tailwind via CDN, no build step.

Run the test suite with `pytest`. There's no CI yet.

---

## License

MIT. See [LICENSE](LICENSE).
