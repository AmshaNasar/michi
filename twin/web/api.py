"""Local web UI for the twin.

Serves the Michi interface and a small JSON API over the same memory layer the
CLI uses. Nothing new is computed here -- this is a second front end onto
`twin.memory.store` and `twin.agent`, not a second source of truth.

Bound to localhost only. The spec rules out remote access (section 5), and
there is no authentication here precisely because there is nothing to
authenticate against: it is one user, on their own machine.
"""

import datetime as dt
import os
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from twin.agent import Agent
from twin.memory import store
from twin.memory.db import apply_schema
from twin.timeutil import zone

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# The sakura has seven stages; each cleared commitment advances one, so a
# full day's list lands somewhere in the middle rather than instantly maxing.
TREE_STAGES = 7
STAGE_NAMES = [
    "First bud",
    "Rooted",
    "Young tree",
    "Branching",
    "First bloom",
    "Full canopy",
    "Ancient sakura",
]

# Which icon the UI shows per item, derived from where the item came from.
SOURCE_KIND = {
    "gmail": "email",
    "calendar": "calendar",
    "slack": "email",
    "todoist": "deadline",
    "notion": "project",
    "conversation": "deadline",
}

app = FastAPI(title="Michi — digital twin", docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------
# Shaping (pure)
# --------------------------------------------------------------------------

def humanize_due(due_at: dt.datetime, now: dt.datetime, tz: dt.tzinfo) -> str:
    """A short, human phrase for when something is due."""
    delta = due_at - now
    hours = delta.total_seconds() / 3600

    if hours < 0:
        overdue = abs(hours)
        if overdue < 24:
            return "{0}h overdue".format(int(overdue))
        return "{0}d overdue".format(int(overdue // 24))
    if hours < 1:
        return "{0} min".format(max(int(delta.total_seconds() // 60), 1))
    if hours < 24:
        return "{0}h".format(int(hours))
    if due_at.astimezone(tz).date() == (now.astimezone(tz) + dt.timedelta(days=1)).date():
        return "Tomorrow"
    return due_at.astimezone(tz).strftime("%a %d %b")


def tree_stage(completed_today: int) -> int:
    """Index into the seven growth stages, clamped to the last one."""
    return min(TREE_STAGES - 1, max(0, completed_today))


def greeting(now_local: dt.datetime) -> str:
    hour = now_local.hour
    if hour < 12:
        return "Good morning"
    if hour < 18:
        return "Good afternoon"
    return "Good evening"


def build_quests(
    deadlines: List[Dict[str, Any]],
    stale: List[Dict[str, Any]],
    now: dt.datetime,
    tz: dt.tzinfo,
) -> List[Dict[str, Any]]:
    """Merge deadlines and stalled projects into one list for the UI.

    Both are things the twin thinks the user owes attention to, and the
    original design treats them as one list of "quests" with different icons.
    """
    quests = []

    for deadline in deadlines:
        quests.append(
            {
                "id": "deadline-{0}".format(deadline["id"]),
                "kind": SOURCE_KIND.get(deadline["source"], "deadline"),
                "title": deadline["description"],
                "detail": "From {0} · due {1}".format(
                    deadline["source"],
                    deadline["due_at"].astimezone(tz).strftime("%a %d %b %H:%M"),
                ),
                "time": humanize_due(deadline["due_at"], now, tz),
                "completed": False,
                "deferrable": True,
            }
        )

    for project in stale:
        quests.append(
            {
                "id": "project-{0}".format(project["id"]),
                "kind": "project",
                "title": "Pick up {0}".format(project["name"]),
                "detail": "No activity for {0} days".format(project["days_stale"]),
                "time": "{0}d".format(project["days_stale"]),
                "completed": False,
                "deferrable": False,
            }
        )

    return quests


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def current_state() -> Dict[str, Any]:
    profile = store.get_profile()
    tz = zone(profile.get("timezone") or "UTC")
    now = dt.datetime.now(dt.timezone.utc)
    now_local = now.astimezone(tz)

    deadlines = store.list_open_deadlines()
    stale = store.stale_projects()
    stats = store.completion_stats()
    nudges = store.pending_nudges()

    quests = build_quests(deadlines, stale, now, tz)
    completed_today = stats["completed_today"]
    total = len(quests) + completed_today
    stage = tree_stage(completed_today)

    if nudges:
        notice = nudges[0]["message"]
    elif quests:
        notice = "{0} thing(s) want your attention. Start with the one that's closest to due.".format(
            len(quests)
        )
    else:
        notice = "Nothing is overdue and no project has gone quiet. Enjoy it."

    return {
        "greeting": "{0}.".format(greeting(now_local)),
        "date": now_local.strftime("%A · %B %-d"),
        "notice": notice,
        "quests": quests,
        "completed_today": completed_today,
        "total_today": total,
        "progress": int(round(100 * completed_today / total)) if total else 0,
        "stage": stage,
        "stage_name": STAGE_NAMES[stage],
        "stage_names": STAGE_NAMES,
        "streak": stats["streak"],
        "kept_this_week": stats["kept_this_week"],
        "connected": profile.get("connected_apps") or [],
        "interests": profile.get("interests") or [],
        "open_deadlines": len(deadlines),
        "stale_projects": len(stale),
        "projects": len(store.list_projects("active")),
    }


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str


class ProjectRequest(BaseModel):
    name: str


@app.get("/api/state")
def get_state() -> Dict[str, Any]:
    return current_state()


@app.post("/api/chat")
def post_chat(request: ChatRequest) -> Dict[str, Any]:
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Empty message.")

    # Write tools stay denied here: the browser has no confirmation UI, and
    # the gate defaults to refusing rather than silently allowing.
    agent = Agent()
    agent.load_history(limit=20)
    try:
        reply = agent.send(message)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Agent failed: {0}".format(exc))

    return {"reply": reply, "state": current_state()}


@app.post("/api/quests/{quest_id}/complete")
def complete_quest(quest_id: str) -> Dict[str, Any]:
    kind, _, raw_id = quest_id.partition("-")

    if kind == "deadline":
        updated = store.set_deadline_status(int(raw_id), "done")
        if updated is None:
            raise HTTPException(status_code=404, detail="No such deadline.")
    elif kind == "project":
        projects = {p["id"]: p for p in store.list_projects()}
        project = projects.get(int(raw_id))
        if project is None:
            raise HTTPException(status_code=404, detail="No such project.")
        # Completing a stale-project nudge means "I worked on it", which is
        # activity, not completion -- the project itself stays active.
        store.touch_project(project["name"])
    else:
        raise HTTPException(status_code=400, detail="Unknown quest type.")

    return current_state()


@app.post("/api/quests/{quest_id}/defer")
def defer_quest(quest_id: str) -> Dict[str, Any]:
    kind, _, raw_id = quest_id.partition("-")
    if kind != "deadline":
        raise HTTPException(status_code=400, detail="Only deadlines can be deferred.")

    updated = store.set_deadline_status(int(raw_id), "dismissed")
    if updated is None:
        raise HTTPException(status_code=404, detail="No such deadline.")
    return current_state()


@app.post("/api/nudges/dismiss")
def dismiss_nudges() -> Dict[str, Any]:
    store.mark_nudges_delivered([n["id"] for n in store.pending_nudges()])
    return current_state()


@app.post("/api/projects")
def add_project(request: ProjectRequest) -> Dict[str, Any]:
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Empty project name.")
    store.upsert_project(name=name, source="web")
    return current_state()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Run the UI. Localhost-bound by default; v1 is not a networked app."""
    import uvicorn

    apply_schema()
    uvicorn.run(app, host=host, port=port, log_level="warning")
