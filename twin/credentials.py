"""Per-service credential storage backed by the OS keychain.

The spec names Windows Credential Manager; `keyring` picks the right backend
per platform, so this is the macOS Keychain here and Credential Manager on
Windows with no code change.

Tokens are stored as JSON blobs keyed by service name (e.g. "gmail"), so an
adapter can round-trip whatever its auth library hands back.
"""

import json
from typing import Any, Dict, Optional

import keyring
from keyring.errors import PasswordDeleteError

SERVICE_NAMESPACE = "digital-twin"


def _key(service: str) -> str:
    return "{0}:{1}".format(SERVICE_NAMESPACE, service)


def save_token(service: str, payload: Dict[str, Any]) -> None:
    """Persist a credential blob for a service."""
    keyring.set_password(_key(service), "default", json.dumps(payload))


def load_token(service: str) -> Optional[Dict[str, Any]]:
    """Return the stored credential blob, or None if the service isn't connected."""
    raw = keyring.get_password(_key(service), "default")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # A corrupted entry should behave as "not connected" rather than crash
        # the agent mid-loop; re-running onboarding will overwrite it.
        return None


def delete_token(service: str) -> None:
    """Disconnect a service. Safe to call when nothing is stored."""
    try:
        keyring.delete_password(_key(service), "default")
    except PasswordDeleteError:
        pass


def is_connected(service: str) -> bool:
    return load_token(service) is not None
