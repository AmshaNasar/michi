"""Proactive checks.

Runs as a scheduled job, not as a second agent (spec section 5). It only
detects and queues; the agent core does the talking, so nudges arrive in the
persona's voice rather than as canned notification strings.
"""

import datetime as dt
from typing import List, Optional

from twin.config import SETTINGS
from twin.memory import store
from twin.timeutil import humanize_duration, zone

# How far ahead a deadline has to be before it stops being urgent.
DEADLINE_HORIZON_DAYS = 3

# A window starting at or after this local hour counts as an "evening" -- the
# kind of opening worth offering for a personal project.
EVENING_FROM_HOUR = 17
FREE_TIME_LOOKAHEAD_DAYS = 7
FREE_TIME_MIN_MINUTES = 60

# Opportunity discovery costs real API credits on every run, so it scans far
# less often than the free local checks.
OPPORTUNITY_COOLDOWN_HOURS = 12
OPPORTUNITY_LOOKAHEAD_DAYS = 30
MAX_OPPORTUNITY_NUDGES = 2

# The deadline sweep also costs model calls, though far fewer than discovery
# because the cheap classifier filters before anything expensive runs.
DEADLINE_SCAN_COOLDOWN_HOURS = 2
# First run has no cursor, so it looks back this far rather than at everything.
DEADLINE_FIRST_SCAN_DAYS = 3
# A stale cursor would make one run scan months of mail; clamp the catch-up.
DEADLINE_MAX_CATCHUP_DAYS = 14
CALENDAR_DEADLINE_LOOKAHEAD_DAYS = 30


def check_stale_projects() -> int:
    """Queue a nudge for each active project past the user's threshold."""
    profile = store.get_profile()
    threshold = profile["staleness_threshold_days"]
    queued = 0

    for project in store.stale_projects(threshold):
        store.queue_nudge(
            kind="staleness",
            message=(
                "'{0}' hasn't moved in {1} days (your threshold is {2}). "
                "Ask whether they want to block time for it or park it."
            ).format(project["name"], project["days_stale"], threshold),
            evidence={
                "project": project["name"],
                "days_stale": project["days_stale"],
                "threshold_days": threshold,
                "last_activity_at": project["last_activity_at"].isoformat(),
            },
        )
        queued += 1
    return queued


def check_deadlines() -> int:
    """Queue a nudge for anything due inside the urgency horizon."""
    now = dt.datetime.now(dt.timezone.utc)
    queued = 0

    for deadline in store.list_open_deadlines(within_days=DEADLINE_HORIZON_DAYS):
        hours_left = (deadline["due_at"] - now).total_seconds() / 3600
        if hours_left < 0:
            urgency = "is {0:.0f} hours OVERDUE".format(abs(hours_left))
        else:
            urgency = "is due in {0:.0f} hours".format(hours_left)

        store.queue_nudge(
            kind="deadline",
            message="'{0}' {1}.".format(deadline["description"], urgency),
            evidence={
                "deadline_id": deadline["id"],
                "description": deadline["description"],
                "due_at": deadline["due_at"].isoformat(),
                "source": deadline["source"],
            },
        )
        queued += 1
    return queued


def check_free_time_for_projects() -> int:
    """Pair real calendar openings with projects that have gone quiet.

    This is the nudge the persona exists for: not "you have a stale project"
    but "you have three free evenings and a stale project". Both halves are
    real tracked data, so the agent can cite them.
    """
    profile = store.get_profile()
    if "calendar" not in (profile.get("connected_apps") or []):
        return 0

    stale = store.stale_projects()
    if not stale:
        return 0

    # Imported lazily so the scheduler still runs on a machine where the
    # Google client libraries aren't importable.
    from twin.tools import calendar_tools
    from twin.tools.google_auth import NotConnected

    try:
        windows = calendar_tools.free_windows(
            days=FREE_TIME_LOOKAHEAD_DAYS, min_minutes=FREE_TIME_MIN_MINUTES
        )
    except NotConnected:
        return 0

    if not windows:
        return 0

    tz = zone(profile.get("timezone") or "UTC")
    evenings = [
        (start, end)
        for start, end in windows
        if start.astimezone(tz).hour >= EVENING_FROM_HOUR
    ]
    # Fall back to any opening if their calendar has no free evenings.
    candidates = evenings or windows
    soonest = min(candidates, key=lambda pair: pair[0])
    project = stale[0]  # stale_projects() returns oldest-first

    local_start = soonest[0].astimezone(tz)
    store.queue_nudge(
        kind="free_time",
        message=(
            "They have {0} free {1} in the next {2} days, and '{3}' hasn't "
            "moved in {4} days. Soonest opening: {5} at {6} ({7} free). Offer "
            "to block time for it."
        ).format(
            len(candidates),
            "evening(s)" if evenings else "window(s)",
            FREE_TIME_LOOKAHEAD_DAYS,
            project["name"],
            project["days_stale"],
            local_start.strftime("%a %d %b"),
            local_start.strftime("%H:%M"),
            humanize_duration(soonest[1] - soonest[0]),
        ),
        evidence={
            "project": project["name"],
            "days_stale": project["days_stale"],
            "free_window_count": len(candidates),
            "are_evenings": bool(evenings),
            "soonest_start": soonest[0].isoformat(),
            "soonest_end": soonest[1].isoformat(),
        },
    )
    return 1


def _free_time_summary(profile: dict) -> Optional[str]:
    """A short description of the user's next openings, or None if they're full.

    Returns None only when the calendar is connected and genuinely has no
    room -- a user who hasn't connected a calendar gets an empty string, so
    discovery still runs but the nudge won't claim to know their schedule.
    """
    if "calendar" not in (profile.get("connected_apps") or []):
        return ""

    from twin.tools import calendar_tools
    from twin.tools.google_auth import NotConnected

    try:
        windows = calendar_tools.free_windows(
            days=FREE_TIME_LOOKAHEAD_DAYS, min_minutes=FREE_TIME_MIN_MINUTES
        )
    except NotConnected:
        return ""
    except Exception:
        return ""

    if not windows:
        return None

    tz = zone(profile.get("timezone") or "UTC")
    soonest = min(windows, key=lambda pair: pair[0])
    local_start = soonest[0].astimezone(tz)
    return "{0} free window(s) in the next {1} days, soonest {2} at {3} ({4} free)".format(
        len(windows),
        FREE_TIME_LOOKAHEAD_DAYS,
        local_start.strftime("%a %d %b"),
        local_start.strftime("%H:%M"),
        humanize_duration(soonest[1] - soonest[0]),
    )


def _opportunity_scan_due(profile: dict, now: dt.datetime) -> bool:
    last = profile.get("last_opportunity_scan")
    if last is None:
        return True
    return (now - last) >= dt.timedelta(hours=OPPORTUNITY_COOLDOWN_HOURS)


def check_opportunities() -> int:
    """Match the user's interests against real upcoming opportunities.

    Gated on a cooldown because, unlike the other checks, this one spends API
    credits every time it runs.
    """
    if not SETTINGS.exa_api_key:
        return 0

    profile = store.get_profile()
    interests = list(profile.get("interests") or [])
    if not interests:
        return 0

    now = dt.datetime.now(dt.timezone.utc)
    if not _opportunity_scan_due(profile, now):
        return 0

    free_summary = _free_time_summary(profile)
    if free_summary is None:
        # Calendar is connected and there's no room -- nothing to offer them.
        return 0

    from twin.tools import opportunity_tools

    try:
        results = opportunity_tools.discover(
            interests=interests,
            location=profile.get("location") or "",
            days=OPPORTUNITY_LOOKAHEAD_DAYS,
            limit=MAX_OPPORTUNITY_NUDGES,
        )
    except opportunity_tools.DiscoveryUnavailable:
        return 0
    except Exception:
        return 0

    # Record the scan even when it finds nothing, so a quiet week doesn't mean
    # re-querying Exa on every tick.
    store.update_profile(last_opportunity_scan=now)

    queued = 0
    for result in results:
        store.queue_nudge(
            kind="opportunity",
            message=(
                "'{0}' ({1}) matches their interest in {2}. {3}{4} Link: {5}. "
                "Offer it directly -- say what it is and why it fits."
            ).format(
                result["title"],
                result.get("kind", "opportunity"),
                ", ".join(interests[:3]),
                result.get("reason", ""),
                " They have {0}.".format(free_summary) if free_summary else "",
                result["url"],
            ),
            evidence={
                "title": result["title"],
                "url": result["url"],
                "score": result.get("score"),
                "kind": result.get("kind"),
                "when": result.get("when"),
                "matched_interests": interests[:3],
                "free_time": free_summary,
            },
        )
        opportunity_tools.mark_surfaced(result)
        queued += 1

    return queued


def sweep_window(profile: dict, now: dt.datetime) -> Optional[dt.datetime]:
    """The `after:` cursor for the mail sweep, or None to use the day window.

    A cursor older than the catch-up limit is clamped, so a twin left off for a
    month doesn't try to classify a month of mail in one run.
    """
    last = profile.get("last_deadline_scan")
    if last is None:
        return None
    floor = now - dt.timedelta(days=DEADLINE_MAX_CATCHUP_DAYS)
    return max(last, floor)


def check_extracted_deadlines() -> int:
    """Sweep email and calendar for deadlines the user hasn't recorded.

    Spec section 2 lists this as a core capability; it runs here rather than in
    the agent loop so the expensive model never sees raw mail.
    """
    profile = store.get_profile()
    connected = profile.get("connected_apps") or []
    now = dt.datetime.now(dt.timezone.utc)

    last = profile.get("last_deadline_scan")
    if last is not None and (now - last) < dt.timedelta(hours=DEADLINE_SCAN_COOLDOWN_HOURS):
        return 0

    found = 0
    ran = False

    if "gmail" in connected:
        from twin.tools import gmail_tools
        from twin.tools.google_auth import NotConnected

        try:
            found += len(
                gmail_tools.sweep_deadlines(
                    since=sweep_window(profile, now), days=DEADLINE_FIRST_SCAN_DAYS
                )
            )
            ran = True
        except NotConnected:
            pass
        except Exception:
            # One failing source must not block the other.
            pass

    if "calendar" in connected:
        from twin.tools import calendar_tools
        from twin.tools.google_auth import NotConnected

        try:
            found += len(
                calendar_tools.sweep_deadlines(days=CALENDAR_DEADLINE_LOOKAHEAD_DAYS)
            )
            ran = True
        except NotConnected:
            pass
        except Exception:
            pass

    if "todoist" in connected:
        # No model call here: a Todoist task already carries a structured
        # date, so this is a straight import rather than an extraction.
        from twin.tools import todoist_auth, todoist_tools

        try:
            tasks = todoist_tools.fetch_tasks()
            found += len(todoist_tools.sync_deadlines(tasks, zone(profile.get("timezone") or "UTC")))
            ran = True
        except todoist_auth.NotConnected:
            pass
        except Exception:
            pass

    # Only advance the cursor if a source actually ran, or a failed sweep would
    # silently skip the mail it never managed to read.
    if ran:
        store.update_profile(last_deadline_scan=now)

    return found


def check_gmail_push() -> str:
    """Renew the Gmail watch and drain pending push notifications.

    Spec section 6 puts watch renewal on the polling scheduler precisely
    because a Gmail watch lapses silently after 7 days; nothing would surface
    the failure except mail quietly no longer arriving.
    """
    if not SETTINGS.gmail_push_enabled:
        return "not configured"

    from twin.tools import gmail_watch
    from twin.tools.google_auth import NotConnected

    renewed = False
    try:
        renewed = gmail_watch.renew_if_needed()
    except (NotConnected, gmail_watch.PushNotConfigured):
        return "not connected"
    except Exception as exc:
        return "renewal failed ({0})".format(type(exc).__name__)

    try:
        result = gmail_watch.process_notifications()
    except Exception as exc:
        # Un-acked notifications redeliver, so a failure here loses nothing.
        return "{0}pull failed ({1})".format(
            "renewed, " if renewed else "", type(exc).__name__
        )

    parts = []
    if renewed:
        parts.append("watch renewed")
    if result["recovered"]:
        parts.append("history cursor expired, re-swept")
    parts.append(
        "{0} notification(s), {1} message(s), {2} deadline(s)".format(
            result["pulled"], result["messages"], result["deadlines"]
        )
    )
    return "; ".join(parts)


def run_once() -> List[str]:
    """Run every proactive check. Returns a short report per check."""
    report = []
    # Push is drained before the sweep so newly arrived mail is included in
    # this tick's deadline checks rather than the next one's.
    report.append("gmail push: {0}".format(check_gmail_push()))
    # Extraction runs first so anything it finds can be nudged on this same
    # tick rather than waiting for the next one.
    report.append(
        "extracted deadlines: {0} recorded".format(check_extracted_deadlines())
    )
    report.append("stale projects: {0} nudge(s) queued".format(check_stale_projects()))
    report.append("deadlines: {0} nudge(s) queued".format(check_deadlines()))
    report.append(
        "free time x projects: {0} nudge(s) queued".format(check_free_time_for_projects())
    )
    report.append("opportunities: {0} nudge(s) queued".format(check_opportunities()))
    return report


def run_forever(interval_minutes: int = 30) -> None:
    """Block, running the proactive checks on an interval."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler()
    scheduler.add_job(run_once, "interval", minutes=interval_minutes, next_run_time=dt.datetime.now())
    scheduler.start()
