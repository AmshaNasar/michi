"""Slack trigger layer, over Socket Mode.

Spec section 6 calls for webhooks "where a service offers them (e.g. Slack
Events API)". The Events API needs a public HTTPS endpoint, which conflicts
with section 5's local-machine-only constraint. Socket Mode is Slack's own
answer: the app opens an outbound WebSocket and receives the same events with
nothing listening on a port -- the same reasoning the spec already applies to
Gmail's Pub/Sub *pull* subscription.

What it does with an incoming message:

1. indexes it into recall, so the twin can answer questions about it later
2. for messages that concern the user specifically, asks the cheap auxiliary
   model whether a deadline or commitment was made, and records it

Step 2 is gated hard. Running an LLM call over every message in a busy
workspace would be both expensive and noisy, so it only fires on messages the
user wrote or was mentioned in.
"""

import datetime as dt
import re
import threading
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional, Set

from twin import extraction
from twin.memory import embeddings, store
from twin.tools import slack_auth, slack_tools

# Deadline parsing is shared with the email and calendar sweeps; re-exported
# here because it's part of this module's tested surface.
parse_extraction = extraction.parse_extraction

# Slack retries undelivered events; ids are kept so a redelivery doesn't
# double-index or double-extract.
SEEN_EVENT_CAPACITY = 2000

# Subtypes that carry no conversational content.
IGNORED_SUBTYPES = {
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_name",
    "message_changed",
    "message_deleted",
    "bot_add",
    "bot_remove",
    "thread_broadcast_joined",
}

MIN_EXTRACTION_CONFIDENCE = extraction.MIN_CONFIDENCE


class SlackListener:
    """Long-running Socket Mode client."""

    def __init__(self, on_event: Optional[Callable[[Dict[str, Any]], None]] = None):
        self._seen: Deque[str] = deque(maxlen=SEEN_EVENT_CAPACITY)
        self._seen_set: Set[str] = set()
        self._on_event = on_event
        self._client = None
        self._stop = threading.Event()

    # -- dedup -------------------------------------------------------------

    def already_seen(self, event_id: str) -> bool:
        """Whether this event id has been handled. Records it if not."""
        if not event_id:
            return False
        if event_id in self._seen_set:
            return True
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(event_id)
        self._seen_set.add(event_id)
        return False

    # -- wiring ------------------------------------------------------------

    def start(self) -> None:
        """Connect and block until stopped. Raises NotConnected without tokens."""
        from slack_sdk import WebClient
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.request import SocketModeRequest
        from slack_sdk.socket_mode.response import SocketModeResponse

        tokens = slack_auth.load_tokens() or {}
        app_token = slack_auth.app_token()  # raises if absent

        self._client = SocketModeClient(
            app_token=app_token,
            web_client=WebClient(token=tokens["bot_token"]),
        )

        def handle(client, request: SocketModeRequest) -> None:
            # Slack requires an ack within three seconds, so acknowledge
            # before doing any work.
            client.send_socket_mode_response(
                SocketModeResponse(envelope_id=request.envelope_id)
            )
            if request.type != "events_api":
                return
            payload = request.payload or {}
            if self.already_seen(payload.get("event_id", "")):
                return
            try:
                self.handle_event(payload.get("event") or {})
            except Exception:
                # A bad message must never kill the listener.
                pass

        self._client.socket_mode_request_listeners.append(handle)
        self._client.connect()
        self._stop.wait()

    def stop(self) -> None:
        self._stop.set()
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass

    # -- processing --------------------------------------------------------

    def handle_event(self, event: Dict[str, Any]) -> None:
        if not is_processable(event, slack_auth.bot_user_id()):
            return

        index_event(event)
        if self._on_event:
            self._on_event(event)

        tokens = slack_auth.load_tokens() or {}
        if should_analyze(event, tokens.get("human_user_id", ""), tokens.get("bot_user_id", "")):
            extract_commitment(event)


# --------------------------------------------------------------------------
# Pure predicates
# --------------------------------------------------------------------------

def is_processable(event: Dict[str, Any], bot_user_id: str = "") -> bool:
    """Whether an event is a real human message worth handling."""
    if event.get("type") != "message":
        return False
    if event.get("subtype") in IGNORED_SUBTYPES:
        return False
    if event.get("bot_id"):
        return False
    if bot_user_id and event.get("user") == bot_user_id:
        # Never react to our own posts.
        return False
    return bool((event.get("text") or "").strip())


def mentions_user(text: str, user_id: str) -> bool:
    if not user_id or not text:
        return False
    return bool(re.search(r"<@{0}(\||>)".format(re.escape(user_id)), text))


def should_analyze(
    event: Dict[str, Any],
    human_user_id: str = "",
    bot_user_id: str = "",
) -> bool:
    """Gate the LLM extraction to messages that actually concern the user.

    Without this, a busy workspace would trigger an auxiliary model call per
    message. Direct messages always qualify; channel messages only when the
    user wrote them or was mentioned.
    """
    text = event.get("text") or ""

    if human_user_id:
        if event.get("user") == human_user_id:
            return True
        if mentions_user(text, human_user_id):
            return True

    # Channel ids beginning with D are direct messages -- always about the user.
    if str(event.get("channel", "")).startswith("D"):
        return True

    if bot_user_id and mentions_user(text, bot_user_id):
        return True

    return False


# --------------------------------------------------------------------------
# Side effects
# --------------------------------------------------------------------------

def index_event(event: Dict[str, Any]) -> None:
    """Add an incoming message to the recall index."""
    channel = str(event.get("channel", "unknown"))
    text = slack_tools.clean_text(event.get("text", ""))
    content = "Slack {0} -- [{1}] {2}: {3}".format(
        channel,
        slack_tools.format_timestamp(event.get("ts", "")),
        event.get("user", "unknown"),
        text,
    )

    vector = None
    try:
        vector = embeddings.embed(content)
    except Exception:
        pass

    store.store_embedding(
        kind="slack",
        ref_id="{0}:{1}".format(channel, event.get("ts", "")),
        content=content,
        vector=vector,
        metadata={"channel": channel, "ts": event.get("ts", ""), "user": event.get("user", "")},
    )


def extract_commitment(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Ask the cheap model whether this message commits the user to something."""
    try:
        sent_at = dt.datetime.fromtimestamp(float(event.get("ts", 0)), dt.timezone.utc)
    except (TypeError, ValueError):
        sent_at = dt.datetime.now(dt.timezone.utc)

    parsed = extraction.extract_one(
        slack_tools.clean_text(event.get("text", "")), sent_at
    )
    if parsed is None:
        return None

    channel = str(event.get("channel", "unknown"))
    store.upsert_deadline(
        description=parsed["description"],
        due_at=parsed["due_at"],
        source="slack",
        source_ref="{0}:{1}".format(channel, event.get("ts", "")),
    )
    return parsed
