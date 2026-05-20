"""Encrypted secret storage via OS keychain.

macOS:   Keychain Access ("login" keychain)
Linux:   libsecret / GNOME Keyring / KWallet (whichever is installed)
Windows: Credential Manager

All secrets are scoped under service name `secondbrain:<connector>` so they
don't collide with other apps and so `sb connect status` can scan them.

Fallback: if keyring's backend is the in-memory `Null` keyring (no real OS
store available), we refuse to store anything and prompt the user to set
SECONDBRAIN_<NAME>_TOKEN as an env var instead.
"""

from __future__ import annotations

import os
from typing import Any

import keyring
from keyring.backends.fail import Keyring as FailKeyring


SERVICE_PREFIX = "secondbrain:"


def _service_name(connector: str) -> str:
    return f"{SERVICE_PREFIX}{connector}"


def _env_var_name(connector: str, key: str) -> str:
    return f"SECONDBRAIN_{connector.upper()}_{key.upper()}"


def _backend_works() -> bool:
    """Return True if we have a real OS keyring (not the fail backend)."""
    backend = keyring.get_keyring()
    return not isinstance(backend, FailKeyring)


def store(connector: str, key: str, value: str) -> None:
    """Store a secret. Raises RuntimeError if no real backend available."""
    if not _backend_works():
        raise RuntimeError(
            f"No OS keychain available (backend: {type(keyring.get_keyring()).__name__}). "
            f"Set {_env_var_name(connector, key)} as an env var instead."
        )
    keyring.set_password(_service_name(connector), key, value)


def retrieve(connector: str, key: str) -> str | None:
    """Retrieve a secret. Falls back to env var. Returns None if neither set."""
    # Env var wins for explicit overrides + CI
    env_val = os.environ.get(_env_var_name(connector, key))
    if env_val:
        return env_val

    if not _backend_works():
        return None
    try:
        return keyring.get_password(_service_name(connector), key)
    except Exception:
        return None


def delete(connector: str, key: str) -> bool:
    """Delete a stored secret. Returns True if deleted, False if not found."""
    if not _backend_works():
        return False
    try:
        keyring.delete_password(_service_name(connector), key)
        return True
    except keyring.errors.PasswordDeleteError:
        return False
    except Exception:
        return False


def list_stored(connector: str, keys: list[str]) -> dict[str, bool]:
    """Check which of the given keys have a stored secret. Used by status checks."""
    return {k: (retrieve(connector, k) is not None) for k in keys}
