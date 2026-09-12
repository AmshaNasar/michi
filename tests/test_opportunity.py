"""Opportunity discovery: query building, JSON extraction, and score filtering.

The scoring filter is the safety-critical part -- it's the only thing standing
between an unfiltered web-search dump and the user being interrupted. A
failure there must produce *nothing*, never an unvetted list.
"""

import pytest

from twin import aux
from twin.config import SETTINGS
from twin.tools import opportunity_tools as opp


def result(url, title="thing", snippet="text"):
    return {
        "title": title,
        "url": url,
        "published_date": "2026-09-01",
        "author": "",
        "snippet": snippet,
    }


# --- query building -------------------------------------------------------

def test_build_query_includes_interest_location_and_window():
    query = opp.build_query("algorithmic trading", "Lagos, Nigeria", 14)
    assert "algorithmic trading" in query
    assert "Lagos, Nigeria" in query
    assert "14 days" in query


def test_build_query_omits_location_when_unset():
    query = opp.build_query("fingerstyle guitar", "", 30)
    assert "in or near" not in query


def test_build_queries_searches_interests_separately():
    """Neural search rewards focused queries, so interests aren't mashed together."""
    queries = opp.build_queries(["algotrading", "fingerstyle"], "Lagos", 30)
    assert len(queries) == 2
    assert "algotrading" in queries[0] and "fingerstyle" not in queries[0]
    assert "fingerstyle" in queries[1] and "algotrading" not in queries[1]


def test_build_queries_caps_api_spend():
    queries = opp.build_queries(["v", "w", "x", "y", "z"], "", 30, limit=3)
    assert len(queries) == 3


# --- dedupe ---------------------------------------------------------------

def test_dedupe_keeps_first_occurrence():
    items = [result("u1", "first"), result("u2"), result("u1", "second")]
    deduped = opp.dedupe(items)
    assert [r["url"] for r in deduped] == ["u1", "u2"]
    assert deduped[0]["title"] == "first"


# --- score filtering ------------------------------------------------------

def test_apply_scores_filters_below_threshold():
    results = [result("u1"), result("u2")]
    scores = [{"index": 0, "score": 9}, {"index": 1, "score": 4}]
    kept = opp.apply_scores(results, scores, min_score=7)
    assert [r["url"] for r in kept] == ["u1"]


def test_apply_scores_ranks_best_first():
    results = [result("u1"), result("u2"), result("u3")]
    scores = [
        {"index": 0, "score": 7},
        {"index": 1, "score": 10},
        {"index": 2, "score": 8},
    ]
    kept = opp.apply_scores(results, scores, min_score=7)
    assert [r["url"] for r in kept] == ["u2", "u3", "u1"]


def test_apply_scores_drops_out_of_range_index():
    """A hallucinated index must not crash or mis-attribute a score."""
    kept = opp.apply_scores([result("u1")], [{"index": 9, "score": 10}], min_score=7)
    assert kept == []


def test_apply_scores_drops_unparseable_entries():
    results = [result("u1"), result("u2")]
    scores = [
        {"index": "not-a-number", "score": 9},
        {"index": 1, "score": "high"},
        "garbage",
        {"index": 0, "score": 9},
    ]
    kept = opp.apply_scores(results, scores, min_score=7)
    assert [r["url"] for r in kept] == ["u1"]


def test_apply_scores_rejects_non_list_response():
    assert opp.apply_scores([result("u1")], {"score": 10}, min_score=7) == []


def test_apply_scores_carries_reason_and_kind_through():
    kept = opp.apply_scores(
        [result("u1")],
        [{"index": 0, "score": 9, "kind": "hackathon", "reason": "matches trading"}],
        min_score=7,
    )
    assert kept[0]["kind"] == "hackathon"
    assert kept[0]["reason"] == "matches trading"


def test_scorer_failure_surfaces_nothing(monkeypatch):
    """If the scoring model fails, we must not fall back to unfiltered results."""
    def boom(system, user, max_tokens=0):
        raise aux.AuxError("model down")

    monkeypatch.setattr(opp.aux, "complete_json", boom)
    assert opp.score_candidates([result("u1")], ["trading"], "Lagos") == []


def test_score_candidates_short_circuits_on_empty_input(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("should not call the model with no candidates")

    monkeypatch.setattr(opp.aux, "complete_json", fail)
    assert opp.score_candidates([], ["trading"], "Lagos") == []


# --- Exa client -----------------------------------------------------------

def test_exa_search_without_key_raises_unavailable(monkeypatch):
    monkeypatch.setattr(SETTINGS, "exa_api_key", None)
    with pytest.raises(opp.DiscoveryUnavailable):
        opp.exa_search("anything")


def test_normalize_skips_result_without_url():
    assert opp._normalize({"title": "x"}) is None


def test_normalize_prefers_text_then_summary():
    assert opp._normalize({"url": "u", "summary": "s"})["snippet"] == "s"
    assert opp._normalize({"url": "u", "text": "t", "summary": "s"})["snippet"] == "t"


def test_discover_with_no_interests_and_no_focus_returns_nothing():
    assert opp.discover(interests=[], location="Lagos") == []


# --- aux JSON extraction --------------------------------------------------

def test_extract_json_handles_bare_array():
    assert aux.extract_json('[{"index": 0}]') == [{"index": 0}]


def test_extract_json_handles_markdown_fences():
    assert aux.extract_json('```json\n[{"index": 1}]\n```') == [{"index": 1}]


def test_extract_json_handles_prose_wrapping():
    raw = 'Here are the scores:\n[{"index": 0, "score": 8}]\nHope that helps!'
    assert aux.extract_json(raw) == [{"index": 0, "score": 8}]


def test_extract_json_handles_objects():
    assert aux.extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_raises_on_no_json():
    with pytest.raises(aux.AuxError):
        aux.extract_json("I could not complete that request.")


def test_extract_json_raises_on_empty():
    with pytest.raises(aux.AuxError):
        aux.extract_json("")
