"""Opportunity discovery -- Phase 3.

Finds activities, events, and competitions matching the user's tracked
interests, so the proactive layer can pair a real calendar opening with
something real to put in it.

Exa is the only source. The spec also names Eventbrite and Meetup, but both
have closed their public discovery APIs: Eventbrite removed public event
search in Dec 2019, and Meetup's GraphQL API gates OAuth consumers behind a
paid Pro subscription. `STRUCTURED_SOURCES` documents the seam where a
structured source would plug in if one becomes available.

Two-stage by design (spec section 10): Exa does broad neural retrieval, then a
cheap auxiliary model scores each candidate against the user's actual
interests. The expensive agent model never sees the raw result dump.
"""

import datetime as dt
from typing import Any, Dict, List, Optional, Sequence

import httpx

from twin import aux
from twin.config import SETTINGS
from twin.memory import embeddings, store
from twin.tools.registry import obj, tool

EXA_SEARCH_URL = "https://api.exa.ai/search"

# How far back to allow a page to have been published. Pages announcing an
# upcoming event are almost always recent; this filters out archived editions
# of the same recurring event.
PUBLISHED_WITHIN_DAYS = 120

# Neural search rewards focused queries, so interests are searched separately
# rather than mashed into one string. Capped to bound API spend per run.
MAX_QUERIES = 3
RESULTS_PER_QUERY = 8

# Below this, a candidate isn't worth interrupting the user for.
MIN_SCORE = 7

# No structured event source is currently usable without a paid subscription.
# A working adapter would return the same normalized shape as _normalize().
STRUCTURED_SOURCES: List[str] = []

SCORING_SYSTEM = """\
You score search results for a personal assistant that surfaces opportunities \
to one specific user. You are the filter that stops them being interrupted \
with junk, so be harsh.

Score each candidate 0-10 on whether it is a genuine, actionable, upcoming \
opportunity that this user would plausibly want.

Score 0 if it is:
- a news article, blog post, listicle, or roundup *about* the topic rather \
than something the user can actually attend, enter, join, or apply to
- an event that has already happened, or whose deadline has passed
- a generic directory, aggregator landing page, or ticket-vendor category page
- paywalled, corporate-internal, or otherwise not open to an individual

Reduce the score substantially if the location is wrong and the thing is not \
remote/online.

Only score 8+ when you would confidently bet the user is glad you interrupted \
them for it.

Respond with ONLY a JSON array, one object per candidate, in the same order:
[{"index": 0, "score": 7, "kind": "hackathon", "when": "2026-10-03 or null", \
"reason": "one short clause"}]
"""


class DiscoveryUnavailable(RuntimeError):
    """Discovery can't run -- typically a missing API key."""


# --------------------------------------------------------------------------
# Query building
# --------------------------------------------------------------------------

def build_query(interest: str, location: str = "", days: int = 30) -> str:
    """Turn one interest into a natural-language neural-search query."""
    parts = [
        "upcoming events, meetups, competitions, hackathons, workshops or "
        "programmes about {0}".format(interest.strip())
    ]
    if location.strip():
        parts.append("in or near {0}".format(location.strip()))
    parts.append("happening within the next {0} days".format(days))
    return ", ".join(parts)


def build_queries(
    interests: Sequence[str],
    location: str = "",
    days: int = 30,
    limit: int = MAX_QUERIES,
) -> List[str]:
    """One focused query per interest, capped to bound API spend."""
    return [build_query(interest, location, days) for interest in interests[:limit]]


# --------------------------------------------------------------------------
# Exa
# --------------------------------------------------------------------------

def _normalize(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = raw.get("url")
    if not url:
        return None
    return {
        "title": raw.get("title") or "(untitled)",
        "url": url,
        "published_date": raw.get("publishedDate") or "",
        "author": raw.get("author") or "",
        "snippet": (raw.get("text") or raw.get("summary") or "").strip()[:800],
    }


def exa_search(query: str, num_results: int = RESULTS_PER_QUERY) -> List[Dict[str, Any]]:
    """Run one Exa search and return normalized results."""
    if not SETTINGS.exa_api_key:
        raise DiscoveryUnavailable(
            "EXA_API_KEY is not set -- opportunity discovery is disabled."
        )

    published_after = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=PUBLISHED_WITHIN_DAYS
    )

    response = httpx.post(
        EXA_SEARCH_URL,
        headers={
            "x-api-key": SETTINGS.exa_api_key,
            "Content-Type": "application/json",
        },
        json={
            "query": query,
            "type": "auto",
            "numResults": num_results,
            "startPublishedDate": published_after.isoformat(),
            "contents": {"text": {"maxCharacters": 800}},
        },
        timeout=45.0,
    )
    response.raise_for_status()

    results = []
    for raw in response.json().get("results", []):
        normalized = _normalize(raw)
        if normalized is not None:
            results.append(normalized)
    return results


def dedupe(results: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop repeats by URL, preserving first-seen order."""
    seen = set()
    unique = []
    for result in results:
        url = result["url"]
        if url in seen:
            continue
        seen.add(url)
        unique.append(result)
    return unique


def drop_already_surfaced(results: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove opportunities the user has already been shown."""
    return [r for r in results if not store.has_embedding("opportunity", r["url"])]


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def render_candidates(results: Sequence[Dict[str, Any]]) -> str:
    blocks = []
    for index, result in enumerate(results):
        blocks.append(
            "[{0}] {1}\nurl: {2}\npublished: {3}\n{4}".format(
                index,
                result["title"],
                result["url"],
                result["published_date"] or "unknown",
                result["snippet"][:500],
            )
        )
    return "\n\n".join(blocks)


def apply_scores(
    results: Sequence[Dict[str, Any]],
    scores: Any,
    min_score: int = MIN_SCORE,
) -> List[Dict[str, Any]]:
    """Attach scores to results, filter by threshold, rank best-first.

    Tolerates a malformed scorer response: anything that doesn't map cleanly
    onto a candidate is dropped rather than surfaced unscored.
    """
    if not isinstance(scores, list):
        return []

    scored = []
    for entry in scores:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("index"))
            score = float(entry.get("score"))
        except (TypeError, ValueError):
            continue
        if not (0 <= index < len(results)):
            continue
        if score < min_score:
            continue

        result = dict(results[index])
        result["score"] = score
        result["kind"] = entry.get("kind") or "opportunity"
        result["when"] = entry.get("when") or ""
        result["reason"] = entry.get("reason") or ""
        scored.append(result)

    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored


def score_candidates(
    results: Sequence[Dict[str, Any]],
    interests: Sequence[str],
    location: str = "",
    min_score: int = MIN_SCORE,
) -> List[Dict[str, Any]]:
    """Score candidates with the cheap model in a single batched call."""
    if not results:
        return []

    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    user_block = (
        "Today's date: {0}\n"
        "User's interests: {1}\n"
        "User's location: {2}\n\n"
        "Candidates:\n\n{3}"
    ).format(
        today,
        ", ".join(interests) or "unknown",
        location or "unknown",
        render_candidates(results),
    )

    try:
        scores = aux.complete_json(SCORING_SYSTEM, user_block, max_tokens=2000)
    except aux.AuxError:
        # A failed scorer must not surface an unfiltered result dump.
        return []

    return apply_scores(results, scores, min_score)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def mark_surfaced(result: Dict[str, Any]) -> None:
    """Record that an opportunity has been shown, so it isn't repeated."""
    content = "{0}\n{1}\n{2}".format(
        result["title"], result["url"], result.get("reason", "")
    )
    vector = None
    try:
        vector = embeddings.embed(content)
    except Exception:
        pass
    store.store_embedding(
        kind="opportunity",
        ref_id=result["url"],
        content=content,
        vector=vector,
        metadata={
            "title": result["title"],
            "score": result.get("score"),
            "kind": result.get("kind"),
            "when": result.get("when"),
        },
    )


def discover(
    interests: Sequence[str],
    location: str = "",
    days: int = 30,
    limit: int = 5,
    min_score: int = MIN_SCORE,
    focus: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Search, score, and rank opportunities matching the user's interests."""
    search_terms = [focus] if focus else list(interests)
    if not search_terms:
        return []

    raw: List[Dict[str, Any]] = []
    for query in build_queries(search_terms, location, days):
        try:
            raw.extend(exa_search(query))
        except DiscoveryUnavailable:
            raise
        except Exception:
            # One bad query shouldn't sink the whole run.
            continue

    candidates = drop_already_surfaced(dedupe(raw))
    scored = score_candidates(candidates, search_terms, location, min_score)
    return scored[:limit]


def format_results(results: Sequence[Dict[str, Any]]) -> str:
    lines = []
    for result in results:
        lines.append(
            "- [{0}/10] {1} ({2})\n  {3}\n  {4}{5}".format(
                int(result["score"]),
                result["title"],
                result.get("kind", "opportunity"),
                result["url"],
                result.get("reason", ""),
                " | when: {0}".format(result["when"]) if result.get("when") else "",
            )
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Tool
# --------------------------------------------------------------------------

@tool(
    name="find_opportunities",
    description=(
        "Search the web for real, upcoming opportunities -- events, meetups, "
        "hackathons, competitions, programmes -- matched against the user's "
        "tracked interests and location. Results are pre-filtered for genuine "
        "actionable opportunities, so anything returned is worth mentioning. "
        "Pass `focus` to search one specific thing instead of their whole "
        "interest list. Anything you surface is recorded, so it won't be "
        "offered again."
    ),
    input_schema=obj(
        {
            "focus": {
                "type": "string",
                "description": "Optional single topic to search instead of all interests.",
            },
            "days": {
                "type": "integer",
                "description": "How far ahead to look. Default 30.",
            },
            "limit": {"type": "integer", "description": "Max results. Default 5."},
        }
    ),
)
def find_opportunities(args: Dict[str, Any]) -> str:
    profile = store.get_profile()
    interests = list(profile.get("interests") or [])
    focus = args.get("focus")

    if not interests and not focus:
        return (
            "No interests recorded yet, and no focus given. Ask the user what "
            "they're into, save it with add_interests, then search again."
        )

    try:
        results = discover(
            interests=interests,
            location=profile.get("location") or "",
            days=int(args.get("days", 30)),
            limit=int(args.get("limit", 5)),
            focus=focus,
        )
    except DiscoveryUnavailable as exc:
        return str(exc)

    if not results:
        return (
            "Nothing worth surfacing for {0}. Either there's nothing genuine "
            "coming up, or everything found was already shown before.".format(
                focus or ", ".join(interests)
            )
        )

    for result in results:
        mark_surfaced(result)

    return format_results(results)
