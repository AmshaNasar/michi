"""Accessors over the digital-twin memory model.

Every read the agent performs and every fact it learns goes through here, so
the profile stays the single source of truth about the user.
"""

import datetime as dt
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
from psycopg2.extras import Json

from twin.memory.db import cursor


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------

def get_profile() -> Dict[str, Any]:
    with cursor() as cur:
        cur.execute("SELECT * FROM user_profile WHERE id = 1")
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO user_profile (id) VALUES (1) RETURNING *")
            row = cur.fetchone()
        return dict(row)


_PROFILE_JSON_FIELDS = {
    "priorities",
    "interests",
    "connected_apps",
    "facts",
    "active_hours",
}
_PROFILE_WRITABLE = _PROFILE_JSON_FIELDS | {
    "staleness_threshold_days",
    "communication_style",
    "onboarded_at",
    "timezone",
    "location",
    "last_opportunity_scan",
    "last_deadline_scan",
    "gmail_history_id",
    "gmail_watch_expires_at",
}


def update_profile(**fields: Any) -> Dict[str, Any]:
    """Patch named profile columns. Unknown fields are rejected loudly."""
    unknown = set(fields) - _PROFILE_WRITABLE
    if unknown:
        raise ValueError("Unknown profile fields: {0}".format(sorted(unknown)))
    if not fields:
        return get_profile()

    assignments = []
    values: List[Any] = []
    for name, value in fields.items():
        assignments.append("{0} = %s".format(name))
        values.append(Json(value) if name in _PROFILE_JSON_FIELDS else value)

    with cursor() as cur:
        cur.execute(
            "UPDATE user_profile SET {0}, updated_at = now() "
            "WHERE id = 1 RETURNING *".format(", ".join(assignments)),
            values,
        )
        return dict(cur.fetchone())


def set_fact(key: str, value: Any) -> None:
    """Record a durable observed fact about the user, timestamped."""
    entry = {"value": value, "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    with cursor() as cur:
        cur.execute(
            "UPDATE user_profile "
            "SET facts = facts || %s, updated_at = now() WHERE id = 1",
            (Json({key: entry}),),
        )


def add_interests(tags: Sequence[str]) -> List[str]:
    """Merge interest tags into the profile, preserving order and de-duping."""
    profile = get_profile()
    existing = list(profile.get("interests") or [])
    lowered = {tag.lower() for tag in existing}
    for tag in tags:
        cleaned = tag.strip()
        if cleaned and cleaned.lower() not in lowered:
            existing.append(cleaned)
            lowered.add(cleaned.lower())
    update_profile(interests=existing)
    return existing


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------

def upsert_project(
    name: str,
    description: str = "",
    status: str = "active",
    source: str = "conversation",
    last_activity_at: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """Create a project or refresh an existing one.

    `last_activity_at` defaults to now, which is right when the user just
    mentioned the project. Sync from an external system must pass that
    system's own timestamp instead -- stamping an imported project as active
    today would silently destroy the staleness signal it exists to provide.
    """
    with cursor() as cur:
        cur.execute(
            "INSERT INTO projects (name, description, status, source, last_activity_at) "
            "VALUES (%s, %s, %s, %s, COALESCE(%s, now())) "
            "ON CONFLICT (name) DO UPDATE SET "
            "  description = CASE WHEN EXCLUDED.description <> '' "
            "                     THEN EXCLUDED.description ELSE projects.description END, "
            "  status = EXCLUDED.status, "
            "  last_activity_at = EXCLUDED.last_activity_at "
            "RETURNING *",
            (name, description, status, source, last_activity_at),
        )
        return dict(cur.fetchone())


def touch_project(name: str) -> Optional[Dict[str, Any]]:
    """Mark a project as active right now. Returns None if it doesn't exist."""
    with cursor() as cur:
        cur.execute(
            "UPDATE projects SET last_activity_at = now() WHERE name = %s RETURNING *",
            (name,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_projects(status: Optional[str] = None) -> List[Dict[str, Any]]:
    with cursor() as cur:
        if status:
            cur.execute(
                "SELECT * FROM projects WHERE status = %s ORDER BY last_activity_at DESC",
                (status,),
            )
        else:
            cur.execute("SELECT * FROM projects ORDER BY last_activity_at DESC")
        return [dict(row) for row in cur.fetchall()]


def stale_projects(threshold_days: Optional[int] = None) -> List[Dict[str, Any]]:
    """Active projects untouched for longer than the user's configured threshold."""
    if threshold_days is None:
        threshold_days = get_profile()["staleness_threshold_days"]
    with cursor() as cur:
        cur.execute(
            "SELECT *, EXTRACT(DAY FROM now() - last_activity_at)::int AS days_stale "
            "FROM projects "
            "WHERE status = 'active' "
            "  AND last_activity_at < now() - make_interval(days => %s) "
            "ORDER BY last_activity_at ASC",
            (threshold_days,),
        )
        return [dict(row) for row in cur.fetchall()]


# --------------------------------------------------------------------------
# Deadlines
# --------------------------------------------------------------------------

def upsert_deadline(
    description: str,
    due_at: dt.datetime,
    source: str = "conversation",
    source_ref: Optional[str] = None,
    project_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Record a deadline. Re-extracting the same source item updates in place."""
    with cursor() as cur:
        if source_ref:
            cur.execute(
                "INSERT INTO deadlines (description, due_at, source, source_ref, project_id) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (source, source_ref) WHERE source_ref IS NOT NULL "
                "DO UPDATE SET description = EXCLUDED.description, "
                "              due_at = EXCLUDED.due_at "
                "RETURNING *",
                (description, due_at, source, source_ref, project_id),
            )
        else:
            cur.execute(
                "INSERT INTO deadlines (description, due_at, source, project_id) "
                "VALUES (%s, %s, %s, %s) RETURNING *",
                (description, due_at, source, project_id),
            )
        return dict(cur.fetchone())


def list_open_deadlines(within_days: Optional[int] = None) -> List[Dict[str, Any]]:
    with cursor() as cur:
        if within_days is None:
            cur.execute(
                "SELECT * FROM deadlines WHERE status = 'open' ORDER BY due_at ASC"
            )
        else:
            cur.execute(
                "SELECT * FROM deadlines WHERE status = 'open' "
                "  AND due_at <= now() + make_interval(days => %s) "
                "ORDER BY due_at ASC",
                (within_days,),
            )
        return [dict(row) for row in cur.fetchall()]


def set_deadline_status(deadline_id: int, status: str) -> Optional[Dict[str, Any]]:
    """Update a deadline's status, stamping completion time when it's done.

    Reopening clears the stamp, so an item toggled done and then reopened
    stops counting toward the streak.
    """
    with cursor() as cur:
        cur.execute(
            "UPDATE deadlines SET status = %s, "
            "  completed_at = CASE WHEN %s = 'done' THEN now() ELSE NULL END "
            "WHERE id = %s RETURNING *",
            (status, status, deadline_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def completion_stats(days: int = 7) -> Dict[str, Any]:
    """Counts behind the streak and progress displays."""
    with cursor() as cur:
        cur.execute(
            "SELECT count(*) AS kept FROM deadlines "
            "WHERE status = 'done' AND completed_at >= now() - make_interval(days => %s)",
            (days,),
        )
        kept = cur.fetchone()["kept"]

        cur.execute(
            "SELECT count(*) AS today FROM deadlines "
            "WHERE status = 'done' AND completed_at::date = current_date"
        )
        today = cur.fetchone()["today"]

        cur.execute(
            "SELECT DISTINCT completed_at::date AS day FROM deadlines "
            "WHERE status = 'done' AND completed_at IS NOT NULL "
            "ORDER BY day DESC LIMIT 90"
        )
        active_days = [row["day"] for row in cur.fetchall()]

    return {
        "kept_this_week": kept,
        "completed_today": today,
        "streak": streak_from_days(active_days),
    }


def streak_from_days(active_days: Sequence[dt.date], today: Optional[dt.date] = None) -> int:
    """Consecutive days ending today, or yesterday if today isn't worked yet.

    Anchoring to yesterday matters: at 9am, before anything is cleared, a real
    streak would otherwise read as zero and look broken.
    """
    if not active_days:
        return 0

    today = today or dt.date.today()
    expected = today if active_days[0] == today else today - dt.timedelta(days=1)

    streak = 0
    for day in active_days:
        if day == expected:
            streak += 1
            expected -= dt.timedelta(days=1)
        elif day < expected:
            break
    return streak


# --------------------------------------------------------------------------
# Conversation history
# --------------------------------------------------------------------------

def append_turn(role: str, content: Any) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO conversation_history (role, content) VALUES (%s, %s)",
            (role, Json(content)),
        )


def recent_turns(limit: int = 20) -> List[Dict[str, Any]]:
    """Most recent turns, oldest first, ready to replay into the agent loop."""
    with cursor() as cur:
        cur.execute(
            "SELECT role, content, created_at FROM conversation_history "
            "ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    rows.reverse()
    return rows


# --------------------------------------------------------------------------
# Embeddings / recall
# --------------------------------------------------------------------------

def store_embedding(
    kind: str,
    ref_id: str,
    content: str,
    vector: Optional[Sequence[float]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO embeddings (kind, ref_id, content, vector, metadata) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (kind, ref_id) DO UPDATE SET "
            "  content = EXCLUDED.content, "
            "  vector = EXCLUDED.vector, "
            "  metadata = EXCLUDED.metadata",
            (
                kind,
                ref_id,
                content,
                list(vector) if vector is not None else None,
                Json(metadata or {}),
            ),
        )


def has_embedding(kind: str, ref_id: str) -> bool:
    """Whether an item has already been indexed.

    Doubles as the "have I already shown them this?" check for opportunities,
    which avoids a second table just to track what's been surfaced.
    """
    with cursor() as cur:
        cur.execute(
            "SELECT 1 FROM embeddings WHERE kind = %s AND ref_id = %s",
            (kind, ref_id),
        )
        return cur.fetchone() is not None


def search_similar(
    query_vector: Sequence[float],
    kind: Optional[str] = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Cosine similarity search.

    Computed in numpy rather than pgvector (see schema.sql). Single-user
    corpora stay small enough that loading the vectors is cheaper than the
    operational cost of an extension that isn't installed.
    """
    with cursor() as cur:
        if kind:
            cur.execute(
                "SELECT id, kind, ref_id, content, vector, metadata FROM embeddings "
                "WHERE vector IS NOT NULL AND kind = %s",
                (kind,),
            )
        else:
            cur.execute(
                "SELECT id, kind, ref_id, content, vector, metadata FROM embeddings "
                "WHERE vector IS NOT NULL"
            )
        rows = [dict(row) for row in cur.fetchall()]

    if not rows:
        return []

    matrix = np.array([row["vector"] for row in rows], dtype=np.float32)
    query = np.array(query_vector, dtype=np.float32)

    norms = np.linalg.norm(matrix, axis=1) * np.linalg.norm(query)
    # Guard against zero-length vectors producing NaNs.
    norms[norms == 0] = 1e-9
    scores = matrix.dot(query) / norms

    ranked = np.argsort(-scores)[:limit]
    results = []
    for index in ranked:
        row = rows[int(index)]
        row.pop("vector", None)
        row["score"] = float(scores[int(index)])
        results.append(row)
    return results


def keyword_search(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Fallback recall when embeddings are not configured."""
    with cursor() as cur:
        cur.execute(
            "SELECT id, kind, ref_id, content, metadata FROM embeddings "
            "WHERE content ILIKE %s ORDER BY created_at DESC LIMIT %s",
            ("%{0}%".format(query), limit),
        )
        return [dict(row) for row in cur.fetchall()]


# --------------------------------------------------------------------------
# Nudges
# --------------------------------------------------------------------------

def queue_nudge(kind: str, message: str, evidence: Optional[Dict[str, Any]] = None) -> None:
    """Queue a proactive nudge, skipping one already pending for the same kind+message."""
    with cursor() as cur:
        cur.execute(
            "SELECT 1 FROM nudges WHERE kind = %s AND message = %s AND status = 'pending'",
            (kind, message),
        )
        if cur.fetchone():
            return
        cur.execute(
            "INSERT INTO nudges (kind, message, evidence) VALUES (%s, %s, %s)",
            (kind, message, Json(evidence or {})),
        )


def pending_nudges() -> List[Dict[str, Any]]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM nudges WHERE status = 'pending' ORDER BY created_at ASC"
        )
        return [dict(row) for row in cur.fetchall()]


def mark_nudges_delivered(nudge_ids: Sequence[int]) -> None:
    if not nudge_ids:
        return
    with cursor() as cur:
        cur.execute(
            "UPDATE nudges SET status = 'delivered', delivered_at = now() "
            "WHERE id = ANY(%s)",
            (list(nudge_ids),),
        )
