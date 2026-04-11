import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_PATH = Path.home() / ".config" / "aw-notion" / "state.json"

PRUNE_WINDOW = timedelta(days=1)


@dataclass
class State:
    last_sync: datetime | None = None
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

        raw_entries = data.get("notion_entries", {})
        entries: dict[str, dict] = {}
        for sig, val in raw_entries.items():
            if isinstance(val, str):
                entries[sig] = {"page_id": val, "created_at": None}
            else:
                entries[sig] = val

        return cls(last_sync=last_sync, notion_entries=entries)

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
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                kept[sig] = val
        return kept

    def save(self, path: Path = STATE_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.notion_entries = self._pruned_entries()
        data = {
            "last_sync": self.last_sync.isoformat() if self.last_sync else None,
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
