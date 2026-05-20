"""secondbrain.web — localhost dashboard (Plane 3).

A read-only web UI for execs/PMs who don't want to use the terminal. Runs on
http://localhost:8765, no auth (OS user IS the auth).

The UI is HTMX + Tailwind via CDN — zero JS build step. Templates live in
secondbrain/web/templates/. Routes live in secondbrain/web/app.py.

Started via:
    sb web                    # foreground
    sb web --port 9000        # custom port
"""
