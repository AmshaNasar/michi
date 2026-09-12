"""Shared deadline and commitment extraction.

Three sources now feed deadlines into the twin -- Slack messages, email, and
calendar events -- and they all need the same thing: ask a cheap model whether
something carries an obligation, then refuse to believe it unless the answer
is specific and confident.

Two stages, per spec section 10:

  classify()     cheap pass over metadata only, to find candidates
  extract()      full pass over the candidates' actual content

Classifying first matters because a mailbox is mostly newsletters. Running the
expensive stage over every message would be wasteful, and running the *agent*
over every message -- which is what the spec is explicitly avoiding -- more so.

Everything that interprets a model response lives here and is pure, because
the failure mode that matters is a confidently wrong deadline appearing in the
user's list.
"""

import datetime as dt
from typing import Any, Dict, List, Optional, Sequence

from twin import aux
from twin.timeutil import parse_iso

# Below this, a deadline is discarded. A missed deadline is recoverable; a
# fabricated one erodes trust in every other thing the twin says.
MIN_CONFIDENCE = 0.6

# Bounds the size of any single auxiliary call.
MAX_BATCH = 40
MAX_ITEM_CHARS = 1200

CLASSIFY_SYSTEM = """\
You triage items for a personal assistant, deciding which ones plausibly carry \
a deadline or an obligation for the user.

You are a cheap first pass, so lean slightly inclusive: it is fine to pass \
through something that turns out not to have a deadline, but do not pass \
through obvious noise.

Definitely exclude: newsletters, marketing, notifications, receipts, social \
updates, automated digests, calendar invitations with no task attached.

Include: anything stating a due date, a submission window, an application \
deadline, a payment date, a requested action with a time attached, or an \
explicit promise the user made.

Respond with ONLY a JSON array of the indices worth a closer look, e.g. [0, 4, 7].
Return [] if none qualify.
"""

EXTRACT_SYSTEM = """\
You read items and pull out deadlines that genuinely apply to the USER.

Respond with ONLY a JSON array, one object per item you are confident about. \
Omit items with no real deadline -- do not pad the array.

[{"index": 0, "description": "submit the grant application", \
"due_at": "2026-10-01T17:00:00Z", "confidence": 0.85}]

Rules:
- The obligation must fall on the user. Someone else's deadline, an FYI, or a \
general announcement is not included.
- due_at must be ISO-8601 UTC, resolved against the item's own timestamp, \
which is given to you. If you cannot determine a specific date, omit the item \
entirely -- a deadline with no date is useless here.
- description is short, concrete, and written from the user's perspective. \
Say "pay the hosting invoice", not "email about invoice".
- confidence is 0-1. Be conservative. A wrong deadline is worse than a missed \
one.
"""


# --------------------------------------------------------------------------
# Response validation (pure)
# --------------------------------------------------------------------------

def parse_index_list(payload: Any, count: int) -> List[int]:
    """Validate a classifier response into in-range, de-duplicated indices."""
    if not isinstance(payload, list):
        return []

    seen = set()
    indices = []
    for entry in payload:
        # Tolerate [{"index": 3}] as well as a bare [3].
        if isinstance(entry, dict):
            entry = entry.get("index")
        try:
            index = int(entry)
        except (TypeError, ValueError):
            continue
        if 0 <= index < count and index not in seen:
            seen.add(index)
            indices.append(index)
    return indices


def parse_extraction(payload: Any) -> Optional[Dict[str, Any]]:
    """Validate one extracted deadline. Returns None for anything unusable."""
    if not isinstance(payload, dict):
        return None

    # Slack's single-message extractor uses this flag; batch entries omit it.
    if "has_commitment" in payload and not payload.get("has_commitment"):
        return None

    description = (payload.get("description") or "").strip()
    if not description:
        return None

    try:
        confidence = float(payload.get("confidence", 0))
    except (TypeError, ValueError):
        return None
    if confidence < MIN_CONFIDENCE:
        return None

    raw_due = payload.get("due_at")
    if not raw_due:
        return None
    try:
        due_at = parse_iso(str(raw_due))
    except (ValueError, TypeError):
        return None

    return {"description": description, "due_at": due_at, "confidence": confidence}


def parse_extraction_batch(payload: Any, count: int) -> Dict[int, Dict[str, Any]]:
    """Validate a batch extraction response into {index: deadline}."""
    if not isinstance(payload, list):
        return {}

    results: Dict[int, Dict[str, Any]] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        if not (0 <= index < count) or index in results:
            continue
        parsed = parse_extraction(entry)
        if parsed is not None:
            results[index] = parsed
    return results


# --------------------------------------------------------------------------
# Rendering (pure)
# --------------------------------------------------------------------------

def render_items(items: Sequence[Dict[str, Any]], max_chars: int = MAX_ITEM_CHARS) -> str:
    """Render items for a model call.

    Each item is {"label": ..., "when": ..., "text": ...}; label and when are
    optional. The index is what the model refers back to.
    """
    blocks = []
    for index, item in enumerate(items):
        header = "[{0}]".format(index)
        if item.get("label"):
            header += " {0}".format(item["label"])
        if item.get("when"):
            header += " | sent: {0}".format(item["when"])
        blocks.append("{0}\n{1}".format(header, (item.get("text") or "")[:max_chars]))
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------
# Model calls
# --------------------------------------------------------------------------

def classify(items: Sequence[Dict[str, Any]]) -> List[int]:
    """Cheap first pass: which items are worth reading in full?"""
    if not items:
        return []

    batch = list(items)[:MAX_BATCH]
    try:
        payload = aux.complete_json(
            CLASSIFY_SYSTEM, render_items(batch, max_chars=400), max_tokens=500
        )
    except aux.AuxError:
        # A failed classifier means no candidates, not all candidates -- the
        # expensive stage must never run on an unfiltered mailbox.
        return []
    return parse_index_list(payload, len(batch))


def extract(items: Sequence[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """Second pass: pull concrete deadlines out of candidate items."""
    if not items:
        return {}

    batch = list(items)[:MAX_BATCH]
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    prompt = "Current time (UTC): {0}\n\nItems:\n\n{1}".format(now, render_items(batch))

    try:
        payload = aux.complete_json(EXTRACT_SYSTEM, prompt, max_tokens=2000)
    except aux.AuxError:
        return {}
    return parse_extraction_batch(payload, len(batch))


def extract_one(text: str, sent_at: Optional[dt.datetime] = None) -> Optional[Dict[str, Any]]:
    """Extract a deadline from a single item. Used by the Slack listener."""
    when = (sent_at or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
    found = extract([{"when": when, "text": text}])
    return found.get(0)
