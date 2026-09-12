"""Slack authentication and client construction.

Slack's OAuth flow requires an HTTPS redirect URL, which a local-only app
(spec section 5) has nowhere to host. So the user creates their own Slack app,
installs it to their workspace, and pastes the tokens during onboarding --
the same shape as the Google flow, minus the browser round-trip.

Three tokens, only the first required:

  bot   (xoxb-)  reading channels and posting          -- required
  app   (xapp-)  Socket Mode listener                  -- optional
  user  (xoxp-)  search.messages, which bots can't call -- optional
"""

from typing import Any, Dict, Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from twin import credentials as credstore

SERVICE = "slack"

# What the user needs to add under OAuth & Permissions. Surfaced verbatim in
# onboarding, because a missing scope is the single most common failure.
BOT_SCOPES = [
    "channels:read",
    "channels:history",
    "groups:read",
    "groups:history",
    "im:read",
    "im:history",
    "users:read",
    "chat:write",
]
APP_SCOPES = ["connections:write"]
USER_SCOPES = ["search:read"]


class NotConnected(RuntimeError):
    """Slack isn't connected, or the needed token type is missing."""


class SlackError(RuntimeError):
    """A Slack API call failed, with the cause translated into English."""


def describe_api_error(exc: SlackApiError) -> str:
    """Turn a Slack error code into something the user can act on."""
    try:
        code = exc.response.get("error", "unknown_error")
    except Exception:
        code = "unknown_error"

    if code == "missing_scope":
        needed = ""
        try:
            needed = exc.response.get("needed") or ""
        except Exception:
            pass
        return (
            "Your Slack app is missing the '{0}' scope. Add it under OAuth & "
            "Permissions, then reinstall the app to the workspace and re-run "
            "onboarding.".format(needed or "required")
        )
    if code == "not_in_channel":
        return (
            "The bot isn't in that channel. Invite it from Slack with "
            "`/invite @your-app-name`, then try again."
        )
    if code == "channel_not_found":
        return "No such channel, or the bot can't see it (private channels need an invite)."
    if code == "invalid_auth":
        return "Slack rejected the token. Re-run onboarding to paste a fresh one."
    if code == "account_inactive":
        return "That Slack token belongs to a deactivated app or user."
    if code == "ratelimited":
        return "Slack is rate-limiting this workspace. Try again shortly."
    return "Slack API error: {0}".format(code)


def validate_bot_token(token: str) -> Dict[str, Any]:
    """Check a bot token with auth.test, returning workspace identity."""
    client = WebClient(token=token)
    try:
        response = client.auth_test()
    except SlackApiError as exc:
        raise SlackError(describe_api_error(exc)) from exc

    return {
        "team": response.get("team", ""),
        "team_id": response.get("team_id", ""),
        "bot_user_id": response.get("user_id", ""),
        "bot_name": response.get("user", ""),
    }


def save_tokens(
    bot_token: str,
    app_token: Optional[str] = None,
    user_token: Optional[str] = None,
    identity: Optional[Dict[str, Any]] = None,
) -> None:
    payload: Dict[str, Any] = {"bot_token": bot_token}
    if app_token:
        payload["app_token"] = app_token
    if user_token:
        payload["user_token"] = user_token
    if identity:
        payload.update(identity)
    credstore.save_token(SERVICE, payload)


def load_tokens() -> Optional[Dict[str, Any]]:
    return credstore.load_token(SERVICE)


def bot_client() -> WebClient:
    tokens = load_tokens()
    if not tokens or not tokens.get("bot_token"):
        raise NotConnected(
            "Slack is not connected. Run `python -m twin.cli onboard` and enable it."
        )
    return WebClient(token=tokens["bot_token"])


def user_client() -> WebClient:
    """Client for endpoints bots cannot call (search.messages)."""
    tokens = load_tokens() or {}
    if not tokens.get("user_token"):
        raise NotConnected(
            "Slack search needs a user token (xoxp-) with the search:read scope. "
            "Bot tokens can't call search.messages. Re-run onboarding to add one."
        )
    return WebClient(token=tokens["user_token"])


def app_token() -> str:
    tokens = load_tokens() or {}
    token = tokens.get("app_token")
    if not token:
        raise NotConnected(
            "The Socket Mode listener needs an app-level token (xapp-) with the "
            "connections:write scope. Create one under Basic Information > "
            "App-Level Tokens, then re-run onboarding."
        )
    return token


def bot_user_id() -> str:
    return (load_tokens() or {}).get("bot_user_id", "")


def has_search() -> bool:
    return bool((load_tokens() or {}).get("user_token"))


def has_socket_mode() -> bool:
    return bool((load_tokens() or {}).get("app_token"))
