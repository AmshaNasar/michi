"""Todoist adapter: due-date resolution, recurring filtering, pagination.

Three things here are easy to get quietly wrong and expensive when wrong:

* Todoist's `deadline` and `due` are independent fields with different
  meanings, and picking the wrong one gives the user the wrong date.
* A date with no time means "end of that day" locally, not midnight UTC --
  getting this wrong shifts every all-day deadline by up to a day.
* List endpoints are cursor-paginated, so ignoring `next_cursor` silently
  imports only the first page of someone's tasks.
"""

import datetime as dt

from zoneinfo import ZoneInfo

from twin.tools import todoist_auth
from twin.tools import todoist_tools as tt

LAGOS = ZoneInfo("Africa/Lagos")  # UTC+1, no DST
UTC = dt.timezone.utc


def task(**overrides):
    payload = {"id": "t1", "content": "write the report", "priority": 1}
    payload.update(overrides)
    return payload


# --- due-date resolution --------------------------------------------------

def test_deadline_takes_precedence_over_due():
    """`due` is when you plan to work on it; `deadline` is when it's actually due."""
    resolved = tt.task_due_at(
        task(due={"date": "2026-10-05"}, deadline={"date": "2026-10-01"}), LAGOS
    )
    assert resolved.date() == dt.date(2026, 10, 1)


def test_due_used_when_no_deadline():
    resolved = tt.task_due_at(task(due={"date": "2026-10-05"}), LAGOS)
    assert resolved.date() == dt.date(2026, 10, 5)


def test_deadline_used_when_no_due():
    """Either field can exist alone."""
    resolved = tt.task_due_at(task(deadline={"date": "2026-10-01"}), LAGOS)
    assert resolved.date() == dt.date(2026, 10, 1)


def test_date_only_resolves_to_end_of_local_day():
    """'Due Friday' means by the end of Friday, in the user's own timezone."""
    resolved = tt.task_due_at(task(due={"date": "2026-10-01"}), LAGOS)
    local = resolved.astimezone(LAGOS)
    assert (local.hour, local.minute) == (23, 59)
    assert local.date() == dt.date(2026, 10, 1)


def test_date_only_differs_between_timezones():
    lagos = tt.task_due_at(task(due={"date": "2026-10-01"}), LAGOS)
    utc = tt.task_due_at(task(due={"date": "2026-10-01"}), UTC)
    assert lagos != utc


def test_explicit_datetime_with_offset_is_preserved():
    resolved = tt.task_due_at(task(due={"datetime": "2026-10-01T17:00:00Z"}), LAGOS)
    assert resolved.astimezone(UTC).hour == 17


def test_floating_datetime_is_interpreted_locally():
    """A datetime with no offset is local to the user, not UTC."""
    resolved = tt.task_due_at(task(due={"datetime": "2026-10-01T17:00:00"}), LAGOS)
    assert resolved.astimezone(LAGOS).hour == 17


def test_due_datetime_honours_its_own_timezone_field():
    resolved = tt.task_due_at(
        task(due={"datetime": "2026-10-01T17:00:00", "timezone": "Europe/London"}), LAGOS
    )
    assert resolved.astimezone(ZoneInfo("Europe/London")).hour == 17


def test_undated_task_has_no_due_date():
    assert tt.task_due_at(task(), LAGOS) is None
    assert tt.task_due_at(task(due=None), LAGOS) is None


def test_malformed_date_returns_none_rather_than_raising():
    assert tt.task_due_at(task(due={"date": "not-a-date"}), LAGOS) is None
    assert tt.task_due_at(task(due="tomorrow"), LAGOS) is None


def test_end_of_day_is_last_moment_of_the_local_day():
    moment = tt.end_of_day(dt.date(2026, 10, 1), LAGOS)
    assert moment.hour == 23 and moment.second == 59
    assert moment.tzinfo is LAGOS


# --- recurrence -----------------------------------------------------------

def test_recurring_detected():
    assert tt.is_recurring(task(due={"date": "2026-10-01", "is_recurring": True})) is True


def test_non_recurring_and_undated_are_not_recurring():
    assert tt.is_recurring(task(due={"date": "2026-10-01"})) is False
    assert tt.is_recurring(task()) is False


# --- labels ---------------------------------------------------------------

def test_high_priority_is_annotated():
    """Todoist inverts priority: 4 is p1."""
    assert "[p1]" in tt.task_label(task(priority=4))
    assert "[p2]" in tt.task_label(task(priority=3))


def test_normal_priority_is_not_annotated():
    assert tt.task_label(task(priority=1)) == "write the report"


def test_empty_content_gets_a_placeholder():
    assert tt.task_label(task(content="  ")) == "(untitled task)"


def test_format_task_shows_due_and_recurrence():
    rendered = tt.format_task(
        task(due={"date": "2026-10-01", "is_recurring": True}), LAGOS
    )
    assert "due" in rendered and "(recurring)" in rendered and "id=t1" in rendered


# --- sync -----------------------------------------------------------------

def capture_deadlines(monkeypatch):
    captured = []

    def fake_upsert(**kwargs):
        captured.append(kwargs)
        return {
            "id": len(captured),
            "description": kwargs["description"],
            "due_at": kwargs["due_at"],
        }

    monkeypatch.setattr(tt.store, "upsert_deadline", fake_upsert)
    return captured


def test_sync_skips_recurring_chores_by_default(monkeypatch):
    """A daily chore regenerating forever would bury the real deadlines."""
    captured = capture_deadlines(monkeypatch)
    tt.sync_deadlines(
        [task(id="a", due={"date": "2026-10-01", "is_recurring": True})], LAGOS
    )
    assert captured == []


def test_sync_includes_recurring_when_asked(monkeypatch):
    captured = capture_deadlines(monkeypatch)
    tt.sync_deadlines(
        [task(id="a", due={"date": "2026-10-01", "is_recurring": True})],
        LAGOS,
        include_recurring=True,
    )
    assert len(captured) == 1


def test_sync_skips_undated_tasks(monkeypatch):
    """An undated task is a someday item, not a deadline."""
    captured = capture_deadlines(monkeypatch)
    tt.sync_deadlines([task(id="a")], LAGOS)
    assert captured == []


def test_sync_uses_task_id_as_source_ref(monkeypatch):
    """source_ref is what makes a re-sync update instead of duplicate."""
    captured = capture_deadlines(monkeypatch)
    tt.sync_deadlines([task(id="abc123", due={"date": "2026-10-01"})], LAGOS)

    assert captured[0]["source"] == "todoist"
    assert captured[0]["source_ref"] == "abc123"


def test_sync_prefers_deadline_field(monkeypatch):
    captured = capture_deadlines(monkeypatch)
    tt.sync_deadlines(
        [task(due={"date": "2026-10-05"}, deadline={"date": "2026-10-01"})], LAGOS
    )
    assert captured[0]["due_at"].date() == dt.date(2026, 10, 1)


def test_sync_skips_tasks_without_an_id(monkeypatch):
    captured = capture_deadlines(monkeypatch)
    tt.sync_deadlines([{"content": "x", "due": {"date": "2026-10-01"}}], LAGOS)
    assert captured == []


def test_sync_handles_empty_input(monkeypatch):
    capture_deadlines(monkeypatch)
    assert tt.sync_deadlines([], LAGOS) == []


# --- pagination -----------------------------------------------------------

def paged(monkeypatch, pages):
    calls = []

    def fake_request(method, path, token=None, json=None, params=None):
        calls.append(dict(params or {}))
        return pages[len(calls) - 1]

    monkeypatch.setattr(todoist_auth, "request", fake_request)
    return calls


def test_paginate_follows_next_cursor(monkeypatch):
    """Ignoring next_cursor would silently import only the first page."""
    calls = paged(monkeypatch, [
        {"results": [{"id": "1"}], "next_cursor": "c1"},
        {"results": [{"id": "2"}], "next_cursor": None},
    ])

    results = todoist_auth.paginate("/tasks")

    assert [r["id"] for r in results] == ["1", "2"]
    assert calls[1]["cursor"] == "c1"


def test_paginate_stops_without_a_cursor(monkeypatch):
    calls = paged(monkeypatch, [{"results": [{"id": "1"}], "next_cursor": None}])
    todoist_auth.paginate("/tasks")
    assert len(calls) == 1


def test_paginate_respects_limit(monkeypatch):
    paged(monkeypatch, [
        {"results": [{"id": "1"}, {"id": "2"}], "next_cursor": "c1"},
        {"results": [{"id": "3"}], "next_cursor": None},
    ])
    assert len(todoist_auth.paginate("/tasks", limit=2)) == 2


def test_paginate_tolerates_a_bare_list(monkeypatch):
    paged(monkeypatch, [[{"id": "1"}]])
    assert todoist_auth.paginate("/tasks") == [{"id": "1"}]


def test_paginate_is_bounded_against_a_cursor_loop(monkeypatch):
    """A server that always returns a cursor must not hang the sweep."""
    def fake_request(method, path, token=None, json=None, params=None):
        return {"results": [{"id": "x"}], "next_cursor": "always"}

    monkeypatch.setattr(todoist_auth, "request", fake_request)
    assert len(todoist_auth.paginate("/tasks")) == todoist_auth.MAX_PAGES


def test_paginate_handles_empty_results(monkeypatch):
    paged(monkeypatch, [{"results": [], "next_cursor": None}])
    assert todoist_auth.paginate("/tasks") == []


# --- error translation ----------------------------------------------------

def test_unauthorized_points_at_onboarding():
    assert "onboarding" in todoist_auth.describe_error(401)


def test_rate_limited_is_translated():
    assert "rate-limit" in todoist_auth.describe_error(429)


def test_not_found_is_translated():
    assert "No such" in todoist_auth.describe_error(404)


def test_bad_request_includes_the_body():
    assert "missing content" in todoist_auth.describe_error(400, "missing content")
