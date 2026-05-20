"""Tests for the connector framework.

v3.0 ships with no built-in connectors — Slack + Outlook are handled by
Claude Desktop's native connector UI. The framework remains for third-party
connectors (Jira, etc.) registered via PyPI entry-points.

We test the contract + the registry + the secrets layer.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from secondbrain.connectors import (
    Connector,
    SendAction,
    SendResult,
    get_connector,
    list_connectors,
)
from secondbrain.connectors.base import ConnectorStatus, Signal
from secondbrain.connectors import secrets


def test_no_builtin_connectors_in_v3() -> None:
    """v3.0 ships with no built-in connectors — use Claude Desktop's instead."""
    out = list_connectors()
    # Should be empty unless a third-party connector is installed via PyPI
    builtin_count = sum(1 for v in out.values() if v.get("source") == "builtin")
    assert builtin_count == 0, "v3.0 should not ship built-in connectors"


def test_get_connector_unknown_returns_none() -> None:
    assert get_connector("doesnotexist") is None


def test_secrets_env_var_override() -> None:
    """Env var should win over keychain (useful for CI + explicit override)."""
    os.environ["SECONDBRAIN_TESTCONN_TOKEN"] = "envvar-token-123"
    try:
        assert secrets.retrieve("testconn", "token") == "envvar-token-123"
    finally:
        del os.environ["SECONDBRAIN_TESTCONN_TOKEN"]


def test_secrets_returns_none_when_no_keychain_and_no_env() -> None:
    # No env var set, no token stored under this random name
    assert secrets.retrieve("nonexistent_connector_xyz", "token") is None


def test_connector_abc_requires_authenticate_and_status() -> None:
    """Subclasses must implement the abstract methods."""
    with pytest.raises(TypeError):
        Connector()  # can't instantiate ABC


def test_dataclass_shapes() -> None:
    """Signal + SendAction + SendResult are real dataclasses with expected fields."""
    sig = Signal(source="slack://abc", timestamp=datetime.now(timezone.utc),
                 author="U123", text="hi")
    assert sig.source == "slack://abc"
    assert sig.metadata == {}

    action = SendAction(target="self", content="hello", kind="dm")
    assert action.kind == "dm"

    result = SendResult(success=True, target="self", message_id="123.456")
    assert result.success
    assert result.permalink is None
