"""Gmail push: notification decoding, cursor handling, watch renewal.

The failure modes here are all silent ones — mail that quietly stops arriving,
or changes that are skipped without anything erroring — so each is pinned
directly.
"""

import base64
import datetime as dt
import json

from twin.config import SETTINGS
from twin.tools import gmail_watch as gw


def encode(payload):
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def notification(history_id="12345", email="a@b.test"):
    return {"data": encode({"emailAddress": email, "historyId": history_id})}


def history_record(message_id, labels=None):
    message = {"id": message_id}
    if labels:
        message["labelIds"] = labels
    return {"messagesAdded": [{"message": message}]}


# --- notification decoding ------------------------------------------------

def test_notification_decodes():
    parsed = gw.parse_notification(notification("999"))
    assert parsed["historyId"] == "999"
    assert parsed["emailAddress"] == "a@b.test"


def test_notification_without_data_is_dropped():
    assert gw.parse_notification({}) is None
    assert gw.parse_notification({"data": ""}) is None


def test_malformed_notification_is_dropped_not_raised():
    """A bad notification must still be ack-able, or it redelivers forever."""
    assert gw.parse_notification({"data": "not-base64!!"}) is None
    assert gw.parse_notification({"data": base64.urlsafe_b64encode(b"nonsense").decode()}) is None


def test_notification_without_history_id_is_dropped():
    assert gw.parse_notification({"data": encode({"emailAddress": "a@b.test"})}) is None


# --- history id selection -------------------------------------------------

def test_lowest_history_id_is_chosen():
    """Gmail coalesces notifications; replaying from the highest skips mail."""
    batch = [{"historyId": "300"}, {"historyId": "100"}, {"historyId": "200"}]
    assert gw.lowest_history_id(batch) == "100"


def test_lowest_history_id_compares_numerically_not_lexically():
    """'1000' < '900' as strings, which would pick the wrong cursor."""
    assert gw.lowest_history_id([{"historyId": "1000"}, {"historyId": "900"}]) == "900"


def test_lowest_history_id_ignores_garbage():
    assert gw.lowest_history_id([{"historyId": "abc"}, {"historyId": "5"}]) == "5"


def test_lowest_history_id_of_empty_batch_is_none():
    assert gw.lowest_history_id([]) is None
    assert gw.lowest_history_id([{"historyId": None}]) is None


# --- watch expiry ---------------------------------------------------------

def test_watch_expiry_parses_epoch_milliseconds():
    # Gmail returns this as a string of epoch millis.
    expires = gw.watch_expiry({"expiration": "1789218000000"})
    assert expires.tzinfo is not None
    assert expires.year == 2026


def test_watch_expiry_missing_or_garbage_is_none():
    assert gw.watch_expiry({}) is None
    assert gw.watch_expiry({"expiration": "soon"}) is None


def test_unregistered_watch_needs_renewal():
    assert gw.needs_renewal(None) is True


def test_watch_near_expiry_needs_renewal():
    """Renew with room to spare so a missed tick can't let it lapse."""
    now = dt.datetime.now(dt.timezone.utc)
    assert gw.needs_renewal(now + dt.timedelta(hours=2), now) is True


def test_fresh_watch_does_not_need_renewal():
    now = dt.datetime.now(dt.timezone.utc)
    assert gw.needs_renewal(now + dt.timedelta(days=6), now) is False


def test_expired_watch_needs_renewal():
    now = dt.datetime.now(dt.timezone.utc)
    assert gw.needs_renewal(now - dt.timedelta(hours=1), now) is True


# --- history replay -------------------------------------------------------

def test_message_ids_collected_from_history():
    ids = gw.message_ids_from_history([history_record("m1"), history_record("m2")])
    assert ids == ["m1", "m2"]


def test_message_ids_are_deduplicated():
    """The same message can appear in several history records."""
    ids = gw.message_ids_from_history([history_record("m1"), history_record("m1")])
    assert ids == ["m1"]


def test_drafts_spam_and_trash_are_skipped():
    """None of these are things the user committed to."""
    records = [
        history_record("m1", ["DRAFT"]),
        history_record("m2", ["SPAM"]),
        history_record("m3", ["TRASH"]),
        history_record("m4", ["INBOX"]),
    ]
    assert gw.message_ids_from_history(records) == ["m4"]


def test_empty_history_yields_nothing():
    assert gw.message_ids_from_history([]) == []
    assert gw.message_ids_from_history([{"messagesAdded": []}]) == []


# --- process_notifications ------------------------------------------------

def _setup(monkeypatch, notifications, ack_ids=("ack1",), message_ids=(), expired=False):
    state = {"acked": [], "cursor_writes": []}

    monkeypatch.setattr(SETTINGS, "gmail_pubsub_topic", "projects/p/topics/t")
    monkeypatch.setattr(SETTINGS, "gmail_pubsub_subscription", "projects/p/subscriptions/s")
    monkeypatch.setattr(gw, "pull", lambda max_messages=50: (list(notifications), list(ack_ids)))
    monkeypatch.setattr(gw, "acknowledge", lambda ids: state["acked"].extend(ids))
    monkeypatch.setattr(
        gw, "fetch_new_message_ids",
        lambda cursor: (list(message_ids), "999", expired),
    )
    monkeypatch.setattr(gw.store, "get_profile", lambda: {"gmail_history_id": "100"})
    monkeypatch.setattr(
        gw.store, "update_profile", lambda **kw: state["cursor_writes"].append(kw)
    )

    from twin.tools import gmail_tools

    monkeypatch.setattr(
        gmail_tools, "process_message_ids", lambda ids, service=None: [{"id": 1} for _ in ids]
    )
    monkeypatch.setattr(gmail_tools, "sweep_deadlines", lambda **kw: [{"id": 9}])
    return state


def test_push_disabled_is_a_no_op(monkeypatch):
    monkeypatch.setattr(SETTINGS, "gmail_pubsub_topic", None)
    monkeypatch.setattr(SETTINGS, "gmail_pubsub_subscription", None)
    assert gw.process_notifications()["pulled"] == 0


def test_empty_pull_still_acknowledges(monkeypatch):
    """Unparseable stragglers must be acked or they redeliver forever."""
    state = _setup(monkeypatch, notifications=[], ack_ids=["ack1"])
    gw.process_notifications()
    assert state["acked"] == ["ack1"]


def test_notifications_drive_extraction_and_advance_the_cursor(monkeypatch):
    state = _setup(
        monkeypatch,
        notifications=[{"historyId": "150"}],
        message_ids=["m1", "m2"],
    )

    result = gw.process_notifications()

    assert result["messages"] == 2 and result["deadlines"] == 2
    assert state["acked"] == ["ack1"]
    assert {"gmail_history_id": "999"} in state["cursor_writes"]


def test_expired_cursor_falls_back_to_a_sweep(monkeypatch):
    """An aged-out history id must not silently lose mail."""
    state = _setup(
        monkeypatch,
        notifications=[{"historyId": "500"}, {"historyId": "700"}],
        expired=True,
    )

    result = gw.process_notifications()

    assert result["recovered"] is True
    assert result["deadlines"] == 1  # from the fallback sweep
    # Cursor re-based on the NEWEST notification, since older history is gone.
    assert {"gmail_history_id": "700"} in state["cursor_writes"]
    assert state["acked"] == ["ack1"]


def test_status_reports_configuration(monkeypatch):
    monkeypatch.setattr(SETTINGS, "gmail_pubsub_topic", "projects/p/topics/t")
    monkeypatch.setattr(SETTINGS, "gmail_pubsub_subscription", "projects/p/subscriptions/s")
    monkeypatch.setattr(
        gw.store, "get_profile",
        lambda: {"gmail_history_id": "100", "gmail_watch_expires_at": None},
    )

    state = gw.status()
    assert state["configured"] is True
    assert state["needs_renewal"] is True
