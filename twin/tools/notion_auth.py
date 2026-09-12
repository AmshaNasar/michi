"""Notion authentication and HTTP client.

Notion's public OAuth flow needs a hosted redirect URL, so -- as with Slack --
the user creates their own internal integration and pastes the token during
onboarding.

Called through httpx rather than the official SDK, because the one thing that
matters most here is pinning `Notion-Version` explicitly. Notion ships
backwards-incompatible versions, and the 2025-09-03 release moved databases to
a data-source model; an SDK upgrade silently changing that header underneath us
would break queries in ways that are hard to trace.
"""

from typing import Any, Dict, Optional

import httpx

from twin import credentials as credstore

SERVICE = "notion"
API_BASE = "https://api.notion.com/v1"

# Pinned deliberately. See module docstring -- do not float this.
NOTION_VERSION = "2026-03-11"

TIMEOUT_SECONDS = 30.0


class NotConnected(RuntimeError):
    """Notion isn't connected."""


class NotionError(RuntimeError):
    """A Notion API call failed, with the cause translated into English."""


def describe_error(status_code: int, payload: Optional[Dict[str, Any]] = None) -> str:
    """Turn a Notion error into something the user can act on."""
    payload = payload or {}
    code = payload.get("code", "")
    message = payload.get("message", "")

    if status_code == 401 or code == "unauthorized":
        return "Notion rejected the token. Re-run onboarding to paste a fresh one."
    if status_code == 404 or code == "object_not_found":
        # By far the most common failure: integrations see nothing by default.
        return (
            "Notion can't see that page or database. In Notion, open it, click "
            "the ... menu > Connections > and add your integration. Sharing a "
            "parent page shares everything beneath it."
        )
    if status_code == 429 or code == "rate_limited":
        return "Notion is rate-limiting this integration. Try again shortly."
    if code == "validation_error":
        return "Notion rejected the request: {0}".format(message or "validation error")
    if code == "restricted_resource":
        return "The integration lacks permission for that action (check its capabilities)."
    return "Notion API error ({0}): {1}".format(status_code, message or code or "unknown")


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": "Bearer {0}".format(token),
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def request(
    method: str,
    path: str,
    token: Optional[str] = None,
    json: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Call the Notion API, raising NotionError with a readable message."""
    if token is None:
        token = load_token()
        if not token:
            raise NotConnected(
                "Notion is not connected. Run `python -m twin.cli onboard` and enable it."
            )

    try:
        response = httpx.request(
            method,
            "{0}{1}".format(API_BASE, path),
            headers=_headers(token),
            json=json,
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise NotionError("Could not reach Notion: {0}".format(exc)) from exc

    if response.status_code >= 400:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        raise NotionError(describe_error(response.status_code, payload))

    try:
        return response.json()
    except ValueError as exc:
        raise NotionError("Notion returned a non-JSON response.") from exc


def validate_token(token: str) -> Dict[str, Any]:
    """Check a token and return the integration's identity."""
    payload = request("GET", "/users/me", token=token)
    bot = payload.get("bot") or {}
    owner = bot.get("owner") or {}
    return {
        "name": payload.get("name") or "integration",
        "bot_id": payload.get("id", ""),
        "workspace_name": bot.get("workspace_name") or "",
        "owner_type": owner.get("type", ""),
    }


def save_token(token: str, identity: Optional[Dict[str, Any]] = None) -> None:
    payload: Dict[str, Any] = {"token": token}
    if identity:
        payload.update(identity)
    credstore.save_token(SERVICE, payload)


def load_token() -> Optional[str]:
    stored = credstore.load_token(SERVICE)
    if not stored:
        return None
    return stored.get("token")


def identity() -> Dict[str, Any]:
    return credstore.load_token(SERVICE) or {}
