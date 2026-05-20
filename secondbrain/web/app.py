"""FastAPI app — localhost dashboard for secondbrain.

Routes:
  GET  /                    — dashboard (overdue + due-today + recent briefings)
  GET  /people              — people list
  GET  /people/{slug}       — single person + their open commitments
  GET  /commitments         — full work_items table, filterable
  GET  /briefings           — recent daemon outputs
  GET  /briefings/{name}    — one briefing's rendered content
  GET  /connectors          — connector status (read-only view)
  GET  /daemon              — daemon status + fire history
  GET  /healthz             — health check JSON

All routes return HTML by default. Add ?format=json to any route to get JSON.
"""

from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from secondbrain import __version__
from secondbrain.core import work_items
from secondbrain.core.wiki import list_pages, read_page
from secondbrain.core.workspace import Workspace, WorkspaceError

_HERE = Path(__file__).parent
# cache_size=0 disables the LRU cache. Some Jinja2 versions try to use the
# (loader, context) tuple as a dict key, which fails when the context dict
# contains non-hashable values (e.g. request objects). Disabling cache is
# fine for our scale — templates are small + few.
templates = Jinja2Templates(directory=str(_HERE / "templates"))
templates.env.cache = {}  # plain dict instead of LRUCache; we have <20 templates


def create_app() -> FastAPI:
    app = FastAPI(
        title="secondbrain",
        description="Local-first executive operating system",
        version=__version__,
    )
    # Static dir is optional (may not ship if empty)
    static_dir = _HERE / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ── helpers ────────────────────────────────────────────────────────

    def _ws() -> Workspace:
        try:
            return Workspace()
        except WorkspaceError as e:
            raise HTTPException(status_code=503, detail=str(e))

    def _want_json(request: Request) -> bool:
        if request.query_params.get("format") == "json":
            return True
        accept = request.headers.get("accept", "")
        return "application/json" in accept and "text/html" not in accept

    def _bucket_items(rows: list[dict]) -> dict[str, list[dict]]:
        # Use local date — what the user thinks "today" means
        today_date = datetime.now().date()
        today = today_date.isoformat()
        week_cutoff = (today_date + timedelta(days=7)).isoformat()
        buckets = {"overdue": [], "due_today": [], "due_this_week": [], "later": []}
        for r in rows:
            due = r.get("due_date") or ""
            if due and due < today:
                buckets["overdue"].append(r)
            elif due == today:
                buckets["due_today"].append(r)
            elif due and today < due <= week_cutoff:
                buckets["due_this_week"].append(r)
            else:
                buckets["later"].append(r)
        return buckets

    # ── routes ─────────────────────────────────────────────────────────

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> Any:
        ws = _ws()
        info = ws.info()
        rows = []
        if ws.db_path.exists():
            try:
                rows = work_items.query(ws.open_db(), item_type="action", status="open", limit=200)
            except Exception:
                rows = []
        buckets = _bucket_items(rows)

        # Recent briefings
        briefing_dir = ws.project_home / "output" / "daemon" / "briefings"
        recent_briefings = []
        if briefing_dir.exists():
            files = sorted(briefing_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            for f in files[:5]:
                recent_briefings.append({
                    "name": f.name,
                    "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "size_kb": f.stat().st_size / 1024,
                })

        if _want_json(request):
            return JSONResponse({
                "workspace": info,
                "buckets": {k: len(v) for k, v in buckets.items()},
                "recent_briefings": recent_briefings,
            })
        return templates.TemplateResponse(request, "dashboard.html", {
            "request": request,
            "info": info,
            "buckets": buckets,
            "recent_briefings": recent_briefings,
            "version": __version__,
        })

    @app.get("/people", response_class=HTMLResponse)
    def people_list(request: Request) -> Any:
        ws = _ws()
        pages = list_pages(ws.wiki_root, "people")
        people = []
        # Get open-item counts in one batch
        commitment_counts: dict[str, int] = {}
        if ws.db_path.exists():
            try:
                conn = ws.open_db()
                rows = work_items.query(conn, item_type="action", status="open", limit=500)
                for r in rows:
                    owner = (r.get("owner") or "").strip()
                    if owner:
                        commitment_counts[owner.lower()] = commitment_counts.get(owner.lower(), 0) + 1
            except Exception:
                pass

        for p in pages:
            fm = p.frontmatter
            name = fm.get("title") or p.slug
            people.append({
                "slug": p.slug,
                "name": name,
                "role": fm.get("role"),
                "relationship": fm.get("relationship"),
                "trust_tier": fm.get("trust_tier"),
                "last_updated": str(fm.get("last_updated") or ""),
                "open_count": commitment_counts.get(name.lower(), 0),
            })

        if _want_json(request):
            return JSONResponse({"count": len(people), "people": people})
        return templates.TemplateResponse(request, "people.html", {
            "request": request,
            "people": people,
            "version": __version__,
        })

    @app.get("/people/{slug}", response_class=HTMLResponse)
    def person_detail(request: Request, slug: str) -> Any:
        ws = _ws()
        page = read_page(ws.wiki_root, f"people/{slug}.md")
        if not page.frontmatter and not page.body.strip():
            raise HTTPException(status_code=404, detail=f"no person page: {slug}")
        name = page.frontmatter.get("title") or slug
        open_items = []
        if ws.db_path.exists():
            try:
                open_items = work_items.query(ws.open_db(), item_type="action", status="open", owner=name, limit=50)
            except Exception:
                pass

        if _want_json(request):
            return JSONResponse({
                "slug": slug,
                "frontmatter": _safe_for_json(page.frontmatter),
                "body": page.body,
                "open_items": open_items,
            })
        return templates.TemplateResponse(request, "person.html", {
            "request": request,
            "slug": slug,
            "frontmatter": page.frontmatter,
            "body_html": _markdown_to_html(page.body),
            "open_items": open_items,
            "version": __version__,
        })

    @app.get("/commitments", response_class=HTMLResponse)
    def commitments(
        request: Request,
        owner: str | None = None,
        status: str | None = "open",
        overdue: bool = False,
    ) -> Any:
        ws = _ws()
        rows = []
        if ws.db_path.exists():
            try:
                rows = work_items.query(
                    ws.open_db(), item_type="action",
                    status=status, owner=owner, overdue=overdue, limit=500,
                )
            except Exception:
                rows = []
        buckets = _bucket_items(rows)

        if _want_json(request):
            return JSONResponse({"count": len(rows), "rows": rows})
        return templates.TemplateResponse(request, "commitments.html", {
            "request": request,
            "buckets": buckets,
            "filters": {"owner": owner, "status": status, "overdue": overdue},
            "version": __version__,
        })

    @app.get("/briefings", response_class=HTMLResponse)
    def briefings(request: Request) -> Any:
        ws = _ws()
        briefing_dir = ws.project_home / "output" / "daemon" / "briefings"
        items = []
        if briefing_dir.exists():
            files = sorted(briefing_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            for f in files[:50]:
                items.append({
                    "name": f.name,
                    "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "size_kb": round(f.stat().st_size / 1024, 1),
                })
        if _want_json(request):
            return JSONResponse({"count": len(items), "briefings": items})
        return templates.TemplateResponse(request, "briefings.html", {
            "request": request,
            "briefings": items,
            "version": __version__,
        })

    @app.get("/briefings/{name}", response_class=HTMLResponse)
    def briefing_detail(request: Request, name: str) -> Any:
        ws = _ws()
        if "/" in name or ".." in name:
            raise HTTPException(status_code=400, detail="invalid name")
        path = ws.project_home / "output" / "daemon" / "briefings" / name
        if not path.exists():
            raise HTTPException(status_code=404, detail="briefing not found")
        body = path.read_text(encoding="utf-8")
        if _want_json(request):
            return JSONResponse({"name": name, "body": body})
        return templates.TemplateResponse(request, "briefing.html", {
            "request": request,
            "name": name,
            "body_html": _markdown_to_html(body),
            "version": __version__,
        })

    @app.get("/connectors", response_class=HTMLResponse)
    def connectors(request: Request) -> Any:
        from secondbrain.connectors import get_connector, list_connectors as list_conns
        statuses = []
        for name, info in list_conns().items():
            entry = {"name": name, "source": info["source"], "available": info["available"]}
            if info["available"]:
                c = get_connector(name)
                if c:
                    try:
                        s = c.status()
                        entry["connected"] = s.connected
                        entry["identity"] = s.identity
                        entry["error"] = s.error
                    except Exception as e:
                        entry["connected"] = False
                        entry["error"] = str(e)
            else:
                entry["missing"] = info.get("missing")
            statuses.append(entry)
        if _want_json(request):
            return JSONResponse({"connectors": statuses})
        return templates.TemplateResponse(request, "connectors.html", {
            "request": request,
            "connectors": statuses,
            "version": __version__,
        })

    @app.get("/daemon", response_class=HTMLResponse)
    def daemon_view(request: Request) -> Any:
        from secondbrain.daemon import state as daemon_state
        running = daemon_state.is_daemon_running()
        pid = daemon_state.read_pid()
        state = daemon_state.read_state()
        history = []
        try:
            ws = _ws()
            if ws.db_path.exists():
                history = daemon_state.fire_history(ws.open_db(), limit=20)
        except HTTPException:
            pass
        if _want_json(request):
            return JSONResponse({
                "running": running, "pid": pid, "state": state, "history": history,
            })
        return templates.TemplateResponse(request, "daemon.html", {
            "request": request,
            "running": running,
            "pid": pid,
            "state": state,
            "history": history,
            "version": __version__,
        })

    return app


def _safe_for_json(obj: Any) -> Any:
    """Coerce non-JSON-serializable types (dates) to strings."""
    if isinstance(obj, dict):
        return {k: _safe_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_safe_for_json(v) for v in obj]
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return obj


def _markdown_to_html(md_text: str) -> str:
    """Minimal markdown → HTML. Avoids pulling a full markdown lib in the core.

    Handles: headings (# ## ###), bullets, bold (**x**), italic (_x_),
    inline code (`x`), code blocks (```), links [text](url), paragraphs.
    Anything else passes through escaped.
    """
    import html
    import re

    lines = md_text.split("\n")
    out: list[str] = []
    in_code_block = False
    in_list = False

    for line in lines:
        if line.startswith("```"):
            if in_code_block:
                out.append("</code></pre>")
                in_code_block = False
            else:
                if in_list:
                    out.append("</ul>")
                    in_list = False
                out.append('<pre class="bg-gray-100 p-3 rounded text-sm overflow-auto"><code>')
                in_code_block = True
            continue

        if in_code_block:
            out.append(html.escape(line))
            continue

        # Headings
        if line.startswith("### "):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f'<h3 class="text-lg font-semibold mt-4 mb-2">{_inline(line[4:])}</h3>')
            continue
        if line.startswith("## "):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f'<h2 class="text-xl font-semibold mt-5 mb-2">{_inline(line[3:])}</h2>')
            continue
        if line.startswith("# "):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f'<h1 class="text-2xl font-bold mt-6 mb-3">{_inline(line[2:])}</h1>')
            continue

        # Bullets
        bullet_match = re.match(r"^(\s*)[-*]\s+(.*)$", line)
        if bullet_match:
            indent = len(bullet_match.group(1))
            content = bullet_match.group(2)
            if not in_list:
                out.append('<ul class="list-disc ml-6 my-2 space-y-1">')
                in_list = True
            extra_class = " ml-4" if indent >= 2 else ""
            out.append(f'<li class="text-sm{extra_class}">{_inline(content)}</li>')
            continue

        # Blank line
        if not line.strip():
            if in_list:
                out.append("</ul>"); in_list = False
            out.append("")
            continue

        # HR
        if line.strip() == "---":
            if in_list:
                out.append("</ul>"); in_list = False
            out.append('<hr class="my-4 border-gray-300">')
            continue

        # Paragraph
        if in_list:
            out.append("</ul>"); in_list = False
        out.append(f'<p class="my-2">{_inline(line)}</p>')

    if in_list:
        out.append("</ul>")
    if in_code_block:
        out.append("</code></pre>")
    return "\n".join(out)


def _inline(text: str) -> str:
    """Inline markdown: bold, italic, code, links. Returns HTML-safe."""
    import html
    import re

    # Escape first, then re-apply formatting (this keeps unsafe HTML out)
    out = html.escape(text)
    # Code spans (do first so we don't touch their innards)
    out = re.sub(r"`([^`]+)`",
                 r'<code class="bg-gray-100 px-1 rounded text-sm">\1</code>', out)
    # Bold + italic
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!_)_([^_]+)_(?!_)", r"<em>\1</em>", out)
    # Links [text](url)
    out = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r'<a href="\2" class="text-blue-600 hover:underline">\1</a>',
        out,
    )
    return out


# ── Module-level app (for `uvicorn secondbrain.web.app:app` and tests) ──
app = create_app()


def find_free_port(preferred: int = 8765) -> int:
    """Try the preferred port; if busy, find a free one."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", preferred))
        s.close()
        return preferred
    except OSError:
        # Find any free port
        s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s2.bind(("127.0.0.1", 0))
        port = s2.getsockname()[1]
        s2.close()
        return port
