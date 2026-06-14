import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

from aw_notion.blocks import FocusBlock
from aw_notion.config import NotionFieldsConfig
from aw_notion.notion import NotionTimeLogClient

DB_ID = "00000000-0000-0000-0000-000000000000"
DS_ID = "11111111-1111-1111-1111-111111111111"
TOKEN = "secret_test"
TZ = ZoneInfo("Europe/Moscow")


def make_block(url=None, note=None) -> FocusBlock:
    return FocusBlock(
        app="Code",
        title="activitywatch.py — aw_notion — VS Code",
        start_utc=datetime(2026, 4, 11, 7, 0, 0, tzinfo=UTC),
        end_utc=datetime(2026, 4, 11, 7, 23, 10, tzinfo=UTC),
        active_seconds=1390.0,
        url=url,
        note=note,
    )


def test_create_entry_returns_page_id(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.notion.com/v1/pages",
        json={"id": "page-abc-123"},
        status_code=200,
    )
    client = NotionTimeLogClient(TOKEN, DB_ID)
    page_id = client.create_entry(make_block(), TZ)
    assert page_id == "page-abc-123"


def test_create_entry_payload_includes_required_fields(httpx_mock):
    captured = {}

    def capture_callback(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(json={"id": "page-xyz"}, status_code=200)

    httpx_mock.add_callback(
        callback=capture_callback,
        method="POST",
        url="https://api.notion.com/v1/pages",
    )
    client = NotionTimeLogClient(TOKEN, DB_ID)
    client.create_entry(make_block(), TZ)

    props = captured["body"]["properties"]
    assert props["Entry"]["title"][0]["text"]["content"] == "activitywatch.py — aw_notion — VS Code"
    assert props["Duration"]["number"] == 23
    assert props["App"]["select"]["name"] == "Code"
    assert props["Sorted"]["checkbox"] is False
    assert "Start" in props
    assert "End" in props


def test_create_entry_omits_note_when_field_not_configured(httpx_mock):
    """Default NotionFieldsConfig has note=None → Note property never written,
    even if the block carries a note value."""
    captured = {}

    def capture_callback(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(json={"id": "page-xyz"}, status_code=200)

    httpx_mock.add_callback(
        callback=capture_callback,
        method="POST",
        url="https://api.notion.com/v1/pages",
    )
    client = NotionTimeLogClient(TOKEN, DB_ID)
    client.create_entry(make_block(note="Conversation about cats"), TZ)

    assert "Note" not in captured["body"]["properties"]


def test_create_entry_writes_note_when_configured(httpx_mock):
    captured = {}

    def capture_callback(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(json={"id": "page-xyz"}, status_code=200)

    httpx_mock.add_callback(
        callback=capture_callback,
        method="POST",
        url="https://api.notion.com/v1/pages",
    )
    fields = NotionFieldsConfig(note="Note")
    client = NotionTimeLogClient(TOKEN, DB_ID, fields=fields)
    client.create_entry(make_block(note="Conversation about cats"), TZ)

    props = captured["body"]["properties"]
    assert props["Note"]["rich_text"][0]["text"]["content"] == "Conversation about cats"


def test_create_entry_omits_note_when_block_note_is_none(httpx_mock):
    """Note configured but block.note is None → Note property omitted."""
    captured = {}

    def capture_callback(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(json={"id": "page-xyz"}, status_code=200)

    httpx_mock.add_callback(
        callback=capture_callback,
        method="POST",
        url="https://api.notion.com/v1/pages",
    )
    fields = NotionFieldsConfig(note="Note")
    client = NotionTimeLogClient(TOKEN, DB_ID, fields=fields)
    client.create_entry(make_block(note=None), TZ)

    assert "Note" not in captured["body"]["properties"]


def test_create_entry_truncates_long_note(httpx_mock):
    captured = {}

    def capture_callback(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(json={"id": "page-xyz"}, status_code=200)

    httpx_mock.add_callback(
        callback=capture_callback,
        method="POST",
        url="https://api.notion.com/v1/pages",
    )
    fields = NotionFieldsConfig(note="Note")
    client = NotionTimeLogClient(TOKEN, DB_ID, fields=fields)
    client.create_entry(make_block(note="x" * 3000), TZ)

    assert len(captured["body"]["properties"]["Note"]["rich_text"][0]["text"]["content"]) == 2000


def test_create_entry_sets_url_property_when_present(httpx_mock):
    captured = {}

    def capture_callback(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(json={"id": "page-xyz"}, status_code=200)

    httpx_mock.add_callback(
        callback=capture_callback,
        method="POST",
        url="https://api.notion.com/v1/pages",
    )
    client = NotionTimeLogClient(TOKEN, DB_ID)
    client.create_entry(make_block(url="https://github.com/x/y"), TZ)

    assert captured["body"]["properties"]["URL"]["url"] == "https://github.com/x/y"


def test_create_entry_truncates_url_at_2000_chars(httpx_mock):
    """Notion's URL property has a 2000-char limit. OAuth redirects with
    nested state tokens frequently exceed this; we truncate rather than
    fail the whole batch."""
    captured = {}

    def capture_callback(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(json={"id": "page-xyz"}, status_code=200)

    httpx_mock.add_callback(
        callback=capture_callback,
        method="POST",
        url="https://api.notion.com/v1/pages",
    )
    long_url = "https://oauth.example/callback?state=" + "x" * 3000
    client = NotionTimeLogClient(TOKEN, DB_ID)
    client.create_entry(make_block(url=long_url), TZ)

    assert len(captured["body"]["properties"]["URL"]["url"]) == 2000


def test_create_entry_omits_url_property_when_absent(httpx_mock):
    captured = {}

    def capture_callback(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(json={"id": "page-xyz"}, status_code=200)

    httpx_mock.add_callback(
        callback=capture_callback,
        method="POST",
        url="https://api.notion.com/v1/pages",
    )
    client = NotionTimeLogClient(TOKEN, DB_ID)
    client.create_entry(make_block(url=None), TZ)

    assert "URL" not in captured["body"]["properties"]


def test_create_entry_uses_configured_field_names(httpx_mock):
    captured = {}

    def capture_callback(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(json={"id": "page-xyz"}, status_code=200)

    httpx_mock.add_callback(
        callback=capture_callback,
        method="POST",
        url="https://api.notion.com/v1/pages",
    )
    fields = NotionFieldsConfig(
        entry="Запись",
        start="Начало",
        end="Конец",
        duration_minutes="Время",
        app="Программа",
        url="Ссылка",
        sorted="Готово",
    )
    client = NotionTimeLogClient(TOKEN, DB_ID, fields=fields)
    client.create_entry(make_block(url="https://x.test"), TZ)

    props = captured["body"]["properties"]
    assert "Запись" in props
    assert "Начало" in props
    assert "Конец" in props
    assert props["Время"]["number"] == 23
    assert props["Программа"]["select"]["name"] == "Code"
    assert props["Ссылка"]["url"] == "https://x.test"
    assert props["Готово"]["checkbox"] is False
    # ensure no English defaults leaked
    assert "Entry" not in props
    assert "Duration" not in props


def test_create_entry_title_truncated_at_100_chars(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.notion.com/v1/pages",
        json={"id": "page-abc"},
        status_code=200,
    )
    long_title_block = FocusBlock(
        app="Code",
        title="x" * 150,
        start_utc=datetime(2026, 4, 11, 7, 0, 0, tzinfo=UTC),
        end_utc=datetime(2026, 4, 11, 7, 5, 0, tzinfo=UTC),
        active_seconds=200.0,
    )
    client = NotionTimeLogClient(TOKEN, DB_ID)
    client.create_entry(long_title_block, TZ)
    req_body = json.loads(httpx_mock.get_request().content)
    title = req_body["properties"]["Entry"]["title"][0]["text"]["content"]
    assert len(title) == 100


# =====================================================================
# fetch_existing_keys — the --since/backfill Notion-side dedup path.
# notion_client v3 (API 2025-09-03) queries data sources, not databases, so the
# client first resolves the DB's data source then POSTs to data_sources/query.
# This path was previously only mocked away in test_cli (FakeNotion); these
# tests pin the real query/pagination/parsing against the Notion HTTP shape.
# =====================================================================
def _mock_ds_lookup(httpx_mock):
    """databases.retrieve → exposes the single Time Log data source."""
    httpx_mock.add_response(
        method="GET",
        url=f"https://api.notion.com/v1/databases/{DB_ID}",
        json={"id": DB_ID, "data_sources": [{"id": DS_ID, "name": "Time Log"}]},
        status_code=200,
    )


def _page(app, start_iso):
    return {
        "id": f"pg-{app}-{start_iso}",
        "properties": {
            "App": {"select": {"name": app}},
            "Start": {"date": {"start": start_iso}},
        },
    }


def test_fetch_existing_keys_normalizes_to_utc_minute(httpx_mock):
    """Key is (lowercased app, UTC start truncated to minute). The UTC
    normalization is load-bearing: an entry stored with a non-UTC offset must
    produce the same key as the recomputed block, so a timezone-config change
    doesn't resurrect duplicates."""
    _mock_ds_lookup(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url=f"https://api.notion.com/v1/data_sources/{DS_ID}/query",
        json={
            "results": [
                _page("Comet", "2026-04-11T10:30:45.000+03:00"),  # Moscow → 07:30 UTC
                _page("Code", "2026-04-11T09:00:00.000+00:00"),
            ],
            "has_more": False,
            "next_cursor": None,
        },
        status_code=200,
    )
    client = NotionTimeLogClient(TOKEN, DB_ID)
    keys = client.fetch_existing_keys(datetime(2026, 4, 11, 0, 0, tzinfo=UTC))
    assert keys == {
        ("comet", "2026-04-11T07:30:00+00:00"),
        ("code", "2026-04-11T09:00:00+00:00"),
    }


def test_fetch_existing_keys_paginates(httpx_mock):
    """has_more=True drives a second query carrying the next_cursor."""
    _mock_ds_lookup(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url=f"https://api.notion.com/v1/data_sources/{DS_ID}/query",
        json={
            "results": [_page("Comet", "2026-04-11T08:00:00.000+00:00")],
            "has_more": True,
            "next_cursor": "CURSOR-2",
        },
        status_code=200,
    )
    httpx_mock.add_response(
        method="POST",
        url=f"https://api.notion.com/v1/data_sources/{DS_ID}/query",
        json={
            "results": [_page("Code", "2026-04-11T09:00:00.000+00:00")],
            "has_more": False,
            "next_cursor": None,
        },
        status_code=200,
    )
    client = NotionTimeLogClient(TOKEN, DB_ID)
    keys = client.fetch_existing_keys(datetime(2026, 4, 11, 0, 0, tzinfo=UTC))
    assert ("comet", "2026-04-11T08:00:00+00:00") in keys
    assert ("code", "2026-04-11T09:00:00+00:00") in keys
    posts = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert len(posts) == 2
    assert json.loads(posts[1].content)["start_cursor"] == "CURSOR-2"


def test_fetch_existing_keys_filter_uses_start_field_and_on_or_after(httpx_mock):
    """The query filters on the configured Start field, on_or_after the window
    start in UTC ISO."""
    _mock_ds_lookup(httpx_mock)
    captured = {}

    def cb(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(json={"results": [], "has_more": False}, status_code=200)

    httpx_mock.add_callback(
        callback=cb,
        method="POST",
        url=f"https://api.notion.com/v1/data_sources/{DS_ID}/query",
    )
    client = NotionTimeLogClient(TOKEN, DB_ID)
    client.fetch_existing_keys(datetime(2026, 4, 11, 5, 0, tzinfo=UTC))
    f = captured["body"]["filter"]
    assert f["property"] == "Start"
    assert f["date"]["on_or_after"] == "2026-04-11T05:00:00+00:00"


def test_fetch_existing_keys_skips_entries_without_start(httpx_mock):
    """A row whose Start date is null is skipped, not crashed on."""
    _mock_ds_lookup(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url=f"https://api.notion.com/v1/data_sources/{DS_ID}/query",
        json={
            "results": [
                {"id": "no-start", "properties": {
                    "App": {"select": {"name": "Code"}}, "Start": {"date": None}}},
                _page("Comet", "2026-04-11T08:00:00.000+00:00"),
            ],
            "has_more": False,
        },
        status_code=200,
    )
    client = NotionTimeLogClient(TOKEN, DB_ID)
    keys = client.fetch_existing_keys(datetime(2026, 4, 11, 0, 0, tzinfo=UTC))
    assert keys == {("comet", "2026-04-11T08:00:00+00:00")}


def test_fetch_existing_keys_uses_data_source_query_not_database(httpx_mock):
    """Regression guard: notion_client v3 dropped databases.query. Resolving the
    data source and hitting data_sources/{id}/query is what keeps this working —
    if it reverts to databases/{id}/query, Notion returns 400 and this fails."""
    _mock_ds_lookup(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url=f"https://api.notion.com/v1/data_sources/{DS_ID}/query",
        json={"results": [], "has_more": False},
        status_code=200,
    )
    client = NotionTimeLogClient(TOKEN, DB_ID)
    client.fetch_existing_keys(datetime(2026, 4, 11, 0, 0, tzinfo=UTC))
    urls = [str(r.url) for r in httpx_mock.get_requests()]
    assert any(f"/data_sources/{DS_ID}/query" in u for u in urls)
    assert not any("/databases/" in u and "/query" in u for u in urls)
