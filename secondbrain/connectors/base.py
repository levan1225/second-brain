"""Connector ABC + data shapes shared by all connectors.

Every connector knows: how to auth (interactive), how to scan (pull signal in),
how to send (push action out), and how to check its current status.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Signal:
    """A piece of incoming data from a scan.

    Connectors emit Signals; the secondbrain pipeline turns them into wiki
    pages or work_items via the privacy filter + extraction layer.
    """
    source: str                       # provenance URI, e.g. slack://channel-id/ts
    timestamp: datetime
    author: str                       # who sent it (slack user id or display name)
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SendAction:
    """A request to push something out via a connector."""
    target: str                       # channel id, email address, etc.
    content: str                      # body
    kind: str = "message"             # "message" | "dm" | "email" | "doc-comment" | ...
    subject: str | None = None        # for emails
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SendResult:
    """What a send() returns."""
    success: bool
    target: str
    message_id: str | None = None     # provider-assigned id, for undo / reference
    permalink: str | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConnectorStatus:
    """`sb connect status` payload."""
    name: str
    connected: bool
    identity: str | None = None       # "logged in as @vanthe.exec"
    last_check_at: datetime | None = None
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class Connector(ABC):
    """Pluggable contract for an external system.

    Subclasses implement at minimum: authenticate, status, scan, send.
    Connectors that only push (e.g. Outlook send-only) can raise
    NotImplementedError from scan(); same for send-only inverse.
    """

    name: str = "unnamed"
    requires_oauth: bool = False
    requires_user_token: bool = True   # most connectors need a per-user secret

    # ── Auth ──

    @abstractmethod
    def authenticate(self) -> ConnectorStatus:
        """Walk the user through the auth flow. Stores credentials encrypted.

        Called from `sb connect <name>`. Should be interactive when called
        from a terminal (use rich.prompt) but may be non-interactive when
        invoked by a programmatic test (check ConnectorContext.interactive).
        """

    @abstractmethod
    def status(self) -> ConnectorStatus:
        """Cheap health check — returns whether the connector is currently usable."""

    # ── Data flow ──

    def scan(self, since: datetime | None = None) -> list[Signal]:
        """Pull new signal since the last scan. Default: not implemented."""
        raise NotImplementedError(f"{self.name} does not support scan()")

    def send(self, action: SendAction) -> SendResult:
        """Push an action out. Default: not implemented."""
        raise NotImplementedError(f"{self.name} does not support send()")

    # ── Disconnect ──

    def disconnect(self) -> None:
        """Revoke credentials and remove stored secrets. Default: no-op."""
