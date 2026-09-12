"""Notion adapter: property flattening, status mapping, project sync.

Notion's property format is deeply nested and differs per type, so each type is
checked individually. The sync tests guard the detail that actually matters:
imported projects must keep Notion's own last-edited time, or the staleness
nudge that motivated the import is silently destroyed.

A few tests pin the post-2025-09-03 data-source API shapes, so a regression to
the old `database` endpoints fails loudly rather than at runtime.
"""

import datetime as dt

from twin.tools import notion_auth, notion_tools as nt


def rich(text):
    return [{"plain_text": text}]


def prop(kind, value):
    return {"type": kind, kind: value}


def page(properties=None, **extra):
    payload = {"id": "p1", "properties": properties or {}}
    payload.update(extra)
    return payload


# --- rich text ------------------------------------------------------------

def test_rich_text_joins_spans():
    assert nt.rich_text_to_plain([{"plain_text": "Hello "}, {"plain_text": "world"}]) == "Hello world"


def test_rich_text_handles_empty_and_garbage():
    assert nt.rich_text_to_plain([]) == ""
    assert nt.rich_text_to_plain(None) == ""
    assert nt.rich_text_to_plain("not a list") == ""


# --- plain_value per property type ---------------------------------------

def test_plain_value_title_and_rich_text():
    assert nt.plain_value(prop("title", rich("My page"))) == "My page"
    assert nt.plain_value(prop("rich_text", rich("some notes"))) == "some notes"


def test_plain_value_number_including_zero():
    """Zero is a real value, not an absent one."""
    assert nt.plain_value(prop("number", 0)) == "0"
    assert nt.plain_value(prop("number", 42)) == "42"
    assert nt.plain_value(prop("number", None)) == ""


def test_plain_value_select_and_status():
    assert nt.plain_value(prop("select", {"name": "High"})) == "High"
    assert nt.plain_value(prop("status", {"name": "In progress"})) == "In progress"


def test_plain_value_multi_select():
    assert nt.plain_value(prop("multi_select", [{"name": "a"}, {"name": "b"}])) == "a, b"


def test_plain_value_date_with_and_without_end():
    assert nt.plain_value(prop("date", {"start": "2026-10-01"})) == "2026-10-01"
    assert "2026-10-05" in nt.plain_value(
        prop("date", {"start": "2026-10-01", "end": "2026-10-05"})
    )


def test_plain_value_checkbox():
    assert nt.plain_value(prop("checkbox", True)) == "yes"
    assert nt.plain_value(prop("checkbox", False)) == "no"


def test_plain_value_people_and_files():
    assert nt.plain_value(prop("people", [{"name": "Sarah"}])) == "Sarah"
    assert nt.plain_value(prop("files", [{"name": "spec.pdf"}])) == "spec.pdf"


def test_plain_value_relation_reports_count():
    assert nt.plain_value(prop("relation", [{"id": "a"}, {"id": "b"}])) == "2 linked"
    assert nt.plain_value(prop("relation", [])) == ""


def test_plain_value_unique_id_with_and_without_prefix():
    assert nt.plain_value(prop("unique_id", {"prefix": "TASK", "number": 7})) == "TASK-7"
    assert nt.plain_value(prop("unique_id", {"prefix": None, "number": 7})) == "7"


def test_plain_value_formula_unwraps_inner_type():
    """A formula wraps its own typed value, so it has to recurse."""
    assert nt.plain_value(prop("formula", {"type": "number", "number": 5})) == "5"
    assert nt.plain_value(prop("formula", {"type": "string", "string": "x"})) == ""


def test_plain_value_rollup_array():
    rollup = prop("rollup", {"type": "array", "array": [prop("number", 1), prop("number", 2)]})
    assert nt.plain_value(rollup) == "1, 2"


def test_plain_value_url_email_phone():
    assert nt.plain_value(prop("url", "https://x.test")) == "https://x.test"
    assert nt.plain_value(prop("email", "a@b.test")) == "a@b.test"


def test_plain_value_unknown_type_is_empty_not_an_error():
    assert nt.plain_value(prop("some_future_type", {"x": 1})) == ""
    assert nt.plain_value(None) == ""


# --- titles ---------------------------------------------------------------

def test_page_title_found_regardless_of_property_name():
    """The title property can be called anything."""
    assert nt.page_title(page({"Project name": prop("title", rich("Guitar"))})) == "Guitar"


def test_page_title_falls_back_to_top_level_for_data_sources():
    assert nt.page_title({"title": rich("Tasks DB")}) == "Tasks DB"


def test_page_title_defaults_to_untitled():
    assert nt.page_title(page()) == "(untitled)"


# --- property lookup ------------------------------------------------------

def test_property_named_is_case_insensitive():
    found = nt.property_named(page({"Description": prop("rich_text", rich("hi"))}), ["description"])
    assert found == "hi"


def test_property_named_takes_first_non_empty_in_order():
    properties = {
        "Description": prop("rich_text", rich("")),
        "Notes": prop("rich_text", rich("real notes")),
    }
    assert nt.property_named(page(properties), ["description", "notes"]) == "real notes"


def test_property_named_missing_returns_empty():
    assert nt.property_named(page(), ["description"]) == ""


# --- status mapping -------------------------------------------------------

def test_status_maps_done_variants():
    for value in ("Done", "Completed", "Shipped", "Archived"):
        assert nt.project_status(page({"S": prop("status", {"name": value})})) == "done"


def test_status_maps_paused_variants():
    for value in ("Paused", "On Hold", "Blocked", "Backlog", "Not started"):
        assert nt.project_status(page({"S": prop("status", {"name": value})})) == "paused"


def test_status_maps_abandoned_variants():
    for value in ("Cancelled", "Abandoned", "Won't do"):
        assert nt.project_status(page({"S": prop("status", {"name": value})})) == "abandoned"


def test_status_mapping_is_case_insensitive():
    assert nt.project_status(page({"S": prop("status", {"name": "DONE"})})) == "done"


def test_unrecognised_status_defaults_to_active():
    """Unknown means keep it visible to the staleness check."""
    assert nt.project_status(page({"S": prop("status", {"name": "Marinating"})})) == "active"


def test_missing_status_defaults_to_active():
    assert nt.project_status(page()) == "active"


def test_status_also_reads_select_properties():
    assert nt.project_status(page({"Stage": prop("select", {"name": "Done"})})) == "done"


# --- timestamps -----------------------------------------------------------

def test_last_edited_parses_notion_timestamp():
    parsed = nt.last_edited({"last_edited_time": "2026-08-20T10:00:00.000Z"})
    assert parsed.year == 2026 and parsed.tzinfo is not None


def test_last_edited_missing_or_bad_returns_none():
    assert nt.last_edited({}) is None
    assert nt.last_edited({"last_edited_time": "whenever"}) is None


# --- blocks ---------------------------------------------------------------

def block(kind, **body):
    return {"type": kind, kind: body}


def test_block_paragraph_and_headings():
    assert nt.block_to_text(block("paragraph", rich_text=rich("hello"))) == "hello"
    assert nt.block_to_text(block("heading_1", rich_text=rich("Title"))) == "# Title"
    assert nt.block_to_text(block("heading_3", rich_text=rich("Sub"))) == "### Sub"


def test_block_list_items():
    assert nt.block_to_text(block("bulleted_list_item", rich_text=rich("a"))) == "- a"
    assert nt.block_to_text(block("numbered_list_item", rich_text=rich("b"))) == "1. b"


def test_block_todo_reflects_checked_state():
    assert nt.block_to_text(block("to_do", rich_text=rich("task"), checked=True)) == "- [x] task"
    assert nt.block_to_text(block("to_do", rich_text=rich("task"), checked=False)) == "- [ ] task"


def test_block_code_keeps_language():
    rendered = nt.block_to_text(block("code", rich_text=rich("print(1)"), language="python"))
    assert "```python" in rendered and "print(1)" in rendered


def test_block_divider_and_child_page():
    assert nt.block_to_text({"type": "divider", "divider": {}}) == "---"
    assert "Sub page" in nt.block_to_text(block("child_page", title="Sub page"))


def test_block_unknown_type_does_not_crash():
    assert nt.block_to_text({"type": "future_block", "future_block": {}}) == ""
    assert nt.block_to_text("not a dict") == ""


def test_blocks_to_text_skips_empty_lines():
    blocks = [
        block("paragraph", rich_text=rich("one")),
        block("paragraph", rich_text=rich("")),
        block("paragraph", rich_text=rich("two")),
    ]
    assert nt.blocks_to_text(blocks) == "one\ntwo"


# --- error translation ----------------------------------------------------

def test_unauthorized_points_at_onboarding():
    assert "onboarding" in notion_auth.describe_error(401, {"code": "unauthorized"})


def test_not_found_explains_sharing():
    """The single most common Notion failure: nothing is shared by default."""
    described = notion_auth.describe_error(404, {"code": "object_not_found"})
    assert "Connections" in described and "integration" in described


def test_rate_limited_is_translated():
    assert "rate-limit" in notion_auth.describe_error(429, {"code": "rate_limited"})


def test_validation_error_includes_notions_message():
    described = notion_auth.describe_error(400, {"code": "validation_error", "message": "bad id"})
    assert "bad id" in described


# --- project sync ---------------------------------------------------------

def db_row(title, status="In progress", edited="2026-08-20T10:00:00.000Z", notes=""):
    properties = {
        "Name": prop("title", rich(title)),
        "Status": prop("status", {"name": status}),
    }
    if notes:
        properties["Notes"] = prop("rich_text", rich(notes))
    return {"id": "row", "properties": properties, "last_edited_time": edited}


def capture_upserts(monkeypatch):
    captured = []

    def fake_upsert(**kwargs):
        captured.append(kwargs)
        return {
            "name": kwargs["name"],
            "status": kwargs["status"],
            "last_activity_at": kwargs["last_activity_at"] or dt.datetime.now(dt.timezone.utc),
        }

    monkeypatch.setattr(nt.store, "upsert_project", fake_upsert)
    return captured


def test_sync_preserves_notions_last_edited_time(monkeypatch):
    """Stamping imports as active-now would silence the staleness nudge."""
    captured = capture_upserts(monkeypatch)
    nt.sync_projects([db_row("Guitar", edited="2026-07-01T09:00:00.000Z")])

    assert captured[0]["last_activity_at"].date() == dt.date(2026, 7, 1)


def test_sync_maps_status_and_source(monkeypatch):
    captured = capture_upserts(monkeypatch)
    nt.sync_projects([db_row("Guitar", status="On Hold")])

    assert captured[0]["status"] == "paused"
    assert captured[0]["source"] == "notion"


def test_sync_carries_description_across(monkeypatch):
    captured = capture_upserts(monkeypatch)
    nt.sync_projects([db_row("Guitar", notes="learn fingerstyle")])
    assert captured[0]["description"] == "learn fingerstyle"


def test_sync_skips_untitled_rows(monkeypatch):
    """An empty row in a Notion database shouldn't become a phantom project."""
    captured = capture_upserts(monkeypatch)
    synced = nt.sync_projects([{"id": "x", "properties": {}}])

    assert captured == [] and synced == []


def test_sync_handles_missing_timestamp(monkeypatch):
    captured = capture_upserts(monkeypatch)
    row = db_row("Guitar")
    del row["last_edited_time"]
    nt.sync_projects([row])

    # None means the store falls back to now(), which is the right default.
    assert captured[0]["last_activity_at"] is None


# --- data-source API shapes ----------------------------------------------

def test_search_filters_on_data_source_not_database(monkeypatch):
    """Post-2025-09-03 the filter value is 'data_source'; 'database' is gone."""
    sent = {}

    def fake_request(method, path, token=None, json=None):
        sent.update({"method": method, "path": path, "json": json})
        return {"results": []}

    monkeypatch.setattr(nt.notion_auth, "request", fake_request)
    nt.search(query="projects", object_type="data_source")

    assert sent["path"] == "/search"
    assert sent["json"]["filter"]["value"] == "data_source"


def test_query_uses_patch_on_the_data_source_endpoint(monkeypatch):
    """The query endpoint moved off /databases and changed verb."""
    sent = {}

    def fake_request(method, path, token=None, json=None):
        sent.update({"method": method, "path": path})
        return {"results": []}

    monkeypatch.setattr(nt.notion_auth, "request", fake_request)
    nt.query_data_source("ds123")

    assert sent["method"] == "PATCH"
    assert sent["path"] == "/data_sources/ds123/query"


def test_query_falls_back_to_resolving_a_database_id(monkeypatch):
    """Notion URLs contain database ids, so accept one rather than erroring."""
    calls = []

    def fake_query(data_source_id, page_size=25, filter_payload=None):
        calls.append(data_source_id)
        if data_source_id == "db123":
            raise notion_auth.NotionError("not a data source")
        return [{"id": "row"}]

    monkeypatch.setattr(nt, "query_data_source", fake_query)
    monkeypatch.setattr(nt, "data_sources_for_database", lambda db: [{"id": "ds456"}])

    rows = nt._query_resolving_database("db123", 25)

    assert calls == ["db123", "ds456"]
    assert rows == [{"id": "row"}]


def test_query_reraises_when_database_has_no_data_sources(monkeypatch):
    def fake_query(data_source_id, page_size=25, filter_payload=None):
        raise notion_auth.NotionError("nope")

    monkeypatch.setattr(nt, "query_data_source", fake_query)
    monkeypatch.setattr(nt, "data_sources_for_database", lambda db: [])

    try:
        nt._query_resolving_database("bad", 25)
    except notion_auth.NotionError:
        return
    raise AssertionError("should have re-raised")


def test_create_page_uses_data_source_id_parent(monkeypatch):
    """Parent type changed from database_id to data_source_id."""
    sent = {}

    def fake_request(method, path, token=None, json=None):
        sent.update({"json": json})
        return {"url": "https://notion.test/p"}

    monkeypatch.setattr(nt.notion_auth, "request", fake_request)
    nt.notion_create_page({"title": "New", "parent_data_source_id": "ds1"})

    assert sent["json"]["parent"] == {"type": "data_source_id", "data_source_id": "ds1"}


def test_create_page_requires_a_parent():
    assert "parent" in nt.notion_create_page({"title": "New"}).lower()
