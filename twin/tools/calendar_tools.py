"""Google Calendar adapter -- Tier 1 integration.

Two jobs. The obvious one is letting the agent view, create, and reschedule
events. The less obvious one is computing **free-time windows**, which the
proactive layer needs in order to say "you have a free evening and a stalled
project" instead of just "you have a stalled project".

Free-time windows are deliberately not stored (spec section 7) -- they're
derived from calendar gaps on demand, because any cached copy is wrong the
moment an event moves.

The interval maths is kept pure and separate from the API calls so it can be
tested without network access.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from twin import extraction
from twin.memory import store
from twin.timeutil import active_window, humanize_duration, parse_iso, zone
from twin.tools import google_auth
from twin.tools.registry import obj, tool

CALENDAR_ID = "primary"
DEFAULT_LOOKAHEAD_DAYS = 7
DEFAULT_MIN_WINDOW_MINUTES = 60

Interval = Tuple[dt.datetime, dt.datetime]


@dataclass
class Event:
    id: str
    summary: str
    start: dt.datetime
    end: dt.datetime
    all_day: bool
    location: str
    # Whether this event actually consumes the user's time. Events they
    # declined, or marked "free", show up in listings but don't block.
    blocking: bool


# --------------------------------------------------------------------------
# Pure logic
# --------------------------------------------------------------------------

def normalize_event(raw: Dict[str, Any], tz: dt.tzinfo) -> Optional[Event]:
    """Convert a Google event into our shape, or None if it should be ignored."""
    if raw.get("status") == "cancelled":
        return None

    start_field = raw.get("start") or {}
    end_field = raw.get("end") or {}

    all_day = "date" in start_field
    if all_day:
        start_date = dt.date.fromisoformat(start_field["date"])
        # Google's all-day end date is exclusive.
        end_date = dt.date.fromisoformat(end_field["date"])
        start = dt.datetime.combine(start_date, dt.time(0), tzinfo=tz)
        end = dt.datetime.combine(end_date, dt.time(0), tzinfo=tz)
    else:
        if "dateTime" not in start_field or "dateTime" not in end_field:
            return None
        start = parse_iso(start_field["dateTime"])
        end = parse_iso(end_field["dateTime"])

    blocking = raw.get("transparency") != "transparent"
    for attendee in raw.get("attendees", []) or []:
        if attendee.get("self") and attendee.get("responseStatus") == "declined":
            blocking = False
            break

    return Event(
        id=raw.get("id", ""),
        summary=raw.get("summary", "(no title)"),
        start=start,
        end=end,
        all_day=all_day,
        location=raw.get("location", "") or "",
        blocking=blocking,
    )


def merge_intervals(intervals: Sequence[Interval]) -> List[Interval]:
    """Collapse overlapping or touching intervals into a minimal set."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda pair: pair[0])
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def subtract_busy(
    window: Interval,
    busy: Sequence[Interval],
) -> List[Interval]:
    """Return the parts of `window` not covered by any busy interval."""
    window_start, window_end = window
    if window_start >= window_end:
        return []

    free: List[Interval] = []
    cursor = window_start

    for busy_start, busy_end in merge_intervals(busy):
        if busy_end <= cursor:
            continue
        if busy_start >= window_end:
            break
        if busy_start > cursor:
            free.append((cursor, min(busy_start, window_end)))
        cursor = max(cursor, busy_end)
        if cursor >= window_end:
            return free

    if cursor < window_end:
        free.append((cursor, window_end))
    return free


def compute_free_windows(
    events: Sequence[Event],
    tz: dt.tzinfo,
    active_start_hour: int,
    active_end_hour: int,
    days: int,
    now: dt.datetime,
    min_minutes: int = DEFAULT_MIN_WINDOW_MINUTES,
) -> List[Interval]:
    """Find gaps in the user's active hours over the next `days` days."""
    busy = [(event.start, event.end) for event in events if event.blocking]
    minimum = dt.timedelta(minutes=min_minutes)
    local_now = now.astimezone(tz)

    windows: List[Interval] = []
    for offset in range(days):
        day = (local_now + dt.timedelta(days=offset)).date()
        day_start, day_end = active_window(day, tz, active_start_hour, active_end_hour)

        # Never offer time that has already passed.
        if day_end <= local_now:
            continue
        day_start = max(day_start, local_now)

        for gap_start, gap_end in subtract_busy((day_start, day_end), busy):
            if gap_end - gap_start >= minimum:
                windows.append((gap_start, gap_end))

    return windows


def find_conflicts(
    events: Sequence[Event],
    start: dt.datetime,
    end: dt.datetime,
) -> List[Event]:
    """Blocking events overlapping a proposed slot."""
    return [
        event
        for event in events
        if event.blocking and event.start < end and start < event.end
    ]


# --------------------------------------------------------------------------
# API access
# --------------------------------------------------------------------------

def _profile_time_settings() -> Tuple[dt.tzinfo, int, int]:
    profile = store.get_profile()
    tz = zone(profile.get("timezone") or "UTC")
    hours = profile.get("active_hours") or {}
    return tz, int(hours.get("start", 9)), int(hours.get("end", 22))


def fetch_events(
    start: dt.datetime,
    end: dt.datetime,
    tz: Optional[dt.tzinfo] = None,
) -> List[Event]:
    """Load and normalize events in a window. Recurring events are expanded."""
    if tz is None:
        tz, _, _ = _profile_time_settings()

    service = google_auth.client("calendar")
    raw_events: List[Dict[str, Any]] = []
    page_token = None

    while True:
        response = (
            service.events()
            .list(
                calendarId=CALENDAR_ID,
                timeMin=start.astimezone(dt.timezone.utc).isoformat(),
                timeMax=end.astimezone(dt.timezone.utc).isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
                pageToken=page_token,
            )
            .execute()
        )
        raw_events.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    events = []
    for raw in raw_events:
        event = normalize_event(raw, tz)
        if event is not None:
            events.append(event)
    return events


def free_windows(
    days: int = DEFAULT_LOOKAHEAD_DAYS,
    min_minutes: int = DEFAULT_MIN_WINDOW_MINUTES,
) -> List[Interval]:
    """Free-time windows over the next `days`, per the user's own settings.

    Used by both the agent tool and the proactive scheduler.
    """
    tz, active_start, active_end = _profile_time_settings()
    now = dt.datetime.now(dt.timezone.utc)
    horizon = now + dt.timedelta(days=days + 1)
    events = fetch_events(now, horizon, tz)
    return compute_free_windows(
        events, tz, active_start, active_end, days, now, min_minutes
    )


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@tool(
    name="calendar_list_events",
    description=(
        "List the user's calendar events over the next N days, with ids, times, "
        "and whether each one actually blocks their time. Use this before "
        "suggesting when they could do something."
    ),
    input_schema=obj(
        {"days": {"type": "integer", "description": "Look-ahead window, default 7."}}
    ),
    requires="calendar",
)
def calendar_list_events(args: Dict[str, Any]) -> str:
    days = int(args.get("days", DEFAULT_LOOKAHEAD_DAYS))
    tz, _, _ = _profile_time_settings()
    now = dt.datetime.now(dt.timezone.utc)
    events = fetch_events(now, now + dt.timedelta(days=days), tz)

    if not events:
        return "No events in the next {0} days.".format(days)

    lines = []
    for event in events:
        when = (
            event.start.astimezone(tz).strftime("%a %d %b (all day)")
            if event.all_day
            else "{0}-{1}".format(
                event.start.astimezone(tz).strftime("%a %d %b %H:%M"),
                event.end.astimezone(tz).strftime("%H:%M"),
            )
        )
        lines.append(
            "- id={0} | {1} | {2}{3}{4}".format(
                event.id,
                when,
                event.summary,
                " @ {0}".format(event.location) if event.location else "",
                "" if event.blocking else " [not blocking]",
            )
        )
    return "\n".join(lines)


@tool(
    name="calendar_find_free_time",
    description=(
        "Find gaps in the user's calendar during their active hours over the "
        "next N days. Use this whenever you're proposing that they spend time "
        "on something -- ground the suggestion in a real opening rather than "
        "guessing they're free."
    ),
    input_schema=obj(
        {
            "days": {"type": "integer", "description": "Look-ahead window, default 7."},
            "min_minutes": {
                "type": "integer",
                "description": "Ignore gaps shorter than this. Default 60.",
            },
        }
    ),
    requires="calendar",
)
def calendar_find_free_time(args: Dict[str, Any]) -> str:
    days = int(args.get("days", DEFAULT_LOOKAHEAD_DAYS))
    min_minutes = int(args.get("min_minutes", DEFAULT_MIN_WINDOW_MINUTES))
    tz, active_start, active_end = _profile_time_settings()

    windows = free_windows(days, min_minutes)
    if not windows:
        return (
            "No free windows of {0}+ minutes in the next {1} days, within their "
            "active hours ({2:02d}:00-{3:02d}:00).".format(
                min_minutes, days, active_start, active_end
            )
        )

    lines = []
    for start, end in windows:
        local_start = start.astimezone(tz)
        lines.append(
            "- {0} {1}-{2} ({3} free)".format(
                local_start.strftime("%a %d %b"),
                local_start.strftime("%H:%M"),
                end.astimezone(tz).strftime("%H:%M"),
                humanize_duration(end - start),
            )
        )
    return "\n".join(lines)


@tool(
    name="calendar_check_conflicts",
    description=(
        "Check whether a proposed time slot collides with anything already on "
        "the calendar. Always run this before creating or rescheduling an event."
    ),
    input_schema=obj(
        {
            "start": {"type": "string", "description": "ISO-8601 start time."},
            "end": {"type": "string", "description": "ISO-8601 end time."},
        },
        ["start", "end"],
    ),
    requires="calendar",
)
def calendar_check_conflicts(args: Dict[str, Any]) -> str:
    try:
        start = parse_iso(args["start"])
        end = parse_iso(args["end"])
    except ValueError:
        return "Could not parse start/end. Use ISO-8601."

    tz, _, _ = _profile_time_settings()
    events = fetch_events(start - dt.timedelta(days=1), end + dt.timedelta(days=1), tz)
    conflicts = find_conflicts(events, start, end)

    if not conflicts:
        return "No conflicts -- that slot is clear."
    return "Conflicts with:\n" + "\n".join(
        "- {0} ({1}-{2})".format(
            event.summary,
            event.start.astimezone(tz).strftime("%a %d %b %H:%M"),
            event.end.astimezone(tz).strftime("%H:%M"),
        )
        for event in conflicts
    )


def deadline_candidates(events: Sequence[Event]) -> List[Event]:
    """Events that could be deadlines rather than ordinary commitments.

    All-day events are the usual shape of a "X due" entry, but a timed event
    can be one too, so the model decides. Declined and free-marked events are
    dropped first -- the user isn't obliged by something they declined.
    """
    return [event for event in events if event.blocking]


def sweep_deadlines(days: int = 30) -> List[Dict[str, Any]]:
    """Record upcoming calendar events that represent deadlines.

    Only the classify stage is needed: unlike an email, an event already
    carries its own time, so the event's start *is* the due date. That's
    correct for the "Thesis submission" shape of entry and avoids inventing a
    date the calendar already told us.
    """
    tz, _, _ = _profile_time_settings()
    now = dt.datetime.now(dt.timezone.utc)
    events = deadline_candidates(fetch_events(now, now + dt.timedelta(days=days), tz))
    if not events:
        return []

    items = [
        {
            "label": "calendar event",
            "when": event.start.astimezone(tz).isoformat(timespec="minutes"),
            "text": "{0}{1}".format(
                event.summary,
                " (all day)" if event.all_day else "",
            ),
        }
        for event in events
    ]

    recorded = []
    for index in extraction.classify(items):
        event = events[index]
        stored = store.upsert_deadline(
            description=event.summary,
            due_at=event.start,
            source="calendar",
            source_ref=event.id,
        )
        recorded.append(
            {
                "id": stored["id"],
                "description": stored["description"],
                "due_at": stored["due_at"],
            }
        )
    return recorded


@tool(
    name="calendar_extract_deadlines",
    description=(
        "Scan upcoming calendar events for ones that are actually deadlines "
        "(submissions, due dates, application closes) rather than ordinary "
        "meetings, and record them. The event's start time becomes the due "
        "date. Re-running updates rather than duplicates."
    ),
    input_schema=obj(
        {"days": {"type": "integer", "description": "How far ahead to scan. Default 30."}}
    ),
    requires="calendar",
)
def calendar_extract_deadlines(args: Dict[str, Any]) -> str:
    tz, _, _ = _profile_time_settings()
    recorded = sweep_deadlines(days=int(args.get("days", 30)))
    if not recorded:
        return "No deadline-shaped events found in that window."
    return "Recorded {0} deadline(s):\n{1}".format(
        len(recorded),
        "\n".join(
            "- #{0} {1} (due {2})".format(
                item["id"],
                item["description"],
                item["due_at"].astimezone(tz).strftime("%a %d %b %H:%M"),
            )
            for item in recorded
        ),
    )


@tool(
    name="calendar_create_event",
    description=(
        "Create an event on the user's primary calendar. Check for conflicts "
        "first. The user must confirm before this runs."
    ),
    input_schema=obj(
        {
            "summary": {"type": "string", "description": "Event title."},
            "start": {"type": "string", "description": "ISO-8601 start time."},
            "end": {"type": "string", "description": "ISO-8601 end time."},
            "description": {"type": "string"},
            "location": {"type": "string"},
        },
        ["summary", "start", "end"],
    ),
    write=True,
    requires="calendar",
)
def calendar_create_event(args: Dict[str, Any]) -> str:
    try:
        start = parse_iso(args["start"])
        end = parse_iso(args["end"])
    except ValueError:
        return "Could not parse start/end. Use ISO-8601."
    if end <= start:
        return "End time must be after start time."

    profile = store.get_profile()
    tz_name = profile.get("timezone") or "UTC"

    body: Dict[str, Any] = {
        "summary": args["summary"],
        "start": {"dateTime": start.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": end.isoformat(), "timeZone": tz_name},
    }
    if args.get("description"):
        body["description"] = args["description"]
    if args.get("location"):
        body["location"] = args["location"]

    service = google_auth.client("calendar")
    created = service.events().insert(calendarId=CALENDAR_ID, body=body).execute()

    tz = zone(tz_name)
    return "Created '{0}' on {1} (event id {2}).".format(
        created.get("summary", args["summary"]),
        start.astimezone(tz).strftime("%a %d %b %H:%M"),
        created.get("id", "?"),
    )


@tool(
    name="calendar_reschedule_event",
    description=(
        "Move an existing event to a new time, by its id from "
        "calendar_list_events. Check for conflicts at the new slot first. The "
        "user must confirm before this runs."
    ),
    input_schema=obj(
        {
            "event_id": {"type": "string"},
            "start": {"type": "string", "description": "New ISO-8601 start time."},
            "end": {"type": "string", "description": "New ISO-8601 end time."},
        },
        ["event_id", "start", "end"],
    ),
    write=True,
    requires="calendar",
)
def calendar_reschedule_event(args: Dict[str, Any]) -> str:
    try:
        start = parse_iso(args["start"])
        end = parse_iso(args["end"])
    except ValueError:
        return "Could not parse start/end. Use ISO-8601."
    if end <= start:
        return "End time must be after start time."

    profile = store.get_profile()
    tz_name = profile.get("timezone") or "UTC"

    service = google_auth.client("calendar")
    updated = (
        service.events()
        .patch(
            calendarId=CALENDAR_ID,
            eventId=args["event_id"],
            body={
                "start": {"dateTime": start.isoformat(), "timeZone": tz_name},
                "end": {"dateTime": end.isoformat(), "timeZone": tz_name},
            },
        )
        .execute()
    )

    tz = zone(tz_name)
    return "Moved '{0}' to {1}.".format(
        updated.get("summary", "(no title)"),
        start.astimezone(tz).strftime("%a %d %b %H:%M"),
    )
