"""Gmail adapter -- Tier 1 integration.

Read tools run unattended. The one write tool creates a *draft* and is gated
on explicit confirmation; nothing in v1 sends mail autonomously.
"""

import base64
import datetime as dt
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Sequence, Tuple

from twin import extraction
from twin.memory import embeddings, store
from twin.tools import google_auth
from twin.tools.registry import obj, tool

MAX_BODY_CHARS = 4000

# Cheap server-side filtering before a single token is spent. Promotions,
# social, and forum mail is where newsletters live, and chats aren't email.
SWEEP_EXCLUSIONS = "-category:promotions -category:social -category:forums -in:chats"

# Bounds one sweep. A backlog larger than this is handled by the next run.
SWEEP_MAX_MESSAGES = 40


def _header(payload: Dict[str, Any], name: str) -> str:
    for header in payload.get("headers", []):
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


def _extract_body(payload: Dict[str, Any]) -> str:
    """Walk the MIME tree for the best available text representation."""
    plain: List[str] = []
    html: List[str] = []

    def walk(part: Dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data:
            if mime == "text/plain":
                plain.append(_decode(data))
            elif mime == "text/html":
                html.append(_decode(data))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)

    if plain:
        return "\n".join(plain)
    if html:
        # Crude tag strip -- enough for the agent to reason over without
        # pulling in an HTML parser dependency.
        import re

        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", "\n".join(html), flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"[ \t]{2,}", " ", text)
    return ""


def _index_message(message_id: str, subject: str, sender: str, body: str) -> None:
    """Add the message to the recall index so the twin can find it later."""
    content = "From: {0}\nSubject: {1}\n\n{2}".format(sender, subject, body[:MAX_BODY_CHARS])
    vector = None
    try:
        vector = embeddings.embed(content)
    except Exception:
        # Indexing is best-effort; a failing embedding provider must not break
        # the user's ability to read their mail.
        pass
    store.store_embedding(
        kind="email",
        ref_id=message_id,
        content=content,
        vector=vector,
        metadata={"subject": subject, "from": sender},
    )


@tool(
    name="gmail_search",
    description=(
        "Search the user's Gmail using native Gmail query syntax (e.g. "
        "'is:unread newer_than:3d', 'from:someone@x.com has:attachment'). "
        "Returns message ids with sender, subject, date, and snippet. Use "
        "gmail_read to get the full body of anything that looks relevant."
    ),
    input_schema=obj(
        {
            "query": {"type": "string", "description": "Gmail search query."},
            "max_results": {"type": "integer", "description": "Default 10, max 50."},
        },
        ["query"],
    ),
    requires="gmail",
)
def gmail_search(args: Dict[str, Any]) -> str:
    service = google_auth.client("gmail")
    max_results = min(int(args.get("max_results", 10)), 50)

    listing = (
        service.users()
        .messages()
        .list(userId="me", q=args["query"], maxResults=max_results)
        .execute()
    )
    message_ids = [m["id"] for m in listing.get("messages", [])]
    if not message_ids:
        return "No messages matched '{0}'.".format(args["query"])

    lines = []
    for message_id in message_ids:
        message = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        payload = message.get("payload", {})
        lines.append(
            "- id={0} | from: {1} | subject: {2} | {3}\n  {4}".format(
                message_id,
                _header(payload, "From"),
                _header(payload, "Subject") or "(no subject)",
                _header(payload, "Date"),
                message.get("snippet", "").replace("\n", " ")[:200],
            )
        )
    return "\n".join(lines)


@tool(
    name="gmail_read",
    description=(
        "Fetch the full body of one Gmail message by id. The message is also "
        "added to the twin's recall index, so you can find it later with recall."
    ),
    input_schema=obj({"message_id": {"type": "string"}}, ["message_id"]),
    requires="gmail",
)
def gmail_read(args: Dict[str, Any]) -> str:
    service = google_auth.client("gmail")
    message = (
        service.users()
        .messages()
        .get(userId="me", id=args["message_id"], format="full")
        .execute()
    )
    payload = message.get("payload", {})
    subject = _header(payload, "Subject") or "(no subject)"
    sender = _header(payload, "From")
    date = _header(payload, "Date")
    body = _extract_body(payload).strip()

    _index_message(args["message_id"], subject, sender, body)

    truncated = body[:MAX_BODY_CHARS]
    if len(body) > MAX_BODY_CHARS:
        truncated += "\n\n[...truncated, {0} chars total]".format(len(body))

    return "From: {0}\nDate: {1}\nSubject: {2}\nThread: {3}\n\n{4}".format(
        sender, date, subject, message.get("threadId", ""), truncated or "(empty body)"
    )


def _reply_headers(service, message_id: str) -> Tuple[str, str, str, Optional[str]]:
    """Pull the To / Subject / thread / Message-ID needed to thread a reply."""
    original = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Message-ID", "Reply-To"],
        )
        .execute()
    )
    payload = original.get("payload", {})
    to_address = _header(payload, "Reply-To") or _header(payload, "From")
    subject = _header(payload, "Subject") or ""
    if not subject.lower().startswith("re:"):
        subject = "Re: {0}".format(subject)
    return to_address, subject, original.get("threadId", ""), _header(payload, "Message-ID")


@tool(
    name="gmail_draft_reply",
    description=(
        "Create a threaded DRAFT reply to a Gmail message. This saves a draft "
        "in the user's Gmail; it does NOT send. The user must confirm before "
        "this runs, and will send it themselves."
    ),
    input_schema=obj(
        {
            "message_id": {"type": "string", "description": "Message being replied to."},
            "body": {"type": "string", "description": "Full reply text."},
        },
        ["message_id", "body"],
    ),
    write=True,
    requires="gmail",
)
def gmail_draft_reply(args: Dict[str, Any]) -> str:
    service = google_auth.client("gmail")
    to_address, subject, thread_id, message_id_header = _reply_headers(
        service, args["message_id"]
    )

    mail = EmailMessage()
    mail["To"] = to_address
    mail["Subject"] = subject
    if message_id_header:
        # Proper threading in the recipient's client.
        mail["In-Reply-To"] = message_id_header
        mail["References"] = message_id_header
    mail.set_content(args["body"])

    raw = base64.urlsafe_b64encode(mail.as_bytes()).decode("utf-8")
    draft = (
        service.users()
        .drafts()
        .create(userId="me", body={"message": {"raw": raw, "threadId": thread_id}})
        .execute()
    )
    return (
        "Draft saved to Gmail (draft id {0}), addressed to {1} with subject "
        "'{2}'. Not sent -- it's waiting in the user's Drafts.".format(
            draft["id"], to_address, subject
        )
    )


def build_sweep_query(since: Optional[dt.datetime] = None, days: int = 3) -> str:
    """Gmail query for the sweep window.

    Prefers an explicit `after:` cursor from the last scan so repeat runs only
    look at genuinely new mail; falls back to a day window on first run.
    """
    if since is not None:
        window = "after:{0}".format(int(since.timestamp()))
    else:
        window = "newer_than:{0}d".format(max(int(days), 1))
    return "{0} {1}".format(window, SWEEP_EXCLUSIONS)


def _message_metadata(service, message_id: str) -> Dict[str, Any]:
    message = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        )
        .execute()
    )
    payload = message.get("payload", {})
    return {
        "id": message_id,
        "from": _header(payload, "From"),
        "subject": _header(payload, "Subject") or "(no subject)",
        "date": _header(payload, "Date"),
        "snippet": message.get("snippet", ""),
    }


def sweep_deadlines(
    since: Optional[dt.datetime] = None,
    days: int = 3,
    max_messages: int = SWEEP_MAX_MESSAGES,
) -> List[Dict[str, Any]]:
    """Find and record deadlines in recent mail, by polling a time window."""
    service = google_auth.client("gmail")

    listing = (
        service.users()
        .messages()
        .list(userId="me", q=build_sweep_query(since, days), maxResults=max_messages)
        .execute()
    )
    message_ids = [m["id"] for m in listing.get("messages", [])]
    return process_message_ids(message_ids, service=service)


def process_message_ids(
    message_ids: Sequence[str],
    service: Any = None,
) -> List[Dict[str, Any]]:
    """Run the two-stage deadline pipeline over specific messages.

    Shared by the polling sweep and the Pub/Sub push path, which differ only
    in how they decide *which* messages to look at.

    Two stages (spec section 10): a cheap classifier reads only sender,
    subject, and snippet to pick candidates, then the full bodies of those
    candidates go to the extractor. A mailbox is mostly newsletters, so
    fetching and extracting every body would be mostly waste.

    Re-recording is harmless -- `deadlines` is unique on (source, source_ref),
    so an overlapping window updates rather than duplicates.
    """
    if not message_ids:
        return []
    if service is None:
        service = google_auth.client("gmail")

    metadata = []
    for message_id in message_ids:
        try:
            metadata.append(_message_metadata(service, message_id))
        except Exception:
            # A message deleted between notification and read is normal.
            continue
    if not metadata:
        return []

    candidates = extraction.classify(
        [
            {
                "label": "from: {0} | subject: {1}".format(item["from"], item["subject"]),
                "when": item["date"],
                "text": item["snippet"],
            }
            for item in metadata
        ]
    )
    if not candidates:
        return []

    items = []
    for index in candidates:
        item = metadata[index]
        try:
            full = (
                service.users()
                .messages()
                .get(userId="me", id=item["id"], format="full")
                .execute()
            )
            body = _extract_body(full.get("payload", {})).strip()
        except Exception:
            # A message we can't read shouldn't sink the sweep.
            body = item["snippet"]
        items.append(
            {
                "id": item["id"],
                "label": "from: {0} | subject: {1}".format(item["from"], item["subject"]),
                "when": item["date"],
                "text": body or item["snippet"],
            }
        )

    found = extraction.extract(items)

    recorded = []
    for index, deadline in found.items():
        stored = store.upsert_deadline(
            description=deadline["description"],
            due_at=deadline["due_at"],
            source="gmail",
            source_ref=items[index]["id"],
        )
        recorded.append(
            {
                "id": stored["id"],
                "description": stored["description"],
                "due_at": stored["due_at"],
                "confidence": deadline["confidence"],
                "subject": items[index]["label"],
            }
        )
    return recorded


@tool(
    name="gmail_extract_deadlines",
    description=(
        "Scan recent email for deadlines and action items and record them "
        "automatically. Use when the user asks you to check their mail for "
        "anything they owe, or after a period away. Already-recorded deadlines "
        "are updated rather than duplicated."
    ),
    input_schema=obj(
        {"days": {"type": "integer", "description": "How far back to scan. Default 3."}}
    ),
    requires="gmail",
)
def gmail_extract_deadlines(args: Dict[str, Any]) -> str:
    recorded = sweep_deadlines(days=int(args.get("days", 3)))
    if not recorded:
        return "No new deadlines found in that window."
    return "Recorded {0} deadline(s):\n{1}".format(
        len(recorded),
        "\n".join(
            "- #{0} {1} (due {2}, confidence {3:.0%}) from {4}".format(
                item["id"],
                item["description"],
                item["due_at"].isoformat(timespec="minutes"),
                item["confidence"],
                item["subject"],
            )
            for item in recorded
        ),
    )


@tool(
    name="gmail_triage",
    description=(
        "Summarize the user's recent unread mail into what needs action, what "
        "carries a deadline, and what is noise. Use for 'what's in my inbox' "
        "style requests. Read the messages it flags before drawing conclusions."
    ),
    input_schema=obj(
        {"days": {"type": "integer", "description": "Look-back window, default 3."}}
    ),
    requires="gmail",
)
def gmail_triage(args: Dict[str, Any]) -> str:
    days = int(args.get("days", 3))
    return gmail_search(
        {"query": "is:unread newer_than:{0}d".format(days), "max_results": 25}
    )
