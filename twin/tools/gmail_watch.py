"""Gmail push notifications over a Pub/Sub *pull* subscription.

Spec section 6 asks for exactly this shape, and the reason is the same one
that drove Slack to Socket Mode: Gmail's push delivers to a Pub/Sub topic, and
the usual way to consume that is an HTTPS push endpoint -- which a
local-machine-only app (section 5) has nowhere to host. A *pull* subscription
inverts it: this process reaches out to Pub/Sub and asks for pending messages,
so nothing listens on a port.

Three moving parts:

1. `users().watch()` tells Gmail to publish change notifications to the topic.
   It expires after 7 days, silently, so the scheduler renews it.
2. Pub/Sub notifications carry only `{emailAddress, historyId}` -- not the
   mail itself. They are a nudge to go look, nothing more.
3. `history().list()` replays what changed since the stored cursor.

The cursor is the fragile part. Gmail expires old history ids, and asking for
one that has aged out returns 404. That is recovered from by falling back to a
time-window sweep and re-basing the cursor, rather than losing mail silently.
"""

import base64
import datetime as dt
import json
from typing import Any, Dict, List, Optional, Tuple

from googleapiclient.errors import HttpError

from twin.config import SETTINGS
from twin.memory import store
from twin.timeutil import parse_iso
from twin.tools import google_auth

# Gmail caps a watch at 7 days. Renew with room to spare so a missed scheduler
# tick doesn't let it lapse.
WATCH_MAX_DAYS = 7
RENEW_WHEN_WITHIN_HOURS = 24

# Pub/Sub pull batch size. Notifications are tiny, and Gmail coalesces, so this
# is generous.
MAX_PULL_MESSAGES = 50

# How far back to re-sweep when the history cursor has expired.
HISTORY_RECOVERY_DAYS = 3


class PushNotConfigured(RuntimeError):
    """Pub/Sub topic/subscription aren't set."""


def _require_config() -> Tuple[str, str]:
    if not SETTINGS.gmail_push_enabled:
        raise PushNotConfigured(
            "Gmail push needs GMAIL_PUBSUB_TOPIC and GMAIL_PUBSUB_SUBSCRIPTION "
            "in your .env (full resource names, e.g. "
            "projects/my-proj/topics/gmail-push)."
        )
    return SETTINGS.gmail_pubsub_topic, SETTINGS.gmail_pubsub_subscription


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def parse_notification(pubsub_message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Decode one Pub/Sub message into {emailAddress, historyId}.

    The payload is base64-encoded JSON. Anything unparseable is dropped rather
    than raised -- a malformed notification must still be acknowledged, or it
    redelivers forever.
    """
    data = (pubsub_message or {}).get("data")
    if not data:
        return None
    try:
        decoded = base64.urlsafe_b64decode(data).decode("utf-8")
        payload = json.loads(decoded)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or "historyId" not in payload:
        return None
    return payload


def lowest_history_id(notifications: List[Dict[str, Any]]) -> Optional[str]:
    """The earliest historyId in a batch.

    Gmail coalesces notifications, so a batch may span several. Replaying from
    the lowest covers all of them; replaying from the highest would skip the
    changes in between.
    """
    ids = []
    for notification in notifications:
        try:
            ids.append(int(notification.get("historyId")))
        except (TypeError, ValueError):
            continue
    return str(min(ids)) if ids else None


def watch_expiry(response: Dict[str, Any]) -> Optional[dt.datetime]:
    """Read the expiry out of a watch response (epoch milliseconds, as a string)."""
    raw = (response or {}).get("expiration")
    if raw is None:
        return None
    try:
        return dt.datetime.fromtimestamp(int(raw) / 1000.0, dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def needs_renewal(
    expires_at: Optional[dt.datetime],
    now: Optional[dt.datetime] = None,
    within_hours: int = RENEW_WHEN_WITHIN_HOURS,
) -> bool:
    """Whether the watch should be re-registered now."""
    if expires_at is None:
        return True
    now = now or dt.datetime.now(dt.timezone.utc)
    return expires_at - now <= dt.timedelta(hours=within_hours)


def message_ids_from_history(history: List[Dict[str, Any]]) -> List[str]:
    """Collect newly added message ids from a history response, de-duplicated."""
    seen = set()
    ids = []
    for record in history or []:
        for added in record.get("messagesAdded", []) or []:
            message = added.get("message") or {}
            message_id = message.get("id")
            if not message_id or message_id in seen:
                continue
            # Drafts and spam aren't things the user committed to.
            labels = set(message.get("labelIds") or [])
            if labels & {"DRAFT", "SPAM", "TRASH"}:
                continue
            seen.add(message_id)
            ids.append(message_id)
    return ids


# --------------------------------------------------------------------------
# Watch registration
# --------------------------------------------------------------------------

def start_watch() -> Dict[str, Any]:
    """Register (or re-register) Gmail push, storing the cursor and expiry."""
    topic, _ = _require_config()
    service = google_auth.client("gmail")

    response = (
        service.users()
        .watch(userId="me", body={"topicName": topic, "labelIds": ["INBOX"]})
        .execute()
    )

    expires_at = watch_expiry(response)
    updates: Dict[str, Any] = {"gmail_watch_expires_at": expires_at}

    history_id = response.get("historyId")
    if history_id and not store.get_profile().get("gmail_history_id"):
        # Only seed the cursor on first registration. Overwriting it on renewal
        # would skip anything that arrived since the last successful pull.
        updates["gmail_history_id"] = str(history_id)

    store.update_profile(**updates)
    return {"history_id": history_id, "expires_at": expires_at}


def stop_watch() -> None:
    service = google_auth.client("gmail")
    service.users().stop(userId="me").execute()
    store.update_profile(gmail_watch_expires_at=None)


def renew_if_needed() -> bool:
    """Re-register the watch if it's near expiry. Returns True if renewed."""
    if not SETTINGS.gmail_push_enabled:
        return False
    profile = store.get_profile()
    if not needs_renewal(profile.get("gmail_watch_expires_at")):
        return False
    start_watch()
    return True


# --------------------------------------------------------------------------
# Pull subscription
# --------------------------------------------------------------------------

def pull(max_messages: int = MAX_PULL_MESSAGES) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Pull pending notifications. Returns (notifications, ack ids)."""
    _, subscription = _require_config()
    service = google_auth.client("pubsub")

    response = (
        service.projects()
        .subscriptions()
        .pull(subscription=subscription, body={"maxMessages": max_messages})
        .execute()
    )

    notifications = []
    ack_ids = []
    for received in response.get("receivedMessages", []) or []:
        ack_id = received.get("ackId")
        if ack_id:
            ack_ids.append(ack_id)
        parsed = parse_notification(received.get("message") or {})
        if parsed is not None:
            notifications.append(parsed)
    return notifications, ack_ids


def acknowledge(ack_ids: List[str]) -> None:
    """Acknowledge notifications so Pub/Sub stops redelivering them."""
    if not ack_ids:
        return
    _, subscription = _require_config()
    service = google_auth.client("pubsub")
    service.projects().subscriptions().acknowledge(
        subscription=subscription, body={"ackIds": ack_ids}
    ).execute()


# --------------------------------------------------------------------------
# History replay
# --------------------------------------------------------------------------

def fetch_new_message_ids(start_history_id: str) -> Tuple[List[str], Optional[str], bool]:
    """Replay history since a cursor.

    Returns (message ids, new cursor, cursor_expired). When the cursor has
    aged out Gmail answers 404; that's reported rather than raised so the
    caller can fall back to a time-window sweep instead of losing mail.
    """
    service = google_auth.client("gmail")
    records: List[Dict[str, Any]] = []
    latest_history_id = None
    page_token = None

    try:
        while True:
            response = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded"],
                    pageToken=page_token,
                )
                .execute()
            )
            records.extend(response.get("history", []) or [])
            latest_history_id = response.get("historyId") or latest_history_id
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    except HttpError as exc:
        if getattr(exc, "status_code", None) == 404 or "404" in str(exc):
            return [], None, True
        raise

    return message_ids_from_history(records), latest_history_id, False


def process_notifications(max_messages: int = MAX_PULL_MESSAGES) -> Dict[str, Any]:
    """Pull, replay, extract, acknowledge.

    Acknowledgement happens only after processing succeeds; a crash mid-way
    leaves the notification un-acked so Pub/Sub redelivers it.
    """
    if not SETTINGS.gmail_push_enabled:
        return {"pulled": 0, "messages": 0, "deadlines": 0, "recovered": False}

    from twin.tools import gmail_tools

    notifications, ack_ids = pull(max_messages)
    if not notifications:
        acknowledge(ack_ids)  # drop any unparseable stragglers
        return {"pulled": 0, "messages": 0, "deadlines": 0, "recovered": False}

    profile = store.get_profile()
    cursor = profile.get("gmail_history_id") or lowest_history_id(notifications)

    recovered = False
    message_ids, new_cursor, expired = fetch_new_message_ids(cursor)

    if expired:
        # The cursor aged out. Re-sweep a short window so nothing is lost, and
        # re-base the cursor on the newest notification.
        recovered = True
        recorded = gmail_tools.sweep_deadlines(days=HISTORY_RECOVERY_DAYS)
        newest = max(int(n["historyId"]) for n in notifications)
        store.update_profile(gmail_history_id=str(newest))
        acknowledge(ack_ids)
        return {
            "pulled": len(notifications),
            "messages": 0,
            "deadlines": len(recorded),
            "recovered": True,
        }

    recorded = gmail_tools.process_message_ids(message_ids) if message_ids else []

    if new_cursor:
        store.update_profile(gmail_history_id=str(new_cursor))
    acknowledge(ack_ids)

    return {
        "pulled": len(notifications),
        "messages": len(message_ids),
        "deadlines": len(recorded),
        "recovered": recovered,
    }


def status() -> Dict[str, Any]:
    profile = store.get_profile()
    expires_at = profile.get("gmail_watch_expires_at")
    return {
        "configured": SETTINGS.gmail_push_enabled,
        "topic": SETTINGS.gmail_pubsub_topic,
        "subscription": SETTINGS.gmail_pubsub_subscription,
        "history_id": profile.get("gmail_history_id"),
        "expires_at": expires_at,
        "needs_renewal": needs_renewal(expires_at),
    }
