# aw-notion

Sync [ActivityWatch](https://activitywatch.net/) focus blocks to a Notion Time Log database. macOS and Linux.

## What it does

- Pulls window/AFK/web events from your local ActivityWatch instance every 15 minutes via launchd.
- Compresses them into "focus blocks" (AFK-aware merging, configurable thresholds).
- Writes each block as a row in your Notion Time Log database.
- Idempotent: signature-based dedup, atomic state writes, file-locked against concurrent runs.

## Requirements

- macOS (uses `launchd`) or Linux (uses `systemd --user` units)
- Python 3.11+
- [ActivityWatch](https://activitywatch.net/) running locally
- Notion integration token + a database with the schema below

## Notion database schema

Create a database in Notion with these properties (the names are the **defaults** — they're configurable in `config.toml`):

| Property | Type | Notes |
| --- | --- | --- |
| `Entry` | Title | Window/tab title (truncated to 100 chars) |
| `Start` | Date | Block start (local time) |
| `End` | Date | Block end (local time) |
| `Duration` | Number | Active minutes |
| `App` | Select | Application name |
| `URL` | URL | Optional, set for browser blocks |
| `Sorted` | Checkbox | Always written as `false` (manual workflow flag) |
| `Note` | Text | **Opt-in.** Context from aw-watcher-ax (Claude/Telegram chat titles) or git branch for terminal blocks. See [Note enrichment](#note-enrichment) below. |

Then share the database with your integration: in Notion go to **Connections → Add connections** on the database page.

## Install

```bash
git clone https://github.com/breakneck-git/aw-notion.git
cd aw-notion
./install.sh
```

The install script will:
- Detect a Python 3.11+ interpreter, create a `.venv`, and editable-install the package
- Seed `~/.config/aw-notion/config.toml` from `config.toml.example` if missing
- On **macOS**: install and load `~/Library/LaunchAgents/com.aw-notion.sync.plist` (15-min `launchd` interval), and supervise ActivityWatch's `aw-watcher-window` with a launchd `KeepAlive` agent — see [Keeping the window-watcher alive (macOS)](#keeping-the-window-watcher-alive-macos)
- On **Linux**: install `aw-notion.service` + `aw-notion.timer` into `~/.config/systemd/user/` and `systemctl --user enable --now aw-notion.timer` (15-min interval)

Then **edit `~/.config/aw-notion/config.toml`** and fill in:
- `notion.token` — your integration token from <https://www.notion.so/my-integrations>
- `notion.timelog_db` — your Notion database UUID (from the database URL)
- `timezone` — your IANA timezone

### Keeping the window-watcher alive (macOS)

aw-notion is only as good as the data ActivityWatch records. On macOS, `aw-qt` starts
`aw-watcher-window` **once at login and never restarts it** if it dies (sleep/wake,
crash). When the window-watcher is down, ActivityWatch silently records no window
events, so every `aw-notion sync` reports `Found 0 focus blocks` and nothing reaches
Notion — even though aw-notion itself is running fine.

To make this self-healing, the macOS installer:

- installs `~/Library/LaunchAgents/com.aw-watcher-window.keepalive.plist`, a launchd
  `KeepAlive` agent that runs `aw-watcher-window` and **auto-restarts it within ~30 s**
  of any death, and starts it at login (`RunAtLoad`);
- removes `aw-watcher-window` from aw-qt's `autostart_modules` (in
  `~/Library/Application Support/activitywatch/aw-qt/aw-qt.toml`, backed up as
  `aw-qt.toml.aw-notion.bak`) so the watcher has exactly one supervisor and never runs
  twice against the same bucket;
- restarts ActivityWatch once so the change takes effect.

Verify it: `launchctl list | grep aw-watcher-window` should show the agent, and
`curl -s localhost:5600/api/0/buckets/ | python3 -m json.tool` should show the
`aw-watcher-window_<host>` bucket's `last_updated` advancing.

## Usage

```bash
aw-notion sync                                # one manual run
aw-notion sync --dry-run                      # compute blocks, log, skip Notion writes
aw-notion sync --since 2026-04-01T00:00:00    # forced backfill from a specific UTC time
aw-notion sync --help
```

The scheduled agent runs `aw-notion sync` automatically every 15 minutes after install.

### Service controls

**macOS** (logs at `~/Library/Logs/aw-notion/sync.log`):

```bash
launchctl unload ~/Library/LaunchAgents/com.aw-notion.sync.plist
launchctl load ~/Library/LaunchAgents/com.aw-notion.sync.plist
launchctl kickstart -k gui/$(id -u)/com.aw-notion.sync   # force a sync
tail -f ~/Library/Logs/aw-notion/sync.log
```

**Linux** (logs captured by systemd journal):

```bash
systemctl --user disable --now aw-notion.timer
systemctl --user enable --now aw-notion.timer
systemctl --user start aw-notion.service                 # force a sync
journalctl --user -u aw-notion.service -f
```

## Configuration

`~/.config/aw-notion/config.toml`. See `config.toml.example` for the full annotated template.

If your Notion database uses different property names (e.g. localized labels), override them in `[notion.fields]`:

```toml
[notion.fields]
duration_minutes = "Время"
entry = "Запись"
```

Other tunables under `[activitywatch]`: `afk_threshold_min`, `merge_gap_sec`, `min_block_duration_sec`. Under `[sync]`: `initial_sync_days`.

## Note enrichment

The `Note` column captures context that isn't already in the window title. It's **opt-in** — leave `note` unset in `[notion.fields]` and the property is never written.

To enable: add a rich text column named `Note` (or any name) to your database, then set `note = "Note"` in `[notion.fields]`.

Two sources feed it:

1. **aw-watcher-ax** (macOS only) — a separate watcher that reads conversation/chat titles from Claude Desktop and Telegram via the Accessibility API and writes them to a `aw-watcher-ax_<host>` bucket. aw-notion reads from that bucket via timestamp overlap and attaches the context to matching focus blocks. See [aw-watcher-ax](https://github.com/breakneck-git/aw-watcher-ax) for install + config.
2. **Git reflog fallback** — for terminal blocks whose title looks like a path (e.g. `~/code/aw-notion`), aw-notion walks `.git/logs/HEAD` and derives `"aw-notion @ main"` from the branch active at the block's end time. Zero-install, works offline, no external watcher needed.

Sources are tried in order: ax-watcher first, git reflog as fallback. Blocks without a source (Slack, Discord, etc.) leave `Note` empty.

## How it works

```
ActivityWatch REST  →  compute_focus_blocks  →  NotionTimeLogClient
  (activitywatch.py)      (blocks.py)             (notion.py)
                              ↓                       ↓
                         AFK-aware merging       state.notion_entries
                         + block signature       (dedup by signature)
```

Each block is keyed by a SHA256 prefix of `app|title|start_utc`. The state file (`~/.config/aw-notion/state.json`) maps signatures → Notion page IDs and tracks `last_sync`. Each run starts from `last_sync - 30 min` to catch late events; entries older than one day are pruned on save to keep the file small.

Web-watcher events (when [aw-watcher-web](https://github.com/ActivityWatch/aw-watcher-web) is installed) override window-watcher events for browsers, so browser blocks get URLs attached.

## Development

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install pytest responses pytest-httpx
.venv/bin/pytest
```

Run a single test:

```bash
.venv/bin/pytest tests/test_blocks.py::test_afk_event_filters_window_events
```

No test hits a live ActivityWatch or Notion instance — HTTP is mocked end-to-end.

## License

MIT — see [LICENSE](LICENSE).
