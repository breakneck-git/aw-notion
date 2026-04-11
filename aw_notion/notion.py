from zoneinfo import ZoneInfo

from notion_client import Client

from .blocks import FocusBlock
from .config import NotionFieldsConfig


class NotionTimeLogClient:
    def __init__(
        self,
        token: str,
        db_id: str,
        fields: NotionFieldsConfig | None = None,
    ):
        self.client = Client(auth=token)
        self.db_id = db_id
        self.fields = fields or NotionFieldsConfig()

    def create_entry(self, block: FocusBlock, tz: ZoneInfo) -> str:
        title = block.title[:100]
        f = self.fields

        properties: dict = {
            f.entry: {"title": [{"text": {"content": title}}]},
            f.start: {"date": {"start": block.start_local(tz).isoformat()}},
            f.end: {"date": {"start": block.end_local(tz).isoformat()}},
            f.duration_minutes: {"number": block.active_minutes()},
            f.app: {"select": {"name": block.app}},
            f.sorted: {"checkbox": False},
        }
        if block.url:
            properties[f.url] = {"url": block.url}

        response = self.client.pages.create(
            parent={"database_id": self.db_id},
            properties=properties,
        )
        return response["id"]
