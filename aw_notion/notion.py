from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from notion_client import Client

from .blocks import FocusBlock
from .config import NotionFieldsConfig


def block_dedup_key(app: str, start_utc: datetime) -> tuple[str, str]:
    """Cross-run idempotency key for a Time Log entry: (lowercased app, UTC
    start truncated to the minute). UTC — not local — so the key survives a
    config `timezone` change: a past entry was written with its then-current
    offset, but the underlying instant is invariant. Used to dedup `--since`
    backfills against Notion's actual contents (signature-based state dedup
    only covers a short prune window)."""
    minute = start_utc.astimezone(UTC).replace(second=0, microsecond=0)
    return ((app or "").lower(), minute.isoformat())


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
        self._ds_id: str | None = None

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
            # Notion URL property is capped at 2000 chars; OAuth/redirect URLs
            # frequently exceed this (state + nested redirect tokens). Truncate
            # rather than fail the whole batch.
            properties[f.url] = {"url": block.url[:2000]}
        if f.note and block.note:
            properties[f.note] = {"rich_text": [{"text": {"content": block.note[:2000]}}]}

        response = self.client.pages.create(
            parent={"database_id": self.db_id},
            properties=properties,
        )
        return response["id"]

    def _timelog_data_source_id(self) -> str:
        """Resolve (and cache) the Time Log data source under the configured
        database. The Notion 2025-09-03 API queries data sources, not databases;
        a single-source DB (the Time Log) has exactly one."""
        if self._ds_id is None:
            db = self.client.databases.retrieve(database_id=self.db_id)
            sources = db.get("data_sources") or []
            if not sources:
                raise RuntimeError(f"database {self.db_id} has no data sources to query")
            self._ds_id = sources[0]["id"]
        return self._ds_id

    def fetch_existing_keys(self, start_utc: datetime) -> set[tuple[str, str]]:
        """Return {block_dedup_key(app, start)} for every Time Log entry whose
        Start is on/after `start_utc`. Lets `--since`/initial backfills skip
        entries that already exist in Notion even when their signatures have
        been pruned from local state. Read-only; paginates the full window."""
        f = self.fields
        keys: set[tuple[str, str]] = set()
        ds_id = self._timelog_data_source_id()
        cursor: str | None = None
        while True:
            kwargs: dict = {
                "filter": {
                    "property": f.start,
                    "date": {"on_or_after": start_utc.astimezone(UTC).isoformat()},
                },
                "page_size": 100,
            }
            if cursor:
                kwargs["start_cursor"] = cursor
            resp = self.client.data_sources.query(ds_id, **kwargs)
            for page in resp.get("results", []):
                props = page.get("properties", {})
                app = (((props.get(f.app) or {}).get("select")) or {}).get("name") or ""
                start_raw = (((props.get(f.start) or {}).get("date")) or {}).get("start")
                if not start_raw:
                    continue
                dt = datetime.fromisoformat(start_raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                keys.add(block_dedup_key(app, dt))
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
        return keys
