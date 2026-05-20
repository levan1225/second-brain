"""Connector discovery.

Built-in connectors register themselves in BUILTIN_CONNECTORS. Third-party
connectors register via Python entry-points (group: secondbrain.connectors).

Resolution order:
  1. Built-in (this file's BUILTIN_CONNECTORS dict)
  2. Entry-points discovered at runtime via importlib.metadata
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Type

from .base import Connector


# v3.0 ships with no built-in connectors. Slack + Outlook are handled by
# Claude Desktop's native connector UI (you set them up once there, and
# Claude sessions can use them via standard MCP tools).
#
# This dict stays — it's the registration point for v3.1+ connectors that
# Claude Desktop can't provide (Jira, Smartsheet, internal systems, etc).
# Third parties register the same way via PyPI entry-points:
#
#   [project.entry-points."secondbrain.connectors"]
#   jira = "secondbrain_jira:JiraConnector"
_BUILTIN_FACTORIES: dict[str, callable] = {}


def list_connectors() -> dict[str, dict]:
    """List every connector name → {available, source, requires}."""
    out: dict[str, dict] = {}

    # Built-ins
    for name, factory in _BUILTIN_FACTORIES.items():
        cls = factory()
        if cls is not None:
            out[name] = {
                "available": True,
                "source": "builtin",
                "class": cls.__module__ + "." + cls.__name__,
            }
        else:
            out[name] = {
                "available": False,
                "source": "builtin",
                "missing": f"pip install 'secondbrain[{name}]'",
            }

    # Entry-points
    try:
        eps = entry_points(group="secondbrain.connectors")
    except TypeError:
        # Python < 3.10 fallback (we require 3.11+ so this is defensive)
        eps = entry_points().get("secondbrain.connectors", [])
    for ep in eps:
        if ep.name in out:
            continue  # built-in wins
        try:
            cls = ep.load()
            out[ep.name] = {
                "available": True,
                "source": "entry-point",
                "class": f"{cls.__module__}.{cls.__name__}",
            }
        except Exception as e:
            out[ep.name] = {
                "available": False,
                "source": "entry-point",
                "error": str(e),
            }

    return out


def get_connector(name: str) -> Connector | None:
    """Return an instance of the named connector, or None if unavailable."""
    if name in _BUILTIN_FACTORIES:
        cls = _BUILTIN_FACTORIES[name]()
        if cls is not None:
            return cls()

    # Entry-points
    try:
        eps = entry_points(group="secondbrain.connectors")
    except TypeError:
        eps = entry_points().get("secondbrain.connectors", [])
    for ep in eps:
        if ep.name == name:
            try:
                cls = ep.load()
                return cls()
            except Exception:
                return None

    return None
