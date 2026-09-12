"""Notion adapter -- Tier 1b integration.

Notion is usually where project state actually lives, so the valuable part of
this integration isn't another read API: it's `notion_sync_projects`, which
pulls a Notion database into the twin's project registry so the staleness
nudges from Phase 2 run against real project state.

Built for the post-2025-09-03 data model: databases contain one or more *data
sources*, and queries address the data source, not the database. Search filters
on `data_source` rather than `database` for the same reason.

Notion's property format is deeply nested and varies per type, so flattening it
is pure and tested directly -- it's where the fiddly bugs live.
"""

import datetime as dt
from typing import Any, Dict, List, Optional, Sequence

from twin.memory import embeddings, store
from twin.timeutil import parse_iso
from twin.tools import notion_auth
from twin.tools.registry import obj, tool

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
MAX_BLOCKS = 100

# Notion status/select values mapped onto the twin's project states. Compared
# case-insensitively against the whole value.
STATUS_MAP = {
    "done": "done",
    "complete": "done",
    "completed": "done",
    "shipped": "done",
    "launched": "done",
    "archived": "done",
    "paused": "paused",
    "on hold": "paused",
    "blocked": "paused",
    "backlog": "paused",
    "someday": "paused",
    "not started": "paused",
    "cancelled": "abandoned",
    "canceled": "abandoned",
    "abandoned": "abandoned",
    "dropped": "abandoned",
    "wont do": "abandoned",
    "won't do": "abandoned",
}

# Property names checked, in order, when looking for a project description.
DESCRIPTION_KEYS = ("description", "notes", "summary", "details")


# --------------------------------------------------------------------------
# Pure: Notion's wire format -> plain values
# --------------------------------------------------------------------------

def rich_text_to_plain(rich_text: Any) -> str:
    """Flatten a Notion rich-text array to a string."""
    if not isinstance(rich_text, list):
        return ""
    return "".join(
        span.get("plain_text", "")
        for span in rich_text
        if isinstance(span, dict)
    ).strip()


def plain_value(prop: Any) -> str:
    """Render any Notion property value as a readable string."""
    if not isinstance(prop, dict):
        return ""

    kind = prop.get("type", "")
    value = prop.get(kind)

    if kind in {"title", "rich_text"}:
        return rich_text_to_plain(value)
    if kind == "number":
        return "" if value is None else str(value)
    if kind in {"select", "status"}:
        return (value or {}).get("name", "") if isinstance(value, dict) else ""
    if kind == "multi_select":
        return ", ".join(
            item.get("name", "") for item in (value or []) if isinstance(item, dict)
        )
    if kind == "date":
        if not isinstance(value, dict):
            return ""
        start = value.get("start") or ""
        end = value.get("end")
        return "{0} - {1}".format(start, end) if end else start
    if kind == "checkbox":
        return "yes" if value else "no"
    if kind in {"url", "email", "phone_number"}:
        return value or ""
    if kind in {"created_time", "last_edited_time"}:
        return value or ""
    if kind == "people":
        return ", ".join(
            person.get("name", "") for person in (value or []) if isinstance(person, dict)
        )
    if kind == "files":
        return ", ".join(
            item.get("name", "") for item in (value or []) if isinstance(item, dict)
        )
    if kind == "relation":
        count = len(value or [])
        return "{0} linked".format(count) if count else ""
    if kind in {"created_by", "last_edited_by"}:
        return (value or {}).get("name", "") if isinstance(value, dict) else ""
    if kind == "unique_id":
        if not isinstance(value, dict):
            return ""
        prefix = value.get("prefix") or ""
        return "{0}{1}".format("{0}-".format(prefix) if prefix else "", value.get("number", ""))
    if kind == "formula":
        # A formula wraps its own typed value, so recurse on the inner shape.
        return plain_value(value) if isinstance(value, dict) else ""
    if kind == "rollup":
        if not isinstance(value, dict):
            return ""
        if value.get("type") == "array":
            return ", ".join(plain_value(item) for item in value.get("array", []))
        return plain_value(value)
    return ""


def page_title(page: Dict[str, Any]) -> str:
    """Find a page's title, whichever property happens to hold it."""
    properties = page.get("properties") or {}
    for prop in properties.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            title = rich_text_to_plain(prop.get("title"))
            if title:
                return title
    # Data sources carry their title at the top level instead.
    top_level = page.get("title")
    if top_level:
        return rich_text_to_plain(top_level)
    return "(untitled)"


def property_named(page: Dict[str, Any], names: Sequence[str]) -> str:
    """First non-empty value among properties whose name matches (case-insensitive)."""
    properties = page.get("properties") or {}
    lowered = {key.lower(): value for key, value in properties.items()}
    for name in names:
        if name in lowered:
            value = plain_value(lowered[name])
            if value:
                return value
    return ""


def project_status(page: Dict[str, Any]) -> str:
    """Map a page's status/select property onto a twin project status."""
    properties = page.get("properties") or {}
    for prop in properties.values():
        if not isinstance(prop, dict) or prop.get("type") not in {"status", "select"}:
            continue
        value = plain_value(prop).strip().lower()
        if value in STATUS_MAP:
            return STATUS_MAP[value]
    # An unrecognised or absent status means the project is live, which is the
    # safe default: it stays visible to the staleness check.
    return "active"


def last_edited(page: Dict[str, Any]) -> Optional[dt.datetime]:
    raw = page.get("last_edited_time")
    if not raw:
        return None
    try:
        return parse_iso(raw)
    except (ValueError, TypeError):
        return None


def block_to_text(block: Dict[str, Any]) -> str:
    """Render one block as a line of plain text."""
    if not isinstance(block, dict):
        return ""

    kind = block.get("type", "")
    body = block.get(kind)
    if not isinstance(body, dict):
        return "---" if kind == "divider" else ""

    text = rich_text_to_plain(body.get("rich_text"))

    if kind == "paragraph":
        return text
    if kind == "heading_1":
        return "# {0}".format(text)
    if kind == "heading_2":
        return "## {0}".format(text)
    if kind == "heading_3":
        return "### {0}".format(text)
    if kind == "bulleted_list_item":
        return "- {0}".format(text)
    if kind == "numbered_list_item":
        return "1. {0}".format(text)
    if kind == "to_do":
        return "- [{0}] {1}".format("x" if body.get("checked") else " ", text)
    if kind in {"toggle", "quote", "callout"}:
        return "> {0}".format(text) if kind == "quote" else text
    if kind == "code":
        return "```{0}\n{1}\n```".format(body.get("language", ""), text)
    if kind == "child_page":
        return "[page] {0}".format(body.get("title", ""))
    if kind == "child_database":
        return "[database] {0}".format(body.get("title", ""))
    if kind == "divider":
        return "---"
    return text


def blocks_to_text(blocks: Sequence[Dict[str, Any]]) -> str:
    lines = [block_to_text(block) for block in blocks]
    return "\n".join(line for line in lines if line)


# --------------------------------------------------------------------------
# API helpers
# --------------------------------------------------------------------------

def search(query: str = "", object_type: Optional[str] = None, limit: int = DEFAULT_PAGE_SIZE):
    """Search pages and data sources the integration can see."""
    payload: Dict[str, Any] = {"page_size": min(limit, MAX_PAGE_SIZE)}
    if query:
        payload["query"] = query
    if object_type:
        # Post-2025-09-03 this is "page" | "data_source"; "database" is gone.
        payload["filter"] = {"property": "object", "value": object_type}
    return notion_auth.request("POST", "/search", json=payload).get("results", [])


def data_sources_for_database(database_id: str) -> List[Dict[str, Any]]:
    """List the data sources inside a database.

    Databases gained multiple data sources in 2025-09-03, and queries address
    the data source rather than the database.
    """
    payload = notion_auth.request("GET", "/databases/{0}".format(database_id))
    return payload.get("data_sources", []) or []


def query_data_source(
    data_source_id: str,
    page_size: int = DEFAULT_PAGE_SIZE,
    filter_payload: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Query rows from a data source."""
    body: Dict[str, Any] = {"page_size": min(page_size, MAX_PAGE_SIZE)}
    if filter_payload:
        body["filter"] = filter_payload
    # PATCH, not POST -- this endpoint moved with the data-source model.
    payload = notion_auth.request(
        "PATCH", "/data_sources/{0}/query".format(data_source_id), json=body
    )
    return payload.get("results", [])


def page_blocks(page_id: str, limit: int = MAX_BLOCKS) -> List[Dict[str, Any]]:
    payload = notion_auth.request(
        "GET", "/blocks/{0}/children?page_size={1}".format(page_id, min(limit, MAX_BLOCKS))
    )
    return payload.get("results", [])


def _index_page(page: Dict[str, Any], body: str) -> None:
    title = page_title(page)
    content = "Notion page: {0}\n{1}".format(title, body[:4000])
    vector = None
    try:
        vector = embeddings.embed(content)
    except Exception:
        pass
    store.store_embedding(
        kind="notion",
        ref_id=page.get("id", ""),
        content=content,
        vector=vector,
        metadata={"title": title, "url": page.get("url", "")},
    )


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@tool(
    name="notion_search",
    description=(
        "Search Notion for pages and databases the integration can see. Notion "
        "integrations only see what has been explicitly shared with them. Use "
        "this first to find ids for the other Notion tools."
    ),
    input_schema=obj(
        {
            "query": {"type": "string", "description": "Title text to search for."},
            "type": {
                "type": "string",
                "enum": ["page", "data_source"],
                "description": "Restrict to pages or to databases (data sources).",
            },
            "limit": {"type": "integer", "description": "Max results, default 25."},
        }
    ),
    requires="notion",
)
def notion_search(args: Dict[str, Any]) -> str:
    try:
        results = search(
            query=args.get("query", ""),
            object_type=args.get("type"),
            limit=int(args.get("limit", DEFAULT_PAGE_SIZE)),
        )
    except (notion_auth.NotionError, notion_auth.NotConnected) as exc:
        return str(exc)

    if not results:
        return (
            "Nothing found. Remember the integration only sees pages shared "
            "with it -- in Notion, open the page, ... menu > Connections > add "
            "your integration."
        )

    lines = []
    for item in results:
        kind = item.get("object", "?")
        lines.append(
            "- [{0}] {1} | id={2}".format(kind, page_title(item), item.get("id", ""))
        )
    return "\n".join(lines)


@tool(
    name="notion_read_page",
    description=(
        "Read a Notion page: its properties and its body content. The page is "
        "added to the twin's recall index, so you can find it later with recall."
    ),
    input_schema=obj({"page_id": {"type": "string"}}, ["page_id"]),
    requires="notion",
)
def notion_read_page(args: Dict[str, Any]) -> str:
    try:
        page = notion_auth.request("GET", "/pages/{0}".format(args["page_id"]))
        blocks = page_blocks(args["page_id"])
    except (notion_auth.NotionError, notion_auth.NotConnected) as exc:
        return str(exc)

    body = blocks_to_text(blocks)
    _index_page(page, body)

    properties = page.get("properties") or {}
    rendered_props = []
    for name, prop in properties.items():
        value = plain_value(prop)
        if value:
            rendered_props.append("{0}: {1}".format(name, value))

    return "# {0}\n\n{1}\n\n{2}".format(
        page_title(page),
        "\n".join(rendered_props) or "(no properties)",
        body or "(empty page)",
    )


@tool(
    name="notion_query_database",
    description=(
        "List rows from a Notion database. Pass the data source id from "
        "notion_search (databases contain one or more data sources, and the "
        "query addresses the data source). If you have a database id instead, "
        "this resolves it to its first data source automatically."
    ),
    input_schema=obj(
        {
            "data_source_id": {"type": "string", "description": "Data source or database id."},
            "limit": {"type": "integer", "description": "Max rows, default 25."},
        },
        ["data_source_id"],
    ),
    requires="notion",
)
def notion_query_database(args: Dict[str, Any]) -> str:
    try:
        rows = _query_resolving_database(
            args["data_source_id"], int(args.get("limit", DEFAULT_PAGE_SIZE))
        )
    except (notion_auth.NotionError, notion_auth.NotConnected) as exc:
        return str(exc)

    if not rows:
        return "That database has no rows the integration can see."

    lines = []
    for row in rows:
        summary = property_named(row, DESCRIPTION_KEYS)
        lines.append(
            "- {0}{1} | id={2}".format(
                page_title(row),
                " -- {0}".format(summary[:120]) if summary else "",
                row.get("id", ""),
            )
        )
    return "\n".join(lines)


def _query_resolving_database(identifier: str, limit: int) -> List[Dict[str, Any]]:
    """Query by data source id, falling back to resolving a database id.

    The agent will sometimes hand over a database id, since that's what most
    Notion URLs contain. Resolving it is cheaper than making that a user error.
    """
    try:
        return query_data_source(identifier, page_size=limit)
    except notion_auth.NotionError:
        sources = data_sources_for_database(identifier)
        if not sources:
            raise
        return query_data_source(sources[0]["id"], page_size=limit)


@tool(
    name="notion_sync_projects",
    description=(
        "Import a Notion database of projects into the twin's project registry, "
        "so staleness tracking works against real project state. Each row "
        "becomes a tracked project, keeping Notion's own last-edited time and "
        "mapping its status. Re-running updates rather than duplicating. Use "
        "when the user says their projects live in Notion."
    ),
    input_schema=obj(
        {
            "data_source_id": {"type": "string", "description": "Data source or database id."},
            "limit": {"type": "integer", "description": "Max rows, default 100."},
        },
        ["data_source_id"],
    ),
    requires="notion",
)
def notion_sync_projects(args: Dict[str, Any]) -> str:
    try:
        rows = _query_resolving_database(
            args["data_source_id"], int(args.get("limit", MAX_PAGE_SIZE))
        )
    except (notion_auth.NotionError, notion_auth.NotConnected) as exc:
        return str(exc)

    synced = sync_projects(rows)
    if not synced:
        return "No rows with titles to import."

    return "Imported {0} project(s) from Notion:\n{1}".format(
        len(synced),
        "\n".join(
            "- {0} [{1}] last edited {2}".format(
                item["name"],
                item["status"],
                item["last_activity_at"].strftime("%Y-%m-%d"),
            )
            for item in synced
        ),
    )


def sync_projects(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Map Notion rows into the project registry.

    Notion's `last_edited_time` is carried across deliberately. Stamping these
    as active now would make every imported project look fresh and silence the
    staleness nudge that motivated the import.
    """
    synced = []
    for row in rows:
        name = page_title(row)
        if not name or name == "(untitled)":
            continue
        project = store.upsert_project(
            name=name,
            description=property_named(row, DESCRIPTION_KEYS),
            status=project_status(row),
            source="notion",
            last_activity_at=last_edited(row),
        )
        synced.append(project)
    return synced


@tool(
    name="notion_create_page",
    description=(
        "Create a page in Notion, either inside a database (pass "
        "parent_data_source_id) or under a page (pass parent_page_id). This "
        "writes to the user's workspace -- they must confirm before it runs."
    ),
    input_schema=obj(
        {
            "title": {"type": "string"},
            "content": {"type": "string", "description": "Body text, one paragraph per line."},
            "parent_data_source_id": {"type": "string"},
            "parent_page_id": {"type": "string"},
        },
        ["title"],
    ),
    write=True,
    requires="notion",
)
def notion_create_page(args: Dict[str, Any]) -> str:
    data_source_id = args.get("parent_data_source_id")
    page_id = args.get("parent_page_id")

    if data_source_id:
        # Parent type changed from database_id to data_source_id in 2025-09-03.
        parent = {"type": "data_source_id", "data_source_id": data_source_id}
    elif page_id:
        parent = {"type": "page_id", "page_id": page_id}
    else:
        return "Give either parent_data_source_id or parent_page_id."

    body: Dict[str, Any] = {
        "parent": parent,
        "properties": {
            "title": {"title": [{"type": "text", "text": {"content": args["title"]}}]}
        },
    }

    content = (args.get("content") or "").strip()
    if content:
        body["children"] = [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": line}}]
                },
            }
            for line in content.split("\n")
            if line.strip()
        ]

    try:
        created = notion_auth.request("POST", "/pages", json=body)
    except (notion_auth.NotionError, notion_auth.NotConnected) as exc:
        return str(exc)

    return "Created Notion page '{0}': {1}".format(
        args["title"], created.get("url", created.get("id", ""))
    )
