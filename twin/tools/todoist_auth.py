"""Todoist authentication and HTTP client.

Uses the unified **API v1** (`https://api.todoist.com/api/v1`). Todoist
deprecated both REST v2 (`/rest/v2`) and Sync v9 in favour of this; the old
paths redirect, but the redirect carries breaking changes -- ids became opaque
strings and paths are strictly lowercase -- so the new base is addressed
directly rather than relying on a redirect.

As with Slack and Notion, OAuth needs a hosted redirect URL, so the user pastes
a personal API token from Settings > Integrations > Developer.
"""

from typing import Any, Dict, List, Optional

import httpx

from twin import credentials as credstore

SERVICE = "todoist"
API_BASE = "https://api.todoist.com/api/v1"
TIMEOUT_SECONDS = 30.0

# Todoist caps page size at 200 for list endpoints.
MAX_PAGE_SIZE = 200
# Bounds a runaway pagination loop on a very large account.
MAX_PAGES = 20


class NotConnected(RuntimeError):
    """Todoist isn't connected."""


class TodoistError(RuntimeError):
    """A Todoist API call failed, with the cause translated into English."""


def describe_error(status_code: int, body: str = "") -> str:
    if status_code == 401:
        return "Todoist rejected the token. Re-run onboarding to paste a fresh one."
    if status_code == 403:
        return "That Todoist token lacks permission for this action."
    if status_code == 404:
        return "No such Todoist project or task, or it isn't visible to this token."
    if status_code == 429:
        return "Todoist is rate-limiting this token. Try again shortly."
    if status_code == 400:
        return "Todoist rejected the request: {0}".format(body[:200] or "bad request")
    return "Todoist API error ({0}): {1}".format(status_code, body[:200] or "unknown")


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": "Bearer {0}".format(token),
        "Content-Type": "application/json",
    }


def request(
    method: str,
    path: str,
    token: Optional[str] = None,
    json: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    """Call the Todoist API, raising TodoistError with a readable message."""
    if token is None:
        token = load_token()
        if not token:
            raise NotConnected(
                "Todoist is not connected. Run `python -m twin.cli onboard` and enable it."
            )

    try:
        response = httpx.request(
            method,
            "{0}{1}".format(API_BASE, path),
            headers=_headers(token),
            json=json,
            params=params,
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise TodoistError("Could not reach Todoist: {0}".format(exc)) from exc

    if response.status_code >= 400:
        raise TodoistError(describe_error(response.status_code, response.text))

    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise TodoistError("Todoist returned a non-JSON response.") from exc


def paginate(
    path: str,
    params: Optional[Dict[str, Any]] = None,
    limit: Optional[int] = None,
    token: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Follow `next_cursor` through a list endpoint.

    API v1 wraps list responses in {"results": [...], "next_cursor": ...}, so
    a single call can silently return a partial view -- which for a deadline
    sync would mean quietly missing tasks.
    """
    collected: List[Dict[str, Any]] = []
    query = dict(params or {})
    query.setdefault("limit", MAX_PAGE_SIZE)
    cursor = None

    for _ in range(MAX_PAGES):
        if cursor:
            query["cursor"] = cursor
        payload = request("GET", path, token=token, params=query)

        if isinstance(payload, list):
            # Defensive: some endpoints have historically returned a bare list.
            collected.extend(payload)
            break

        collected.extend((payload or {}).get("results", []) or [])
        cursor = (payload or {}).get("next_cursor")
        if not cursor:
            break
        if limit is not None and len(collected) >= limit:
            break

    return collected[:limit] if limit is not None else collected


def validate_token(token: str) -> Dict[str, Any]:
    """Check a token by making the cheapest authenticated call available."""
    payload = request("GET", "/projects", token=token, params={"limit": 1})
    projects = (payload or {}).get("results", []) if isinstance(payload, dict) else payload
    return {"project_count_sample": len(projects or [])}


def save_token(token: str, identity: Optional[Dict[str, Any]] = None) -> None:
    stored: Dict[str, Any] = {"token": token}
    if identity:
        stored.update(identity)
    credstore.save_token(SERVICE, stored)


def load_token() -> Optional[str]:
    stored = credstore.load_token(SERVICE)
    if not stored:
        return None
    return stored.get("token")
