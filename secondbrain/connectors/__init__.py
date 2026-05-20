"""secondbrain.connectors — plugin framework for external systems.

A connector is anything that pulls signal in (scan) or pushes action out (send).
Built-in connectors: slack, outlook (future), drive (future), zoom (future).
Third-party connectors are PyPI packages that register via entry-points:

    # in someone's pyproject.toml
    [project.entry-points."secondbrain.connectors"]
    asana = "secondbrain_asana_connector:AsanaConnector"

Public surface:
    Connector       — abstract base class
    Signal          — what a scan() returns (incoming data)
    SendAction      — what a send() consumes (outgoing data)
    SendResult      — what a send() returns
    registry        — discovery: list_connectors(), get_connector(name)
    secrets         — encrypted token storage via OS keychain
"""

from .base import Connector, Signal, SendAction, SendResult
from .registry import list_connectors, get_connector

__all__ = [
    "Connector",
    "Signal",
    "SendAction",
    "SendResult",
    "list_connectors",
    "get_connector",
]
