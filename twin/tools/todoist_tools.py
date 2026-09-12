"""Todoist adapter -- Tier 1b integration.

Todoist is where dated commitments already live, so the payoff here is
`todoist_sync_deadlines`: tasks with dates become tracked deadlines directly.
No model call is involved -- unlike email or Slack, a Todoist task already
carries a structured date, so inferring one would only add a way to be wrong.

Two details drive the date logic:

* Todoist distinguishes **due** (when you plan to work on it) from **deadline**
  (when it must actually be done). They are independent fields and either can
  exist alone, so `deadline` wins when present.
* A date with no time means "by end of that day" to a person, so it resolves to
  the end of the day in the user's own timezone rather than to midnight UTC.
"""

import datetime as dt
from typing import Any, Dict, List, Optional, Sequence

from twin.memory import store
from twin.timeutil import parse_iso, zone
from twin.tools import todoist_auth
from twin.tools.registry import obj, tool

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# Todoist priority is inverted from its UI labels: 4 is p1 (urgent), 1 is p4.
PRIORITY_LABELS = {4: "p1", 3: "p2", 2: "p3", 1: "p4"}


# --------------------------------------------------------------------------
# Pure: dates
# --------------------------------------------------------------------------

def end_of_day(day: dt.date, tz: dt.tzinfo) -> dt.datetime:
    """The last moment of a local day, as an aware datetime."""
    return dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=tz)


def _parse_date_field(
    field: Any,
    tz: dt.tzinfo,
    date_key: str = "date",
) -> Optional[dt.datetime]:
    """Resolve a Todoist date object into an aware datetime."""
    if not isinstance(field, dict):
        return None

    # A datetime is exact; use it as given.
    raw_datetime = field.get("datetime")
    if raw_datetime:
        try:
            parsed = parse_iso(raw_datetime)
        except (ValueError, TypeError):
            parsed = None
        if parsed is not None:
            # A floating datetime (no offset) is local to the user.
            if parsed.tzinfo is dt.timezone.utc and "Z" not in raw_datetime and "+" not in raw_datetime:
                naive = parsed.replace(tzinfo=None)
                field_tz = zone(field["timezone"]) if field.get("timezone") else tz
                return naive.replace(tzinfo=field_tz)
            return parsed

    raw_date = field.get(date_key)
    if not raw_date:
        return None
    try:
        day = dt.date.fromisoformat(str(raw_date)[:10])
    except ValueError:
        return None
    return end_of_day(day, tz)


def task_due_at(task: Dict[str, Any], tz: Optional[dt.tzinfo] = None) -> Optional[dt.datetime]:
    """When a task is actually due.

    `deadline` is Todoist's hard deadline and takes precedence over `due`,
    which is only a plan for when to work on it. Either may be absent.
    """
    tz = tz or dt.timezone.utc

    deadline = _parse_date_field(task.get("deadline"), tz)
    if deadline is not None:
        return deadline
    return _parse_date_field(task.get("due"), tz)


def is_recurring(task: Dict[str, Any]) -> bool:
    due = task.get("due")
    return bool(isinstance(due, dict) and due.get("is_recurring"))


def task_label(task: Dict[str, Any]) -> str:
    content = (task.get("content") or "").strip() or "(untitled task)"
    priority = task.get("priority")
    if priority in PRIORITY_LABELS and priority != 1:
        return "{0} [{1}]".format(content, PRIORITY_LABELS[priority])
    return content


def format_task(task: Dict[str, Any], tz: Optional[dt.tzinfo] = None) -> str:
    tz = tz or dt.timezone.utc
    due_at = task_due_at(task, tz)
    parts = ["- {0}".format(task_label(task))]
    if due_at:
        parts.append("due {0}".format(due_at.astimezone(tz).strftime("%a %d %b %H:%M")))
    if is_recurring(task):
        parts.append("(recurring)")
    parts.append("id={0}".format(task.get("id", "")))
    return " | ".join(parts)


def _tz() -> dt.tzinfo:
    return zone(store.get_profile().get("timezone") or "UTC")


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

def sync_deadlines(
    tasks: Sequence[Dict[str, Any]],
    tz: Optional[dt.tzinfo] = None,
    include_recurring: bool = False,
) -> List[Dict[str, Any]]:
    """Record dated Todoist tasks as tracked deadlines.

    Recurring tasks are skipped by default: a daily chore regenerating forever
    is not a deadline, and letting them in would bury the real ones.
    """
    tz = tz or dt.timezone.utc
    recorded = []

    for task in tasks:
        if not include_recurring and is_recurring(task):
            continue
        due_at = task_due_at(task, tz)
        if due_at is None:
            # An undated task is a someday item, not a deadline.
            continue
        task_id = str(task.get("id") or "")
        if not task_id:
            continue

        stored = store.upsert_deadline(
            description=(task.get("content") or "").strip() or "(untitled task)",
            due_at=due_at,
            source="todoist",
            source_ref=task_id,
        )
        recorded.append(stored)
    return recorded


def fetch_tasks(project_id: Optional[str] = None, limit: int = MAX_LIMIT) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {}
    if project_id:
        params["project_id"] = project_id
    return todoist_auth.paginate("/tasks", params=params, limit=limit)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@tool(
    name="todoist_list_projects",
    description="List the user's Todoist projects with their ids.",
    input_schema=obj({}),
    requires="todoist",
)
def todoist_list_projects(args: Dict[str, Any]) -> str:
    try:
        projects = todoist_auth.paginate("/projects")
    except (todoist_auth.TodoistError, todoist_auth.NotConnected) as exc:
        return str(exc)

    if not projects:
        return "No Todoist projects found."
    return "\n".join(
        "- {0} | id={1}{2}".format(
            project.get("name", "(unnamed)"),
            project.get("id", ""),
            " [inbox]" if project.get("is_inbox_project") else "",
        )
        for project in projects
    )


@tool(
    name="todoist_list_tasks",
    description=(
        "List the user's open Todoist tasks, optionally for one project or "
        "only those due within N days. Shows each task's real due date, "
        "preferring Todoist's deadline field over its planned date."
    ),
    input_schema=obj(
        {
            "project_id": {"type": "string", "description": "Restrict to one project."},
            "due_within_days": {
                "type": "integer",
                "description": "Only tasks due within this many days.",
            },
            "limit": {"type": "integer", "description": "Max tasks, default 50."},
        }
    ),
    requires="todoist",
)
def todoist_list_tasks(args: Dict[str, Any]) -> str:
    tz = _tz()
    try:
        tasks = fetch_tasks(args.get("project_id"), limit=MAX_LIMIT)
    except (todoist_auth.TodoistError, todoist_auth.NotConnected) as exc:
        return str(exc)

    within = args.get("due_within_days")
    if within is not None:
        # Filtered here rather than server-side: v1's filter syntax is a
        # separate query language, and a personal task list is small enough
        # that filtering locally is both simpler and exact.
        horizon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=int(within))
        tasks = [
            task for task in tasks
            if (task_due_at(task, tz) or None) is not None
            and task_due_at(task, tz) <= horizon
        ]

    tasks = tasks[: int(args.get("limit", DEFAULT_LIMIT))]
    if not tasks:
        return "No matching Todoist tasks."

    tasks.sort(key=lambda t: task_due_at(t, tz) or dt.datetime.max.replace(tzinfo=dt.timezone.utc))
    return "\n".join(format_task(task, tz) for task in tasks)


@tool(
    name="todoist_sync_deadlines",
    description=(
        "Import dated Todoist tasks into the twin's deadline tracking, so they "
        "show up in reminders alongside deadlines found in email and calendar. "
        "Recurring chores are skipped. Re-running updates rather than "
        "duplicating."
    ),
    input_schema=obj(
        {
            "project_id": {"type": "string", "description": "Restrict to one project."},
            "include_recurring": {
                "type": "boolean",
                "description": "Include recurring tasks. Default false.",
            },
        }
    ),
    requires="todoist",
)
def todoist_sync_deadlines(args: Dict[str, Any]) -> str:
    tz = _tz()
    try:
        tasks = fetch_tasks(args.get("project_id"))
    except (todoist_auth.TodoistError, todoist_auth.NotConnected) as exc:
        return str(exc)

    recorded = sync_deadlines(
        tasks, tz, include_recurring=bool(args.get("include_recurring", False))
    )
    if not recorded:
        return "No dated Todoist tasks to import."

    return "Imported {0} deadline(s) from Todoist:\n{1}".format(
        len(recorded),
        "\n".join(
            "- #{0} {1} (due {2})".format(
                item["id"],
                item["description"],
                item["due_at"].astimezone(tz).strftime("%a %d %b %H:%M"),
            )
            for item in recorded
        ),
    )


@tool(
    name="todoist_create_task",
    description=(
        "Create a task in Todoist. `due_string` accepts Todoist's natural "
        "language ('tomorrow at 5pm', 'every monday'). This changes the user's "
        "task list -- they must confirm before it runs."
    ),
    input_schema=obj(
        {
            "content": {"type": "string", "description": "Task title."},
            "description": {"type": "string"},
            "project_id": {"type": "string", "description": "Defaults to Inbox."},
            "due_string": {
                "type": "string",
                "description": "Natural-language due date, e.g. 'friday at 3pm'.",
            },
            "priority": {
                "type": "integer",
                "description": "1-4, where 4 is most urgent (Todoist's p1).",
            },
        },
        ["content"],
    ),
    write=True,
    requires="todoist",
)
def todoist_create_task(args: Dict[str, Any]) -> str:
    body: Dict[str, Any] = {"content": args["content"]}
    for key in ("description", "project_id", "due_string"):
        if args.get(key):
            body[key] = args[key]
    if args.get("priority"):
        body["priority"] = max(1, min(int(args["priority"]), 4))

    try:
        created = todoist_auth.request("POST", "/tasks", json=body)
    except (todoist_auth.TodoistError, todoist_auth.NotConnected) as exc:
        return str(exc)

    due_at = task_due_at(created or {}, _tz())
    return "Created Todoist task '{0}'{1} (id {2}).".format(
        args["content"],
        " due {0}".format(due_at.isoformat(timespec="minutes")) if due_at else "",
        (created or {}).get("id", "?"),
    )


@tool(
    name="todoist_complete_task",
    description=(
        "Mark a Todoist task complete, by id from todoist_list_tasks. This "
        "changes the user's task list -- they must confirm before it runs."
    ),
    input_schema=obj({"task_id": {"type": "string"}}, ["task_id"]),
    write=True,
    requires="todoist",
)
def todoist_complete_task(args: Dict[str, Any]) -> str:
    try:
        todoist_auth.request("POST", "/tasks/{0}/close".format(args["task_id"]))
    except (todoist_auth.TodoistError, todoist_auth.NotConnected) as exc:
        return str(exc)

    # Keep the twin's own record in step with Todoist.
    for deadline in store.list_open_deadlines():
        if deadline["source"] == "todoist" and deadline["source_ref"] == args["task_id"]:
            store.set_deadline_status(deadline["id"], "done")
            break

    return "Completed Todoist task {0}.".format(args["task_id"])
