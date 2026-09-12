"""Slack adapter: wire-format handling, error translation, and listener gating.

Slack's message format wraps mentions, channels, and links in markup that is
unreadable if passed straight to a model or to speech, so the unwrapping is
tested case by case. The listener's `should_analyze` gate matters just as much
-- without it, every message in a busy workspace triggers an LLM call.
"""

import datetime as dt

from slack_sdk.errors import SlackApiError
from zoneinfo import ZoneInfo

from twin.tools import slack_auth, slack_listener, slack_tools

LAGOS = ZoneInfo("Africa/Lagos")

# 2026-09-12 13:00:00 UTC
TS = "1789218000.000100"


def message(text="hello", user="U123", ts=TS, **extra):
    payload = {"type": "message", "text": text, "user": user, "ts": ts}
    payload.update(extra)
    return payload


def api_error(code, **extra):
    response = {"error": code}
    response.update(extra)
    return SlackApiError("boom", response)


# --- channel refs ---------------------------------------------------------

def test_normalize_strips_leading_hash():
    assert slack_tools.normalize_channel_ref("#general") == "general"
    assert slack_tools.normalize_channel_ref("  #eng  ") == "eng"
    assert slack_tools.normalize_channel_ref("C0123456") == "C0123456"


# --- clean_text -----------------------------------------------------------

def test_clean_text_resolves_user_mentions():
    assert slack_tools.clean_text("hi <@U123>", {"U123": "sarah"}) == "hi @sarah"


def test_clean_text_uses_inline_label_when_present():
    assert slack_tools.clean_text("hi <@U123|sarah>") == "hi @sarah"


def test_clean_text_falls_back_to_raw_id():
    assert slack_tools.clean_text("hi <@U999>") == "hi @U999"


def test_clean_text_unwraps_channel_references():
    assert slack_tools.clean_text("see <#C456|eng>") == "see #eng"


def test_clean_text_unwraps_labelled_links_keeping_label():
    assert slack_tools.clean_text("<https://x.test/pr|the PR>") == "the PR"


def test_clean_text_unwraps_bare_links():
    assert slack_tools.clean_text("<https://x.test/pr>") == "https://x.test/pr"


def test_clean_text_unwraps_mailto():
    assert slack_tools.clean_text("<mailto:a@b.test|a@b.test>") == "a@b.test"


def test_clean_text_handles_broadcast_mentions():
    assert slack_tools.clean_text("ping <!here>") == "ping @here"


def test_clean_text_unescapes_slack_entities():
    assert slack_tools.clean_text("a &amp; b &lt;c&gt;") == "a & b <c>"


def test_clean_text_handles_empty():
    assert slack_tools.clean_text("") == ""
    assert slack_tools.clean_text(None) == ""


# --- timestamps -----------------------------------------------------------

def test_format_timestamp_uses_supplied_timezone():
    """13:00 UTC is 14:00 in Lagos."""
    assert "14:00" in slack_tools.format_timestamp(TS, LAGOS)
    assert "13:00" in slack_tools.format_timestamp(TS, dt.timezone.utc)


def test_format_timestamp_survives_garbage():
    assert slack_tools.format_timestamp("not-a-ts") == "unknown time"
    assert slack_tools.format_timestamp("") == "unknown time"


# --- format_message -------------------------------------------------------

def test_format_message_includes_author_and_body():
    rendered = slack_tools.format_message(message("ship it"), {"U123": "sarah"})
    assert "sarah" in rendered and "ship it" in rendered


def test_format_message_advertises_thread_ts_for_replies():
    """The agent needs thread_ts to be able to read the thread."""
    rendered = slack_tools.format_message(message(reply_count=3), {})
    assert "3 replies" in rendered
    assert "thread_ts={0}".format(TS) in rendered


def test_format_message_marks_in_thread_replies():
    rendered = slack_tools.format_message(
        message(ts="1789218100.000200", thread_ts=TS), {}
    )
    assert "(in thread)" in rendered


def test_format_message_falls_back_for_unknown_author():
    assert "U999" in slack_tools.format_message(message(user="U999"), {})


# --- indexability ---------------------------------------------------------

def test_join_and_leave_notices_are_not_indexed():
    for subtype in ("channel_join", "channel_leave", "channel_topic"):
        assert slack_tools.is_indexable(message(subtype=subtype)) is False


def test_empty_message_is_not_indexed():
    assert slack_tools.is_indexable(message(text="   ")) is False


def test_ordinary_message_is_indexed():
    assert slack_tools.is_indexable(message("real content")) is True


# --- error translation ----------------------------------------------------

def test_missing_scope_error_names_the_scope():
    described = slack_auth.describe_api_error(
        api_error("missing_scope", needed="channels:history")
    )
    assert "channels:history" in described
    assert "reinstall" in described.lower()


def test_not_in_channel_error_explains_the_invite():
    described = slack_auth.describe_api_error(api_error("not_in_channel"))
    assert "/invite" in described


def test_invalid_auth_error_points_at_onboarding():
    assert "onboarding" in slack_auth.describe_api_error(api_error("invalid_auth"))


def test_unknown_error_code_is_passed_through():
    assert "weird_thing" in slack_auth.describe_api_error(api_error("weird_thing"))


# --- listener: processability --------------------------------------------

def test_non_message_events_are_ignored():
    assert slack_listener.is_processable({"type": "reaction_added"}) is False


def test_bot_messages_are_ignored():
    assert slack_listener.is_processable(message(bot_id="B123")) is False


def test_own_messages_are_ignored():
    """Never react to our own posts -- that's how loops start."""
    assert slack_listener.is_processable(message(user="UBOT"), "UBOT") is False


def test_join_notices_are_ignored():
    assert slack_listener.is_processable(message(subtype="channel_join")) is False


def test_edits_and_deletes_are_ignored():
    assert slack_listener.is_processable(message(subtype="message_changed")) is False
    assert slack_listener.is_processable(message(subtype="message_deleted")) is False


def test_ordinary_human_message_is_processable():
    assert slack_listener.is_processable(message("hello"), "UBOT") is True


# --- listener: analysis gating -------------------------------------------

def test_mentions_user_matches_both_mention_forms():
    assert slack_listener.mentions_user("hi <@U123>", "U123") is True
    assert slack_listener.mentions_user("hi <@U123|sarah>", "U123") is True


def test_mentions_user_does_not_match_a_prefix():
    """U12 must not match <@U123>."""
    assert slack_listener.mentions_user("hi <@U123>", "U12") is False


def test_users_own_message_is_analyzed():
    assert slack_listener.should_analyze(message(user="UHUMAN"), "UHUMAN") is True


def test_message_mentioning_user_is_analyzed():
    event = message("can <@UHUMAN> take this?", user="UOTHER")
    assert slack_listener.should_analyze(event, "UHUMAN") is True


def test_direct_messages_are_always_analyzed():
    event = message("are you free friday", user="UOTHER", channel="D0123")
    assert slack_listener.should_analyze(event, "") is True


def test_unrelated_channel_chatter_is_not_analyzed():
    """This gate is what stops an LLM call per message in a busy workspace."""
    event = message("lunch anyone", user="UOTHER", channel="C0123")
    assert slack_listener.should_analyze(event, "UHUMAN", "UBOT") is False


def test_bot_mention_is_analyzed_when_human_id_unknown():
    event = message("hey <@UBOT> remind me", user="UOTHER", channel="C0123")
    assert slack_listener.should_analyze(event, "", "UBOT") is True


# --- listener: extraction parsing ----------------------------------------

def valid_extraction(**overrides):
    payload = {
        "has_commitment": True,
        "description": "send the Q3 draft",
        "due_at": "2026-09-19T17:00:00Z",
        "confidence": 0.8,
    }
    payload.update(overrides)
    return payload


def test_valid_extraction_parses():
    parsed = slack_listener.parse_extraction(valid_extraction())
    assert parsed["description"] == "send the Q3 draft"
    assert parsed["due_at"].year == 2026
    assert parsed["due_at"].tzinfo is not None


def test_extraction_rejected_when_no_commitment():
    assert slack_listener.parse_extraction(valid_extraction(has_commitment=False)) is None


def test_extraction_rejected_below_confidence_floor():
    """A wrong deadline is worse than a missed one."""
    assert slack_listener.parse_extraction(valid_extraction(confidence=0.3)) is None


def test_extraction_rejected_without_a_date():
    assert slack_listener.parse_extraction(valid_extraction(due_at=None)) is None


def test_extraction_rejected_with_unparseable_date():
    assert slack_listener.parse_extraction(valid_extraction(due_at="sometime soon")) is None


def test_extraction_rejected_without_description():
    assert slack_listener.parse_extraction(valid_extraction(description="  ")) is None


def test_extraction_rejected_for_non_dict():
    assert slack_listener.parse_extraction(["nope"]) is None
    assert slack_listener.parse_extraction(None) is None


def test_extraction_rejected_for_bad_confidence_type():
    assert slack_listener.parse_extraction(valid_extraction(confidence="high")) is None


# --- listener: event dedup ------------------------------------------------

def test_repeated_event_id_is_suppressed():
    """Slack redelivers events; handling one twice would double-record."""
    listener = slack_listener.SlackListener()
    assert listener.already_seen("Ev1") is False
    assert listener.already_seen("Ev1") is True


def test_distinct_event_ids_pass():
    listener = slack_listener.SlackListener()
    assert listener.already_seen("Ev1") is False
    assert listener.already_seen("Ev2") is False


def test_missing_event_id_is_never_treated_as_seen():
    listener = slack_listener.SlackListener()
    assert listener.already_seen("") is False
    assert listener.already_seen("") is False


def test_dedup_cache_evicts_without_leaking():
    """The id set must shrink with the deque, or it grows forever."""
    listener = slack_listener.SlackListener()
    listener._seen = type(listener._seen)(maxlen=3)
    listener._seen_set = set()

    for index in range(5):
        listener.already_seen("Ev{0}".format(index))

    assert len(listener._seen_set) == 3
    assert len(listener._seen) == 3
    # The oldest ids fell out, so they'd be processed again rather than
    # silently blocking forever.
    assert listener.already_seen("Ev0") is False
