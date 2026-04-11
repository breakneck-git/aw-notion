from zoneinfo import ZoneInfo
from notion_client import Client

from .blocks import FocusBlock


class NotionTimeLogClient:
    def __init__(self, token: str, db_id: str):
        self.client = Client(auth=token)
        self.db_id = db_id

    def create_entry(self, block: FocusBlock, tz: ZoneInfo) -> str:
        title = block.title[:100]

        note_parts = [f"App: {block.app}"]
        if block.url:
            note_parts.append(f"URL: {block.url}")
        note = "\n".join(note_parts)

        response = self.client.pages.create(
            parent={"database_id": self.db_id},
            properties={
                "Entry": {
                    "title": [{"text": {"content": title}}]
                },
                "Start": {
                    "date": {"start": block.start_local(tz).isoformat()}
                },
                "End": {
                    "date": {"start": block.end_local(tz).isoformat()}
                },
                "Время": {
                    "rich_text": [{"text": {"content": f"{block.active_minutes()}м"}}]
                },
                "Note": {
                    "rich_text": [{"text": {"content": note}}]
                },
                "Sorted": {"checkbox": False},
            },
        )
        return response["id"]
