import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

STATE_PATH = Path.home() / ".config" / "timetrack" / "state.json"


@dataclass
class State:
    last_sync: datetime | None = None
    notion_entries: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = STATE_PATH) -> "State":
        if not path.exists():
            return cls()
        with open(path) as f:
            data = json.load(f)
        last_sync = None
        if data.get("last_sync"):
            last_sync = datetime.fromisoformat(data["last_sync"])
        return cls(
            last_sync=last_sync,
            notion_entries=data.get("notion_entries", {}),
        )

    def save(self, path: Path = STATE_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "last_sync": self.last_sync.isoformat() if self.last_sync else None,
            "notion_entries": self.notion_entries,
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
