"""Deadline extraction: response validation, two-stage gating, sweep wiring.

The validators carry the weight here. A fabricated deadline appearing in the
user's list is worse than a missed one, so anything vague, low-confidence, or
undated must be dropped rather than guessed at.
"""

import datetime as dt

import pytest

from twin import aux, extraction


def entry(index=0, **overrides):
    payload = {
        "index": index,
        "description": "submit the grant application",
        "due_at": "2026-10-01T17:00:00Z",
        "confidence": 0.85,
    }
    payload.update(overrides)
    return payload


# --- parse_index_list -----------------------------------------------------

def test_index_list_accepts_bare_integers():
    assert extraction.parse_index_list([0, 2, 4], 5) == [0, 2, 4]


def test_index_list_accepts_wrapped_objects():
    """Models drift between [0] and [{"index": 0}]; accept both."""
    assert extraction.parse_index_list([{"index": 1}, {"index": 3}], 5) == [1, 3]


def test_index_list_drops_out_of_range():
    assert extraction.parse_index_list([0, 9, -1], 3) == [0]


def test_index_list_dedupes():
    assert extraction.parse_index_list([1, 1, 2], 5) == [1, 2]


def test_index_list_drops_unparseable():
    assert extraction.parse_index_list(["x", None, 2], 5) == [2]


def test_index_list_rejects_non_list():
    assert extraction.parse_index_list({"indices": [1]}, 5) == []


def test_index_list_handles_empty():
    assert extraction.parse_index_list([], 5) == []


# --- parse_extraction -----------------------------------------------------

def test_valid_extraction_parses():
    parsed = extraction.parse_extraction(entry())
    assert parsed["description"] == "submit the grant application"
    assert parsed["due_at"].tzinfo is not None
    assert parsed["due_at"].year == 2026


def test_extraction_rejected_below_confidence_floor():
    assert extraction.parse_extraction(entry(confidence=0.4)) is None


def test_extraction_accepted_exactly_at_floor():
    assert extraction.parse_extraction(entry(confidence=extraction.MIN_CONFIDENCE)) is not None


def test_extraction_rejected_without_date():
    """A deadline with no date isn't actionable, so it isn't a deadline."""
    assert extraction.parse_extraction(entry(due_at=None)) is None
    assert extraction.parse_extraction(entry(due_at="")) is None


def test_extraction_rejected_with_unparseable_date():
    assert extraction.parse_extraction(entry(due_at="next-ish Friday")) is None


def test_extraction_rejected_without_description():
    assert extraction.parse_extraction(entry(description="   ")) is None


def test_extraction_rejected_for_bad_confidence_type():
    assert extraction.parse_extraction(entry(confidence="high")) is None


def test_extraction_rejects_non_dict():
    assert extraction.parse_extraction(["nope"]) is None
    assert extraction.parse_extraction(None) is None


def test_has_commitment_false_is_honoured():
    """The Slack listener's single-item shape still short-circuits."""
    assert extraction.parse_extraction(entry(has_commitment=False)) is None


def test_has_commitment_true_passes_through():
    assert extraction.parse_extraction(entry(has_commitment=True)) is not None


# --- parse_extraction_batch ----------------------------------------------

def test_batch_maps_entries_to_indices():
    parsed = extraction.parse_extraction_batch([entry(0), entry(2)], 3)
    assert sorted(parsed) == [0, 2]


def test_batch_drops_invalid_entries_but_keeps_valid_ones():
    parsed = extraction.parse_extraction_batch(
        [entry(0, confidence=0.1), entry(1), "garbage", {"index": 9}], 3
    )
    assert list(parsed) == [1]


def test_batch_ignores_duplicate_index():
    parsed = extraction.parse_extraction_batch(
        [entry(0, description="first"), entry(0, description="second")], 2
    )
    assert parsed[0]["description"] == "first"


def test_batch_rejects_non_list():
    assert extraction.parse_extraction_batch({"index": 0}, 3) == {}


# --- render_items ---------------------------------------------------------

def test_render_numbers_items_for_the_model():
    rendered = extraction.render_items(
        [{"label": "from: a", "when": "Mon", "text": "hello"}]
    )
    assert "[0]" in rendered and "from: a" in rendered and "hello" in rendered


def test_render_truncates_long_text():
    rendered = extraction.render_items([{"text": "x" * 5000}], max_chars=100)
    assert rendered.count("x") == 100


def test_render_tolerates_missing_fields():
    assert "[0]" in extraction.render_items([{}])


# --- classify -------------------------------------------------------------

def test_classify_short_circuits_on_empty(monkeypatch):
    monkeypatch.setattr(
        extraction.aux, "complete_json",
        lambda *a, **k: pytest.fail("called the model with no items"),
    )
    assert extraction.classify([]) == []


def test_classifier_failure_yields_no_candidates(monkeypatch):
    """A broken classifier must not let the expensive stage see everything."""
    def boom(*args, **kwargs):
        raise aux.AuxError("model down")

    monkeypatch.setattr(extraction.aux, "complete_json", boom)
    assert extraction.classify([{"text": "anything"}]) == []


def test_classify_caps_batch_size(monkeypatch):
    seen = {}

    def capture(system, user, max_tokens=0):
        seen["user"] = user
        return list(range(extraction.MAX_BATCH + 10))

    monkeypatch.setattr(extraction.aux, "complete_json", capture)
    items = [{"text": "x"} for _ in range(extraction.MAX_BATCH + 10)]
    result = extraction.classify(items)

    # Indices beyond the truncated batch are out of range and dropped.
    assert max(result) < extraction.MAX_BATCH


# --- extract --------------------------------------------------------------

def test_extract_short_circuits_on_empty(monkeypatch):
    monkeypatch.setattr(
        extraction.aux, "complete_json",
        lambda *a, **k: pytest.fail("called the model with no items"),
    )
    assert extraction.extract([]) == {}


def test_extractor_failure_yields_nothing(monkeypatch):
    def boom(*args, **kwargs):
        raise aux.AuxError("model down")

    monkeypatch.setattr(extraction.aux, "complete_json", boom)
    assert extraction.extract([{"text": "due friday"}]) == {}


def test_extract_returns_indexed_deadlines(monkeypatch):
    monkeypatch.setattr(extraction.aux, "complete_json", lambda *a, **k: [entry(0)])
    found = extraction.extract([{"text": "grant due"}])
    assert found[0]["description"] == "submit the grant application"


def test_extract_one_returns_first_item(monkeypatch):
    monkeypatch.setattr(extraction.aux, "complete_json", lambda *a, **k: [entry(0)])
    parsed = extraction.extract_one("grant due oct 1", dt.datetime.now(dt.timezone.utc))
    assert parsed["description"] == "submit the grant application"


def test_extract_one_returns_none_when_model_omits_the_item(monkeypatch):
    """Omission is how the batch extractor says 'no deadline here'."""
    monkeypatch.setattr(extraction.aux, "complete_json", lambda *a, **k: [])
    assert extraction.extract_one("just saying hi") is None


# --- Gmail sweep ----------------------------------------------------------

class FakeGmail:
    """Minimal stand-in for the Gmail client, recording what was fetched."""

    def __init__(self, message_ids):
        self.message_ids = message_ids
        self.full_fetches = []
        self.queries = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId=None, q=None, maxResults=None):
        self.queries.append(q)
        return _Exec({"messages": [{"id": mid} for mid in self.message_ids]})

    def get(self, userId=None, id=None, format=None, metadataHeaders=None):
        if format == "full":
            self.full_fetches.append(id)
            return _Exec({
                "payload": {
                    "mimeType": "text/plain",
                    "body": {"data": _b64("Please submit by October 1st.")},
                    "headers": [],
                }
            })
        return _Exec({
            "snippet": "snippet for {0}".format(id),
            "payload": {"headers": [
                {"name": "From", "value": "a@b.test"},
                {"name": "Subject", "value": "Subject {0}".format(id)},
                {"name": "Date", "value": "Mon, 12 Sep 2026 10:00:00 +0000"},
            ]},
        })


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


def _b64(text):
    import base64
    return base64.urlsafe_b64encode(text.encode()).decode()


def _setup_gmail_sweep(monkeypatch, message_ids, candidates, extracted, recorded):
    from twin.tools import gmail_tools

    client = FakeGmail(message_ids)
    monkeypatch.setattr(gmail_tools.google_auth, "client", lambda service: client)
    monkeypatch.setattr(gmail_tools.extraction, "classify", lambda items: candidates)
    monkeypatch.setattr(gmail_tools.extraction, "extract", lambda items: extracted)
    monkeypatch.setattr(
        gmail_tools.store, "upsert_deadline",
        lambda **kw: recorded.append(kw) or {
            "id": len(recorded), "description": kw["description"], "due_at": kw["due_at"]
        },
    )
    return client


def test_gmail_sweep_only_fetches_bodies_for_candidates(monkeypatch):
    """The whole point of the two-stage design: don't read every email."""
    recorded = []
    client = _setup_gmail_sweep(
        monkeypatch,
        message_ids=["m0", "m1", "m2", "m3"],
        candidates=[1, 3],
        extracted={},
        recorded=recorded,
    )

    from twin.tools import gmail_tools

    gmail_tools.sweep_deadlines(days=3)
    assert client.full_fetches == ["m1", "m3"]


def test_gmail_sweep_skips_extraction_when_nothing_classifies(monkeypatch):
    recorded = []
    client = _setup_gmail_sweep(
        monkeypatch, ["m0", "m1"], candidates=[], extracted={}, recorded=recorded
    )

    from twin.tools import gmail_tools

    assert gmail_tools.sweep_deadlines(days=3) == []
    assert client.full_fetches == []


def test_gmail_sweep_records_with_message_id_as_source_ref(monkeypatch):
    """source_ref is what makes a re-scan update instead of duplicate."""
    recorded = []
    _setup_gmail_sweep(
        monkeypatch,
        message_ids=["m0", "m1"],
        candidates=[1],
        extracted={0: {
            "description": "submit the grant",
            "due_at": dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
            "confidence": 0.9,
        }},
        recorded=recorded,
    )

    from twin.tools import gmail_tools

    result = gmail_tools.sweep_deadlines(days=3)

    assert len(result) == 1
    assert recorded[0]["source"] == "gmail"
    assert recorded[0]["source_ref"] == "m1"


def test_gmail_sweep_handles_an_empty_mailbox(monkeypatch):
    recorded = []
    _setup_gmail_sweep(monkeypatch, [], candidates=[], extracted={}, recorded=recorded)

    from twin.tools import gmail_tools

    assert gmail_tools.sweep_deadlines(days=3) == []


def test_gmail_sweep_uses_cursor_when_given(monkeypatch):
    recorded = []
    client = _setup_gmail_sweep(monkeypatch, [], [], {}, recorded)

    from twin.tools import gmail_tools

    gmail_tools.sweep_deadlines(since=dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc))
    assert "after:" in client.queries[0]


def test_gmail_sweep_query_excludes_bulk_categories():
    """Filter newsletters server-side, before a single token is spent."""
    from twin.tools import gmail_tools

    built = gmail_tools.build_sweep_query(days=3)
    for excluded in ("promotions", "social", "forums"):
        assert excluded in built


# --- Calendar sweep -------------------------------------------------------

def _fake_event(event_id, summary, start, blocking=True, all_day=False):
    from twin.tools.calendar_tools import Event

    return Event(
        id=event_id, summary=summary, start=start,
        end=start + dt.timedelta(hours=1), all_day=all_day,
        location="", blocking=blocking,
    )


def test_calendar_sweep_uses_event_start_as_due_date(monkeypatch):
    """The calendar already knows when it is -- don't invent a date."""
    from twin.tools import calendar_tools

    when = dt.datetime(2026, 10, 1, 9, tzinfo=dt.timezone.utc)
    recorded = []

    monkeypatch.setattr(
        calendar_tools, "fetch_events",
        lambda *a, **k: [_fake_event("e1", "Thesis submission", when)],
    )
    monkeypatch.setattr(calendar_tools, "_profile_time_settings", lambda: (dt.timezone.utc, 9, 22))
    monkeypatch.setattr(calendar_tools.extraction, "classify", lambda items: [0])
    monkeypatch.setattr(
        calendar_tools.store, "upsert_deadline",
        lambda **kw: recorded.append(kw) or {
            "id": 1, "description": kw["description"], "due_at": kw["due_at"]
        },
    )

    calendar_tools.sweep_deadlines(days=30)

    assert recorded[0]["due_at"] == when
    assert recorded[0]["source"] == "calendar"
    assert recorded[0]["source_ref"] == "e1"


def test_calendar_sweep_ignores_declined_events(monkeypatch):
    """You aren't obliged by a meeting you declined."""
    from twin.tools import calendar_tools

    when = dt.datetime(2026, 10, 1, 9, tzinfo=dt.timezone.utc)
    seen = {}

    monkeypatch.setattr(
        calendar_tools, "fetch_events",
        lambda *a, **k: [_fake_event("e1", "Declined thing", when, blocking=False)],
    )
    monkeypatch.setattr(calendar_tools, "_profile_time_settings", lambda: (dt.timezone.utc, 9, 22))
    monkeypatch.setattr(
        calendar_tools.extraction, "classify",
        lambda items: seen.update(count=len(items)) or [],
    )

    assert calendar_tools.sweep_deadlines(days=30) == []
    assert seen == {}, "sent a declined event to the classifier"


def test_calendar_sweep_records_nothing_when_no_events(monkeypatch):
    from twin.tools import calendar_tools

    monkeypatch.setattr(calendar_tools, "fetch_events", lambda *a, **k: [])
    monkeypatch.setattr(calendar_tools, "_profile_time_settings", lambda: (dt.timezone.utc, 9, 22))
    assert calendar_tools.sweep_deadlines(days=30) == []
