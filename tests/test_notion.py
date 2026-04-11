from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json
import httpx

from timetrack.blocks import FocusBlock
from timetrack.notion import NotionTimeLogClient

DB_ID = "35b4cfe8-1f3a-457a-80a8-fe61aa465a18"
TOKEN = "secret_test"
TZ = ZoneInfo("Europe/Moscow")

def make_block(url=None) -> FocusBlock:
    return FocusBlock(
        app="Code",
        title="activitywatch.py — timetrack — VS Code",
        start_utc=datetime(2026, 4, 11, 7, 0, 0, tzinfo=timezone.utc),
        end_utc=datetime(2026, 4, 11, 7, 23, 10, tzinfo=timezone.utc),
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
    assert props["Entry"]["title"][0]["text"]["content"] == "activitywatch.py — timetrack — VS Code"
    assert "23м" in props["Время"]["rich_text"][0]["text"]["content"]
    assert props["Sorted"]["checkbox"] is False
    assert "Start" in props
    assert "End" in props

def test_create_entry_note_includes_url_when_present(httpx_mock):
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

    note = captured["body"]["properties"]["Note"]["rich_text"][0]["text"]["content"]
    assert "https://github.com/x/y" in note

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
        start_utc=datetime(2026, 4, 11, 7, 0, 0, tzinfo=timezone.utc),
        end_utc=datetime(2026, 4, 11, 7, 5, 0, tzinfo=timezone.utc),
        active_seconds=200.0,
    )
    client = NotionTimeLogClient(TOKEN, DB_ID)
    client.create_entry(long_title_block, TZ)
    req_body = json.loads(httpx_mock.get_request().content)
    title = req_body["properties"]["Entry"]["title"][0]["text"]["content"]
    assert len(title) == 100
