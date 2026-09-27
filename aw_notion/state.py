import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

STATE_PATH = Path.home() / ".config" / "aw-notion" / "state.json"

PRUNE_WINDOW = timedelta(days=1)


@dataclass
class State:
    last_sync: datetime | None = None
    # Start of the earliest block still open (not yet written) at last_sync.
    # The next sync must fetch from before it, or the server clips the block
    # to the query start and it's never seen whole.
    pending_since: datetime | None = None
    # Set when a Notion write failed in a way that may still have created the
    # page (timeout, dropped connection, 5xx): the next sync must check Notion
    # before writing, or the retry duplicates it.
    verify_notion: bool = False
    notion_entries: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = STATE_PATH) -> "State":
        if not path.exists():
            return cls()
        with open(path) as f:
            data = json.load(f)
        last_sync = None
        if data.get("last_sync"):
            last_sync = datetime.fromisoformat(data["last_sync"])

        pending_since = None
        if data.get("pending_since"):
            pending_since = datetime.fromisoformat(data["pending_since"])

        raw_entries = data.get("notion_entries", {})
        entries: dict[str, dict] = {}
        for sig, val in raw_entries.items():
            if isinstance(val, str):
                entries[sig] = {"page_id": val, "created_at": None}
            else:
                entries[sig] = val

        return cls(
            last_sync=last_sync,
            pending_since=pending_since,
            verify_notion=bool(data.get("verify_notion", False)),
            notion_entries=entries,
        )

    def _pruned_entries(self) -> dict[str, dict]:
        if self.last_sync is None:
            return self.notion_entries
        cutoff = self.last_sync - PRUNE_WINDOW
        kept: dict[str, dict] = {}
        for sig, val in self.notion_entries.items():
            created_at = val.get("created_at")
            if created_at is None:
                continue
            ts = datetime.fromisoformat(created_at)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts >= cutoff:
                kept[sig] = val
        return kept

    def save(self, path: Path = STATE_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.notion_entries = self._pruned_entries()
        data = {
            "last_sync": self.last_sync.isoformat() if self.last_sync else None,
            "pending_since": self.pending_since.isoformat() if self.pending_since else None,
            "verify_notion": self.verify_notion,
            "notion_entries": self.notion_entries,
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
