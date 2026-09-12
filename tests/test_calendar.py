"""Calendar interval logic.

All pure -- no API access -- because the free-time computation is where the
subtle bugs live (past windows, all-day events, declined invitations, gaps
that straddle a busy block).
"""

import datetime as dt

from zoneinfo import ZoneInfo

from twin.tools.calendar_tools import (
    Event,
    compute_free_windows,
    find_conflicts,
    merge_intervals,
    normalize_event,
    subtract_busy,
)

UTC = dt.timezone.utc
LAGOS = ZoneInfo("Africa/Lagos")  # UTC+1, no DST


def at(hour, minute=0, day=12, tz=UTC):
    return dt.datetime(2026, 9, day, hour, minute, tzinfo=tz)


def event(start, end, blocking=True, all_day=False, summary="thing"):
    return Event(
        id="e",
        summary=summary,
        start=start,
        end=end,
        all_day=all_day,
        location="",
        blocking=blocking,
    )


# --- merge_intervals ------------------------------------------------------

def test_merge_collapses_overlaps_and_touching():
    merged = merge_intervals(
        [(at(10), at(11)), (at(10, 30), at(12)), (at(12), at(13)), (at(15), at(16))]
    )
    assert merged == [(at(10), at(13)), (at(15), at(16))]


def test_merge_handles_unsorted_input():
    merged = merge_intervals([(at(15), at(16)), (at(9), at(10))])
    assert merged == [(at(9), at(10)), (at(15), at(16))]


def test_merge_absorbs_fully_contained_interval():
    merged = merge_intervals([(at(9), at(17)), (at(11), at(12))])
    assert merged == [(at(9), at(17))]


# --- subtract_busy --------------------------------------------------------

def test_subtract_nothing_busy_returns_whole_window():
    assert subtract_busy((at(9), at(17)), []) == [(at(9), at(17))]


def test_subtract_splits_around_a_meeting():
    free = subtract_busy((at(9), at(17)), [(at(12), at(13))])
    assert free == [(at(9), at(12)), (at(13), at(17))]


def test_subtract_busy_covering_window_leaves_nothing():
    assert subtract_busy((at(9), at(17)), [(at(8), at(18))]) == []


def test_subtract_trims_leading_and_trailing_busy():
    free = subtract_busy((at(9), at(17)), [(at(8), at(10)), (at(16), at(19))])
    assert free == [(at(10), at(16))]


def test_subtract_ignores_busy_outside_window():
    free = subtract_busy((at(9), at(17)), [(at(6), at(7)), (at(20), at(21))])
    assert free == [(at(9), at(17))]


def test_subtract_empty_window_returns_nothing():
    assert subtract_busy((at(9), at(9)), []) == []


# --- compute_free_windows -------------------------------------------------

def test_free_windows_clamp_to_now():
    """Time already past today must never be offered as free."""
    windows = compute_free_windows(
        events=[], tz=UTC, active_start_hour=9, active_end_hour=22,
        days=1, now=at(14), min_minutes=30,
    )
    assert windows == [(at(14), at(22))]


def test_free_windows_skip_a_day_that_is_already_over():
    windows = compute_free_windows(
        events=[], tz=UTC, active_start_hour=9, active_end_hour=17,
        days=2, now=at(19), min_minutes=30,
    )
    # Today's active window ended at 17:00, so only tomorrow is offered.
    assert windows == [(at(9, day=13), at(17, day=13))]


def test_free_windows_respect_minimum_duration():
    events = [event(at(11), at(12))]
    windows = compute_free_windows(
        events=events, tz=UTC, active_start_hour=10, active_end_hour=22,
        days=1, now=at(10), min_minutes=120,
    )
    # The 10:00-11:00 gap is only an hour, so it's dropped.
    assert windows == [(at(12), at(22))]


def test_declined_and_transparent_events_do_not_block():
    events = [event(at(12), at(18), blocking=False)]
    windows = compute_free_windows(
        events=events, tz=UTC, active_start_hour=9, active_end_hour=22,
        days=1, now=at(9), min_minutes=30,
    )
    assert windows == [(at(9), at(22))]


def test_all_day_event_consumes_the_whole_day():
    all_day = event(
        dt.datetime(2026, 9, 12, 0, tzinfo=UTC),
        dt.datetime(2026, 9, 13, 0, tzinfo=UTC),
        all_day=True,
    )
    windows = compute_free_windows(
        events=[all_day], tz=UTC, active_start_hour=9, active_end_hour=22,
        days=1, now=at(9), min_minutes=30,
    )
    assert windows == []


def test_free_windows_use_the_users_timezone():
    """Active hours are local hours, not UTC hours."""
    windows = compute_free_windows(
        events=[], tz=LAGOS, active_start_hour=18, active_end_hour=22,
        days=1, now=at(6), min_minutes=60,
    )
    assert len(windows) == 1
    start, end = windows[0]
    assert start.astimezone(LAGOS).hour == 18
    assert end.astimezone(LAGOS).hour == 22
    # 18:00 in Lagos is 17:00 UTC.
    assert start.astimezone(UTC).hour == 17


# --- normalize_event ------------------------------------------------------

def test_normalize_skips_cancelled():
    assert normalize_event({"status": "cancelled", "id": "x"}, UTC) is None


def test_normalize_marks_declined_as_non_blocking():
    raw = {
        "id": "x",
        "summary": "standup",
        "start": {"dateTime": "2026-09-12T10:00:00Z"},
        "end": {"dateTime": "2026-09-12T10:30:00Z"},
        "attendees": [{"self": True, "responseStatus": "declined"}],
    }
    assert normalize_event(raw, UTC).blocking is False


def test_normalize_marks_transparent_as_non_blocking():
    raw = {
        "id": "x",
        "summary": "reminder",
        "start": {"dateTime": "2026-09-12T10:00:00Z"},
        "end": {"dateTime": "2026-09-12T10:30:00Z"},
        "transparency": "transparent",
    }
    assert normalize_event(raw, UTC).blocking is False


def test_normalize_treats_all_day_end_date_as_exclusive():
    raw = {
        "id": "x",
        "summary": "conference",
        "start": {"date": "2026-09-12"},
        "end": {"date": "2026-09-14"},
    }
    parsed = normalize_event(raw, UTC)
    assert parsed.all_day is True
    # Two full days: the 12th and the 13th.
    assert (parsed.end - parsed.start) == dt.timedelta(days=2)


def test_normalize_skips_event_without_times():
    assert normalize_event({"id": "x", "start": {}, "end": {}}, UTC) is None


# --- find_conflicts -------------------------------------------------------

def test_find_conflicts_detects_partial_overlap():
    events = [event(at(11), at(12), summary="lunch")]
    assert [e.summary for e in find_conflicts(events, at(11, 30), at(13))] == ["lunch"]


def test_find_conflicts_ignores_adjacent_slots():
    """An event ending exactly when the new one starts is not a conflict."""
    events = [event(at(11), at(12))]
    assert find_conflicts(events, at(12), at(13)) == []


def test_find_conflicts_ignores_non_blocking_events():
    events = [event(at(11), at(13), blocking=False)]
    assert find_conflicts(events, at(11, 30), at(12)) == []
