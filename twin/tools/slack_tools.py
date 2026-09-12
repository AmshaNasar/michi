"""Slack adapter -- Tier 1b integration.

Read tools run unattended. Posting is the one write, and it's gated on
confirmation like every other outbound action.

Messages the twin reads are indexed into recall, so "what did Sarah say about
the deadline" works later without re-querying Slack.

The text-mangling functions are pure and tested directly: Slack's wire format
wraps mentions, channels, and links in markup that is unreadable if passed
through to a model or read aloud.
"""

import datetime as dt
import re
from typing import Any, Dict, List, Optional, Sequence

from slack_sdk.errors import SlackApiError

from twin.memory import embeddings, store
from twin.timeutil import zone
from twin.tools import slack_auth
from twin.tools.registry import obj, tool

DEFAULT_MESSAGE_LIMIT = 50
MAX_MESSAGE_LIMIT = 200

# Resolved once per process; workspaces don't rename channels mid-session.
_channel_cache: Dict[str, str] = {}
_user_cache: Dict[str, str] = {}


# --------------------------------------------------------------------------
# Pure text handling
# --------------------------------------------------------------------------

def normalize_channel_ref(ref: str) -> str:
    """Accept '#general', 'general', or a raw channel id."""
    return (ref or "").strip().lstrip("#")


def clean_text(text: str, user_names: Optional[Dict[str, str]] = None) -> str:
    """Unwrap Slack's markup into something readable.

    Slack sends `<@U123>`, `<#C123|general>`, and `<https://x|label>`; passed
    through raw these waste tokens and are unintelligible when spoken.
    """
    if not text:
        return ""

    names = user_names or {}

    def replace_user(match):
        user_id = match.group(1)
        label = match.group(2)
        return "@{0}".format(label or names.get(user_id) or user_id)

    cleaned = re.sub(r"<@([UW][A-Z0-9]+)(?:\|([^>]+))?>", replace_user, text)
    cleaned = re.sub(r"<#([C][A-Z0-9]+)\|([^>]+)>", r"#\2", cleaned)
    cleaned = re.sub(r"<#([C][A-Z0-9]+)>", r"#\1", cleaned)
    cleaned = re.sub(r"<!(here|channel|everyone)>", r"@\1", cleaned)
    cleaned = re.sub(r"<mailto:[^|>]+\|([^>]+)>", r"\1", cleaned)
    cleaned = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2", cleaned)
    cleaned = re.sub(r"<(https?://[^>]+)>", r"\1", cleaned)

    # Slack HTML-escapes these three and only these three.
    cleaned = cleaned.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return cleaned.strip()


def format_timestamp(ts: str, tz: Optional[dt.tzinfo] = None) -> str:
    """Render a Slack ts ('1726142400.123456') as a local datetime."""
    try:
        moment = dt.datetime.fromtimestamp(float(ts), tz or dt.timezone.utc)
    except (TypeError, ValueError):
        return "unknown time"
    return moment.strftime("%a %d %b %H:%M")


def format_message(
    message: Dict[str, Any],
    user_names: Optional[Dict[str, str]] = None,
    tz: Optional[dt.tzinfo] = None,
) -> str:
    names = user_names or {}
    user_id = message.get("user") or message.get("bot_id") or ""
    author = names.get(user_id) or message.get("username") or user_id or "unknown"
    body = clean_text(message.get("text", ""), names)

    parts = ["[{0}] {1}: {2}".format(format_timestamp(message.get("ts", ""), tz), author, body)]

    if message.get("thread_ts") and message.get("thread_ts") != message.get("ts"):
        parts.append("(in thread)")
    reply_count = message.get("reply_count")
    if reply_count:
        parts.append("({0} repl{1}, thread_ts={2})".format(
            reply_count, "y" if reply_count == 1 else "ies", message.get("ts")
        ))
    return " ".join(parts)


def is_indexable(message: Dict[str, Any]) -> bool:
    """Whether a message is worth storing in recall.

    Join/leave notices and channel-topic changes are noise, and empty texts
    (file-only posts) have nothing to embed.
    """
    if message.get("subtype") in {
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "bot_add",
        "bot_remove",
    }:
        return False
    return bool((message.get("text") or "").strip())


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------

def _user_names(client, message_list: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """Resolve the user ids appearing in a batch, using a process-wide cache."""
    wanted = {
        message.get("user")
        for message in message_list
        if message.get("user") and message["user"] not in _user_cache
    }
    for user_id in wanted:
        try:
            info = client.users_info(user=user_id)
            profile = info.get("user", {})
            _user_cache[user_id] = (
                profile.get("profile", {}).get("display_name")
                or profile.get("real_name")
                or profile.get("name")
                or user_id
            )
        except SlackApiError:
            # A name we can't resolve shouldn't cost us the whole read.
            _user_cache[user_id] = user_id
    return dict(_user_cache)


def resolve_channel(client, ref: str) -> str:
    """Turn a channel name into an id, passing ids through untouched."""
    ref = normalize_channel_ref(ref)
    if not ref:
        raise slack_auth.SlackError("No channel given.")

    # Slack channel/DM ids start with C, G, or D and are all-caps.
    if re.fullmatch(r"[CGD][A-Z0-9]{5,}", ref):
        return ref

    if ref in _channel_cache:
        return _channel_cache[ref]

    cursor = None
    while True:
        response = client.conversations_list(
            types="public_channel,private_channel,mpim,im",
            limit=1000,
            cursor=cursor,
            exclude_archived=True,
        )
        for channel in response.get("channels", []):
            name = channel.get("name")
            if name:
                _channel_cache[name] = channel["id"]
        cursor = (response.get("response_metadata") or {}).get("next_cursor")
        if ref in _channel_cache or not cursor:
            break

    if ref not in _channel_cache:
        raise slack_auth.SlackError(
            "No channel called '{0}' is visible to the bot. Private channels "
            "need `/invite @your-app-name` first.".format(ref)
        )
    return _channel_cache[ref]


def _index_messages(channel_ref: str, messages: Sequence[Dict[str, Any]], names: Dict[str, str]) -> None:
    for message in messages:
        if not is_indexable(message):
            continue
        content = "Slack #{0} -- {1}".format(
            channel_ref, format_message(message, names)
        )
        vector = None
        try:
            vector = embeddings.embed(content)
        except Exception:
            pass
        store.store_embedding(
            kind="slack",
            ref_id="{0}:{1}".format(channel_ref, message.get("ts", "")),
            content=content,
            vector=vector,
            metadata={"channel": channel_ref, "ts": message.get("ts", "")},
        )


def _tz() -> dt.tzinfo:
    return zone(store.get_profile().get("timezone") or "UTC")


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@tool(
    name="slack_list_channels",
    description=(
        "List Slack channels and DMs the bot can see. Use this to find the "
        "right channel id before reading. The bot only sees private channels "
        "it has been invited to."
    ),
    input_schema=obj(
        {"limit": {"type": "integer", "description": "Max channels, default 100."}}
    ),
    requires="slack",
)
def slack_list_channels(args: Dict[str, Any]) -> str:
    client = slack_auth.bot_client()
    limit = min(int(args.get("limit", 100)), 1000)
    try:
        response = client.conversations_list(
            types="public_channel,private_channel,mpim,im",
            limit=limit,
            exclude_archived=True,
        )
    except SlackApiError as exc:
        return slack_auth.describe_api_error(exc)

    channels = response.get("channels", [])
    if not channels:
        return "The bot can't see any channels yet. Invite it to one with `/invite @your-app-name`."

    lines = []
    for channel in channels:
        name = channel.get("name")
        if name:
            _channel_cache[name] = channel["id"]
            label = "#{0}".format(name)
        else:
            label = "(direct message)"
        lines.append(
            "- {0} | id={1}{2}".format(
                label, channel["id"], " [member]" if channel.get("is_member") else ""
            )
        )
    return "\n".join(lines)


@tool(
    name="slack_read_channel",
    description=(
        "Read recent messages from a Slack channel by name ('#standup') or id. "
        "Messages are added to the twin's recall index, so you can find them "
        "later with recall. Use this to pick up commitments and deadlines the "
        "user agreed to in conversation."
    ),
    input_schema=obj(
        {
            "channel": {"type": "string", "description": "Channel name or id."},
            "limit": {"type": "integer", "description": "Messages to fetch, default 50."},
            "hours": {
                "type": "integer",
                "description": "Only messages from the last N hours. Omit for most recent.",
            },
        },
        ["channel"],
    ),
    requires="slack",
)
def slack_read_channel(args: Dict[str, Any]) -> str:
    client = slack_auth.bot_client()
    limit = min(int(args.get("limit", DEFAULT_MESSAGE_LIMIT)), MAX_MESSAGE_LIMIT)

    try:
        channel_id = resolve_channel(client, args["channel"])
        params: Dict[str, Any] = {"channel": channel_id, "limit": limit}
        if args.get("hours"):
            oldest = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=int(args["hours"]))
            params["oldest"] = str(oldest.timestamp())
        response = client.conversations_history(**params)
    except slack_auth.SlackError as exc:
        return str(exc)
    except SlackApiError as exc:
        return slack_auth.describe_api_error(exc)

    messages = response.get("messages", [])
    if not messages:
        return "No messages in that window."

    messages = list(reversed(messages))  # oldest first reads more naturally
    names = _user_names(client, messages)
    _index_messages(normalize_channel_ref(args["channel"]), messages, names)

    tz = _tz()
    return "\n".join(format_message(message, names, tz) for message in messages)


@tool(
    name="slack_read_thread",
    description=(
        "Read a full Slack thread. Pass the parent message's thread_ts, which "
        "slack_read_channel reports for messages that have replies."
    ),
    input_schema=obj(
        {
            "channel": {"type": "string"},
            "thread_ts": {"type": "string", "description": "Parent message timestamp."},
        },
        ["channel", "thread_ts"],
    ),
    requires="slack",
)
def slack_read_thread(args: Dict[str, Any]) -> str:
    client = slack_auth.bot_client()
    try:
        channel_id = resolve_channel(client, args["channel"])
        response = client.conversations_replies(
            channel=channel_id, ts=args["thread_ts"], limit=MAX_MESSAGE_LIMIT
        )
    except slack_auth.SlackError as exc:
        return str(exc)
    except SlackApiError as exc:
        return slack_auth.describe_api_error(exc)

    messages = response.get("messages", [])
    if not messages:
        return "No such thread."

    names = _user_names(client, messages)
    _index_messages(normalize_channel_ref(args["channel"]), messages, names)
    tz = _tz()
    return "\n".join(format_message(message, names, tz) for message in messages)


@tool(
    name="slack_search",
    description=(
        "Search Slack message history across the workspace. Requires a user "
        "token, which not every setup has -- if unavailable, fall back to "
        "slack_read_channel on a likely channel."
    ),
    input_schema=obj(
        {
            "query": {"type": "string", "description": "Slack search syntax works here."},
            "limit": {"type": "integer", "description": "Max results, default 20."},
        },
        ["query"],
    ),
    requires="slack",
)
def slack_search(args: Dict[str, Any]) -> str:
    try:
        client = slack_auth.user_client()
    except slack_auth.NotConnected as exc:
        return str(exc)

    try:
        response = client.search_messages(
            query=args["query"], count=min(int(args.get("limit", 20)), 100)
        )
    except SlackApiError as exc:
        return slack_auth.describe_api_error(exc)

    matches = (response.get("messages") or {}).get("matches", [])
    if not matches:
        return "No messages matched '{0}'.".format(args["query"])

    tz = _tz()
    lines = []
    for match in matches:
        channel = (match.get("channel") or {}).get("name", "?")
        lines.append(
            "- #{0} [{1}] {2}: {3}".format(
                channel,
                format_timestamp(match.get("ts", ""), tz),
                match.get("username") or "unknown",
                clean_text(match.get("text", ""))[:300],
            )
        )
    return "\n".join(lines)


@tool(
    name="slack_post_message",
    description=(
        "Post a message to a Slack channel, or reply in a thread by passing "
        "thread_ts. This is visible to other people -- the user must confirm "
        "before it runs."
    ),
    input_schema=obj(
        {
            "channel": {"type": "string", "description": "Channel name or id."},
            "text": {"type": "string", "description": "Message body."},
            "thread_ts": {
                "type": "string",
                "description": "Reply in this thread instead of the channel.",
            },
        },
        ["channel", "text"],
    ),
    write=True,
    requires="slack",
)
def slack_post_message(args: Dict[str, Any]) -> str:
    client = slack_auth.bot_client()
    try:
        channel_id = resolve_channel(client, args["channel"])
        params: Dict[str, Any] = {"channel": channel_id, "text": args["text"]}
        if args.get("thread_ts"):
            params["thread_ts"] = args["thread_ts"]
        response = client.chat_postMessage(**params)
    except slack_auth.SlackError as exc:
        return str(exc)
    except SlackApiError as exc:
        return slack_auth.describe_api_error(exc)

    return "Posted to {0} (ts {1}).".format(args["channel"], response.get("ts", "?"))
