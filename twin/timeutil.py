"""Shared time handling.

Calendar work is all timezone-sensitive: a "free evening" only means anything
in the user's own zone, so conversions happen here rather than being repeated
per adapter.
"""

import datetime as dt
import os
from typing import Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - zoneinfo is stdlib from 3.9
    from backports.zoneinfo import ZoneInfo  # type: ignore


def detect_timezone() -> str:
    """Best guess at the host's IANA timezone, for use as an onboarding default.

    Only a suggestion -- the user confirms or overrides it, since the agent
    must not infer this kind of thing silently.
    """
    env_tz = os.environ.get("TZ")
    if env_tz:
        try:
            ZoneInfo(env_tz)
            return env_tz
        except Exception:
            pass

    # macOS and most Linux distros symlink /etc/localtime into the tz database,
    # which is the only reliable way to recover the IANA name.
    try:
        path = os.path.realpath("/etc/localtime")
        if "zoneinfo" in path:
            candidate = path.split("zoneinfo/")[-1].lstrip("/")
            ZoneInfo(candidate)
            return candidate
    except Exception:
        pass

    return "UTC"


def parse_iso(value: str) -> dt.datetime:
    """Parse an ISO-8601 timestamp, tolerating a trailing Z and a bare date.

    Python 3.9's fromisoformat rejects the Z suffix that both models and the
    Google API emit, so normalise it rather than failing the caller.
    """
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def zone(name: str) -> dt.tzinfo:
    """Resolve a timezone name, falling back to UTC rather than raising.

    A bad profile value should degrade the quality of a suggestion, not crash
    the scheduler.
    """
    try:
        return ZoneInfo(name)
    except Exception:
        return dt.timezone.utc


def active_window(
    day: dt.date,
    tz: dt.tzinfo,
    start_hour: int,
    end_hour: int,
) -> Tuple[dt.datetime, dt.datetime]:
    """The user's available span on a given local day."""
    start = dt.datetime.combine(day, dt.time(hour=start_hour), tzinfo=tz)
    if end_hour >= 24:
        end = dt.datetime.combine(day, dt.time(hour=0), tzinfo=tz) + dt.timedelta(days=1)
    else:
        end = dt.datetime.combine(day, dt.time(hour=end_hour), tzinfo=tz)
    return start, end


def humanize_duration(delta: dt.timedelta) -> str:
    """Render a gap the way a person would say it."""
    minutes = int(delta.total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return "{0}h {1}m".format(hours, minutes)
    if hours:
        return "{0}h".format(hours)
    return "{0}m".format(minutes)
