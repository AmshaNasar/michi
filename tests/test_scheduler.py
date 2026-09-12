"""Proactive checks.

Focused on the free-time x stale-project pairing, since that's the nudge the
persona exists for and it has the most ways to go wrong quietly.
"""

import datetime as dt

from zoneinfo import ZoneInfo

from twin import scheduler

LAGOS = ZoneInfo("Africa/Lagos")


def at(hour, day=12, tz=LAGOS):
    return dt.datetime(2026, 9, day, hour, tzinfo=tz)


def _setup(monkeypatch, connected, stale, windows, queued):
    monkeypatch.setattr(scheduler.store, "get_profile", lambda: {
        "connected_apps": connected,
        "timezone": "Africa/Lagos",
        "staleness_threshold_days": 14,
    })
    monkeypatch.setattr(scheduler.store, "stale_projects", lambda *a, **k: stale)
    monkeypatch.setattr(
        scheduler.store,
        "queue_nudge",
        lambda kind, message, evidence=None: queued.append(
            {"kind": kind, "message": message, "evidence": evidence}
        ),
    )

    from twin.tools import calendar_tools

    monkeypatch.setattr(calendar_tools, "free_windows", lambda **kwargs: windows)


def project(name="guitar project", days_stale=23):
    return {
        "name": name,
        "days_stale": days_stale,
        "last_activity_at": dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc),
    }


def test_no_nudge_when_calendar_not_connected(monkeypatch):
    queued = []
    _setup(monkeypatch, ["gmail"], [project()], [(at(18), at(22))], queued)
    assert scheduler.check_free_time_for_projects() == 0
    assert queued == []


def test_no_nudge_when_nothing_is_stale(monkeypatch):
    queued = []
    _setup(monkeypatch, ["calendar"], [], [(at(18), at(22))], queued)
    assert scheduler.check_free_time_for_projects() == 0
    assert queued == []


def test_no_nudge_when_calendar_is_full(monkeypatch):
    queued = []
    _setup(monkeypatch, ["calendar"], [project()], [], queued)
    assert scheduler.check_free_time_for_projects() == 0
    assert queued == []


def test_pairs_soonest_free_evening_with_stalest_project(monkeypatch):
    queued = []
    windows = [
        (at(19, day=14), at(22, day=14)),
        (at(18, day=13), at(21, day=13)),  # soonest evening
    ]
    _setup(monkeypatch, ["calendar"], [project()], windows, queued)

    assert scheduler.check_free_time_for_projects() == 1
    nudge = queued[0]

    assert nudge["kind"] == "free_time"
    assert "guitar project" in nudge["message"]
    assert "23 days" in nudge["message"]
    assert "2 free evening(s)" in nudge["message"]

    # Evidence must carry the real numbers so the agent can cite them.
    assert nudge["evidence"]["free_window_count"] == 2
    assert nudge["evidence"]["are_evenings"] is True
    assert nudge["evidence"]["soonest_start"] == at(18, day=13).isoformat()


def test_falls_back_to_daytime_when_no_evenings_are_free(monkeypatch):
    queued = []
    _setup(monkeypatch, ["calendar"], [project()], [(at(10), at(13))], queued)

    assert scheduler.check_free_time_for_projects() == 1
    nudge = queued[0]

    assert "window(s)" in nudge["message"]
    assert nudge["evidence"]["are_evenings"] is False


def test_evening_boundary_uses_local_time(monkeypatch):
    """17:00 local counts as an evening even though it's 16:00 UTC."""
    queued = []
    _setup(monkeypatch, ["calendar"], [project()], [(at(17), at(20))], queued)

    scheduler.check_free_time_for_projects()
    assert queued[0]["evidence"]["are_evenings"] is True


def test_stalest_project_wins_when_several_are_stale(monkeypatch):
    queued = []
    stale = [project("mql5 EA", 40), project("guitar project", 23)]
    _setup(monkeypatch, ["calendar"], stale, [(at(18), at(21))], queued)

    scheduler.check_free_time_for_projects()
    assert "mql5 EA" in queued[0]["message"]


# --- opportunity discovery ------------------------------------------------

def _setup_opportunities(
    monkeypatch,
    queued,
    interests=("algorithmic trading",),
    exa_key="test-key",
    last_scan=None,
    connected=(),
    windows=None,
    results=None,
    discover_calls=None,
):
    monkeypatch.setattr(scheduler.SETTINGS, "exa_api_key", exa_key)
    monkeypatch.setattr(scheduler.store, "get_profile", lambda: {
        "interests": list(interests),
        "connected_apps": list(connected),
        "location": "Lagos, Nigeria",
        "timezone": "Africa/Lagos",
        "last_opportunity_scan": last_scan,
        "staleness_threshold_days": 14,
    })
    monkeypatch.setattr(
        scheduler.store,
        "queue_nudge",
        lambda kind, message, evidence=None: queued.append(
            {"kind": kind, "message": message, "evidence": evidence}
        ),
    )

    recorded = {}
    monkeypatch.setattr(
        scheduler.store, "update_profile", lambda **kw: recorded.update(kw)
    )

    from twin.tools import calendar_tools, opportunity_tools

    monkeypatch.setattr(calendar_tools, "free_windows", lambda **kw: windows or [])

    def fake_discover(**kwargs):
        if discover_calls is not None:
            discover_calls.append(kwargs)
        return list(results or [])

    monkeypatch.setattr(opportunity_tools, "discover", fake_discover)
    monkeypatch.setattr(opportunity_tools, "mark_surfaced", lambda result: None)
    return recorded


def opportunity(title="Lagos Algo Trading Hackathon", url="https://x.test/1"):
    return {
        "title": title,
        "url": url,
        "score": 9,
        "kind": "hackathon",
        "when": "2026-10-03",
        "reason": "matches algorithmic trading",
    }


def test_no_opportunity_scan_without_exa_key(monkeypatch):
    queued, calls = [], []
    _setup_opportunities(monkeypatch, queued, exa_key=None, discover_calls=calls)
    assert scheduler.check_opportunities() == 0
    assert calls == [], "searched despite having no API key"


def test_no_opportunity_scan_without_interests(monkeypatch):
    queued, calls = [], []
    _setup_opportunities(monkeypatch, queued, interests=(), discover_calls=calls)
    assert scheduler.check_opportunities() == 0
    assert calls == []


def test_cooldown_blocks_a_recent_rescan(monkeypatch):
    """Discovery spends credits, so it must not run on every scheduler tick."""
    queued, calls = [], []
    recent = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    _setup_opportunities(monkeypatch, queued, last_scan=recent, discover_calls=calls)

    assert scheduler.check_opportunities() == 0
    assert calls == [], "re-queried Exa inside the cooldown window"


def test_scan_runs_once_cooldown_has_elapsed(monkeypatch):
    queued, calls = [], []
    stale_scan = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        hours=scheduler.OPPORTUNITY_COOLDOWN_HOURS + 1
    )
    _setup_opportunities(
        monkeypatch, queued, last_scan=stale_scan,
        results=[opportunity()], discover_calls=calls,
    )

    assert scheduler.check_opportunities() == 1
    assert len(calls) == 1


def test_full_calendar_skips_discovery(monkeypatch):
    """No point surfacing things they have no room to attend."""
    queued, calls = [], []
    _setup_opportunities(
        monkeypatch, queued, connected=("calendar",), windows=[], discover_calls=calls
    )
    assert scheduler.check_opportunities() == 0
    assert calls == []


def test_discovery_runs_without_a_connected_calendar(monkeypatch):
    """Interest matching is still grounded data even with no calendar."""
    queued, calls = [], []
    _setup_opportunities(
        monkeypatch, queued, connected=(), results=[opportunity()], discover_calls=calls
    )

    assert scheduler.check_opportunities() == 1
    assert queued[0]["evidence"]["free_time"] == ""


def test_nudge_carries_citable_evidence(monkeypatch):
    queued = []
    _setup_opportunities(
        monkeypatch, queued, connected=("calendar",),
        windows=[(at(18), at(21))], results=[opportunity()],
    )

    scheduler.check_opportunities()
    nudge = queued[0]

    assert nudge["kind"] == "opportunity"
    assert "Lagos Algo Trading Hackathon" in nudge["message"]
    assert "https://x.test/1" in nudge["message"]
    assert nudge["evidence"]["score"] == 9
    assert nudge["evidence"]["matched_interests"] == ["algorithmic trading"]
    assert "free window(s)" in nudge["evidence"]["free_time"]


def test_scan_timestamp_recorded_even_when_nothing_found(monkeypatch):
    """A quiet week must still start the cooldown, or every tick re-queries."""
    queued = []
    recorded = _setup_opportunities(monkeypatch, queued, results=[])

    assert scheduler.check_opportunities() == 0
    assert "last_opportunity_scan" in recorded


def test_discovery_failure_is_contained(monkeypatch):
    queued = []
    _setup_opportunities(monkeypatch, queued)

    from twin.tools import opportunity_tools

    def boom(**kwargs):
        raise RuntimeError("exa exploded")

    monkeypatch.setattr(opportunity_tools, "discover", boom)
    assert scheduler.check_opportunities() == 0
    assert queued == []


# --- deadline extraction sweep -------------------------------------------

def _setup_sweep(
    monkeypatch,
    connected=("gmail", "calendar"),
    last_scan=None,
    gmail_result=None,
    calendar_result=None,
    todoist_result=None,
    gmail_error=None,
    calendar_error=None,
    todoist_error=None,
):
    recorded = {}
    calls = {"gmail": [], "calendar": [], "todoist": []}

    monkeypatch.setattr(scheduler.store, "get_profile", lambda: {
        "connected_apps": list(connected),
        "last_deadline_scan": last_scan,
        "timezone": "Africa/Lagos",
        "staleness_threshold_days": 14,
    })
    monkeypatch.setattr(
        scheduler.store, "update_profile", lambda **kw: recorded.update(kw)
    )

    from twin.tools import calendar_tools, gmail_tools

    def fake_gmail(since=None, days=None, **kwargs):
        calls["gmail"].append({"since": since, "days": days})
        if gmail_error:
            raise gmail_error
        return list(gmail_result or [])

    def fake_calendar(days=None, **kwargs):
        calls["calendar"].append({"days": days})
        if calendar_error:
            raise calendar_error
        return list(calendar_result or [])

    monkeypatch.setattr(gmail_tools, "sweep_deadlines", fake_gmail)
    monkeypatch.setattr(calendar_tools, "sweep_deadlines", fake_calendar)

    from twin.tools import todoist_tools

    def fake_todoist_fetch(*args, **kwargs):
        if todoist_error:
            raise todoist_error
        return [{"id": "t1"}]

    def fake_todoist_sync(tasks, tz=None, include_recurring=False):
        calls["todoist"].append({"count": len(tasks)})
        return list(todoist_result or [])

    monkeypatch.setattr(todoist_tools, "fetch_tasks", fake_todoist_fetch)
    monkeypatch.setattr(todoist_tools, "sync_deadlines", fake_todoist_sync)
    return recorded, calls


def test_sweep_window_is_none_on_first_run():
    assert scheduler.sweep_window({"last_deadline_scan": None}, dt.datetime.now(dt.timezone.utc)) is None


def test_sweep_window_passes_a_recent_cursor_through():
    now = dt.datetime.now(dt.timezone.utc)
    last = now - dt.timedelta(hours=6)
    assert scheduler.sweep_window({"last_deadline_scan": last}, now) == last


def test_sweep_window_clamps_a_stale_cursor():
    """A twin left off for months must not classify months of mail at once."""
    now = dt.datetime.now(dt.timezone.utc)
    last = now - dt.timedelta(days=200)
    clamped = scheduler.sweep_window({"last_deadline_scan": last}, now)
    assert clamped > last
    assert (now - clamped).days == scheduler.DEADLINE_MAX_CATCHUP_DAYS


def test_sweep_skipped_when_nothing_is_connected(monkeypatch):
    recorded, calls = _setup_sweep(monkeypatch, connected=())
    assert scheduler.check_extracted_deadlines() == 0
    assert calls["gmail"] == [] and calls["calendar"] == []
    assert recorded == {}, "advanced the cursor without scanning anything"


def test_sweep_respects_cooldown(monkeypatch):
    recent = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
    recorded, calls = _setup_sweep(monkeypatch, last_scan=recent)

    assert scheduler.check_extracted_deadlines() == 0
    assert calls["gmail"] == [], "re-scanned inside the cooldown window"


def test_sweep_runs_once_cooldown_elapsed(monkeypatch):
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        hours=scheduler.DEADLINE_SCAN_COOLDOWN_HOURS + 1
    )
    recorded, calls = _setup_sweep(
        monkeypatch, last_scan=stale, gmail_result=[{"id": 1}], calendar_result=[{"id": 2}]
    )

    assert scheduler.check_extracted_deadlines() == 2
    assert len(calls["gmail"]) == 1 and len(calls["calendar"]) == 1
    assert "last_deadline_scan" in recorded


def test_sweep_only_runs_connected_sources(monkeypatch):
    recorded, calls = _setup_sweep(monkeypatch, connected=("gmail",))
    scheduler.check_extracted_deadlines()
    assert len(calls["gmail"]) == 1
    assert calls["calendar"] == []


def test_sweep_passes_cursor_to_gmail(monkeypatch):
    last = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)
    recorded, calls = _setup_sweep(monkeypatch, connected=("gmail",), last_scan=last)

    scheduler.check_extracted_deadlines()
    assert calls["gmail"][0]["since"] == last


def test_one_failing_source_does_not_block_the_other(monkeypatch):
    recorded, calls = _setup_sweep(
        monkeypatch, gmail_error=RuntimeError("gmail down"), calendar_result=[{"id": 1}]
    )

    assert scheduler.check_extracted_deadlines() == 1
    assert len(calls["calendar"]) == 1


def test_cursor_advances_when_a_source_ran(monkeypatch):
    recorded, _ = _setup_sweep(monkeypatch, connected=("gmail",), gmail_result=[])
    scheduler.check_extracted_deadlines()
    assert "last_deadline_scan" in recorded


def test_cursor_does_not_advance_when_every_source_failed(monkeypatch):
    """Otherwise a failed sweep silently skips the mail it never read."""
    recorded, _ = _setup_sweep(
        monkeypatch,
        gmail_error=RuntimeError("down"),
        calendar_error=RuntimeError("down"),
    )

    assert scheduler.check_extracted_deadlines() == 0
    assert recorded == {}


def test_todoist_is_swept_when_connected(monkeypatch):
    recorded, calls = _setup_sweep(
        monkeypatch, connected=("todoist",), todoist_result=[{"id": 1}, {"id": 2}]
    )

    assert scheduler.check_extracted_deadlines() == 2
    assert len(calls["todoist"]) == 1
    assert "last_deadline_scan" in recorded


def test_todoist_not_swept_when_unconnected(monkeypatch):
    recorded, calls = _setup_sweep(monkeypatch, connected=("gmail",))
    scheduler.check_extracted_deadlines()
    assert calls["todoist"] == []


def test_all_three_sources_contribute(monkeypatch):
    recorded, calls = _setup_sweep(
        monkeypatch,
        connected=("gmail", "calendar", "todoist"),
        gmail_result=[{"id": 1}],
        calendar_result=[{"id": 2}],
        todoist_result=[{"id": 3}],
    )

    assert scheduler.check_extracted_deadlines() == 3
    assert all(len(calls[source]) == 1 for source in ("gmail", "calendar", "todoist"))


def test_failing_todoist_does_not_block_other_sources(monkeypatch):
    recorded, calls = _setup_sweep(
        monkeypatch,
        connected=("gmail", "todoist"),
        gmail_result=[{"id": 1}],
        todoist_error=RuntimeError("todoist down"),
    )

    assert scheduler.check_extracted_deadlines() == 1
    assert len(calls["gmail"]) == 1
