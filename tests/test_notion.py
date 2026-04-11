import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

from aw_notion.blocks import FocusBlock
from aw_notion.config import NotionFieldsConfig
from aw_notion.notion import NotionTimeLogClient

DB_ID = "00000000-0000-0000-0000-000000000000"
TOKEN = "secret_test"
TZ = ZoneInfo("Europe/Moscow")


def make_block(url=None) -> FocusBlock:
    return FocusBlock(
        app="Code",
        title="activitywatch.py — aw_notion — VS Code",
        start_utc=datetime(2026, 4, 11, 7, 0, 0, tzinfo=UTC),
        end_utc=datetime(2026, 4, 11, 7, 23, 10, tzinfo=UTC),
        active_seconds=1390.0,
        url=url,
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


def test_create_entry_omits_note_property(httpx_mock):
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

    assert "Note" not in captured["body"]["properties"]


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
