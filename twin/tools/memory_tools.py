"""Tools the agent uses to read and extend its model of the user.

These are always available -- they need no OAuth and no external service.
"""

import datetime as dt
import json
from typing import Any, Dict

from twin.config import SETTINGS
from twin.memory import embeddings, store
from twin.timeutil import parse_iso
from twin.tools.registry import obj, tool


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------

@tool(
    name="remember_fact",
    description=(
        "Store a durable fact about the user in the twin profile -- a preference, "
        "a recurring commitment, a constraint, an observed pattern. Use this "
        "whenever the user reveals something that will still be true next week. "
        "Do not use it for one-off task details; those belong in deadlines."
    ),
    input_schema=obj(
        {
            "key": {
                "type": "string",
                "description": "Short snake_case identifier, e.g. 'preferred_work_hours'.",
            },
            "value": {"type": "string", "description": "The fact itself, in plain language."},
        },
        ["key", "value"],
    ),
)
def remember_fact(args: Dict[str, Any]) -> str:
    store.set_fact(args["key"], args["value"])
    return "Remembered: {0} = {1}".format(args["key"], args["value"])


@tool(
    name="add_interests",
    description=(
        "Add concrete interest tags to the user's profile. These drive proactive "
        "opportunity matching, so prefer specific tags ('algorithmic trading', "
        "'fingerstyle guitar') over vague ones ('tech', 'music')."
    ),
    input_schema=obj(
        {"tags": {"type": "array", "items": {"type": "string"}}},
        ["tags"],
    ),
)
def add_interests(args: Dict[str, Any]) -> str:
    interests = store.add_interests(args["tags"])
    return "Interests now: {0}".format(", ".join(interests))


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------

@tool(
    name="track_project",
    description=(
        "Register a personal project the user mentions, or update an existing "
        "one. Registering also counts as activity, so the staleness clock "
        "restarts. Call this any time the user talks about something they are "
        "working on or intend to start."
    ),
    input_schema=obj(
        {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["active", "paused", "done", "abandoned"],
            },
        },
        ["name"],
    ),
)
def track_project(args: Dict[str, Any]) -> str:
    project = store.upsert_project(
        name=args["name"],
        description=args.get("description", ""),
        status=args.get("status", "active"),
    )
    return "Tracking project '{0}' (status: {1}).".format(project["name"], project["status"])


@tool(
    name="log_project_activity",
    description=(
        "Record that the user worked on or made progress against a project, "
        "resetting its staleness clock. Use when they report progress rather "
        "than when they merely mention the project in passing."
    ),
    input_schema=obj({"name": {"type": "string"}}, ["name"]),
)
def log_project_activity(args: Dict[str, Any]) -> str:
    project = store.touch_project(args["name"])
    if project is None:
        return "No project named '{0}'. Use track_project to create it first.".format(
            args["name"]
        )
    return "Logged activity on '{0}'.".format(project["name"])


@tool(
    name="list_projects",
    description="List tracked projects with their status and how long since last activity.",
    input_schema=obj(
        {"status": {"type": "string", "enum": ["active", "paused", "done", "abandoned"]}}
    ),
)
def list_projects(args: Dict[str, Any]) -> str:
    projects = store.list_projects(args.get("status"))
    if not projects:
        return "No projects tracked yet."
    lines = []
    now = dt.datetime.now(dt.timezone.utc)
    for project in projects:
        days = (now - project["last_activity_at"]).days
        lines.append(
            "- {0} [{1}] last activity {2}d ago: {3}".format(
                project["name"], project["status"], days, project["description"] or "-"
            )
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Deadlines
# --------------------------------------------------------------------------

@tool(
    name="add_deadline",
    description=(
        "Record a deadline or action item with a due date. Provide due_at as an "
        "ISO-8601 timestamp. If the user gives a relative date ('next Friday'), "
        "resolve it against the current time given in your context first."
    ),
    input_schema=obj(
        {
            "description": {"type": "string"},
            "due_at": {
                "type": "string",
                "description": "ISO-8601, e.g. 2026-09-19T17:00:00Z",
            },
            "source": {
                "type": "string",
                "description": "Where this came from: conversation, gmail, calendar.",
            },
        },
        ["description", "due_at"],
    ),
)
def add_deadline(args: Dict[str, Any]) -> str:
    try:
        due_at = parse_iso(args["due_at"])
    except ValueError:
        return "Could not parse due_at '{0}'. Use ISO-8601.".format(args["due_at"])
    deadline = store.upsert_deadline(
        description=args["description"],
        due_at=due_at,
        source=args.get("source", "conversation"),
    )
    return "Deadline #{0} recorded: {1} due {2}.".format(
        deadline["id"], deadline["description"], deadline["due_at"].isoformat()
    )


@tool(
    name="list_deadlines",
    description="List open deadlines, optionally limited to the next N days.",
    input_schema=obj({"within_days": {"type": "integer"}}),
)
def list_deadlines(args: Dict[str, Any]) -> str:
    deadlines = store.list_open_deadlines(args.get("within_days"))
    if not deadlines:
        return "No open deadlines."
    return "\n".join(
        "- #{0} {1} (due {2}, from {3})".format(
            d["id"], d["description"], d["due_at"].isoformat(), d["source"]
        )
        for d in deadlines
    )


@tool(
    name="complete_deadline",
    description="Mark a deadline done or dismissed, by its id from list_deadlines.",
    input_schema=obj(
        {
            "deadline_id": {"type": "integer"},
            "status": {"type": "string", "enum": ["done", "dismissed"]},
        },
        ["deadline_id", "status"],
    ),
)
def complete_deadline(args: Dict[str, Any]) -> str:
    deadline = store.set_deadline_status(args["deadline_id"], args["status"])
    if deadline is None:
        return "No deadline #{0}.".format(args["deadline_id"])
    return "Deadline #{0} marked {1}.".format(deadline["id"], deadline["status"])


# --------------------------------------------------------------------------
# Recall
# --------------------------------------------------------------------------

@tool(
    name="recall",
    description=(
        "Search everything the twin has indexed -- past emails, messages, and "
        "earlier conversations -- for context relevant to a query. Use this "
        "before claiming you don't know something about the user."
    ),
    input_schema=obj(
        {"query": {"type": "string"}, "limit": {"type": "integer"}},
        ["query"],
    ),
)
def recall(args: Dict[str, Any]) -> str:
    query = args["query"]
    limit = args.get("limit", 5)

    if SETTINGS.embeddings_enabled:
        vector = embeddings.embed(query)
        results = store.search_similar(vector, limit=limit)
    else:
        # No embedding provider configured -- degrade rather than fail.
        results = store.keyword_search(query, limit=limit)

    if not results:
        return "Nothing indexed matches that."
    return "\n".join(
        "- [{0}] {1}".format(r["kind"], r["content"][:300]) for r in results
    )


@tool(
    name="get_profile",
    description=(
        "Read the full twin profile: priorities, interests, known facts, "
        "communication style, and settings. Use when you need to ground a "
        "suggestion in what you actually know about the user."
    ),
    input_schema=obj({}),
)
def get_profile(args: Dict[str, Any]) -> str:
    profile = store.get_profile()
    return json.dumps(
        {
            "priorities": profile["priorities"],
            "interests": profile["interests"],
            "facts": profile["facts"],
            "communication_style": profile["communication_style"],
            "staleness_threshold_days": profile["staleness_threshold_days"],
            "connected_apps": profile["connected_apps"],
        },
        indent=2,
        default=str,
    )
