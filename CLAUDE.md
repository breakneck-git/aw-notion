# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`aw-notion` is a macOS + Linux CLI that pulls window/AFK/web events from a locally running [ActivityWatch](https://activitywatch.net/) instance, compresses them into "focus blocks," and writes each block as a row to a Notion "Time Log" database. A `launchd` agent (macOS) or `systemd --user` timer (Linux) runs `aw-notion sync` every 15 minutes. Python package: `aw_notion`.

**Data-source reliability gotcha (macOS):** if `sync` logs `Found 0 focus blocks` every run while aw-notion is otherwise healthy (`last exit code = 0`), the cause is almost always **ActivityWatch's `aw-watcher-window` being dead**, not aw-notion. `aw-qt` launches that watcher once and does NOT restart it after a sleep/wake or crash, so window events silently stop. Check `pgrep -fl aw-watcher-window` and the `aw-watcher-window_<host>` bucket's `last_updated`. The installer mitigates this by supervising the watcher with a launchd `KeepAlive` agent (`com.aw-watcher-window.keepalive`) and removing it from aw-qt's `autostart_modules` — see README "Keeping the window-watcher alive (macOS)".

## Commands

```bash
# First-time install (detects python3.11+, creates .venv, seeds config, loads launchd OR systemd agent)
./install.sh

# Editable install into an existing venv
pip install -e .
pip install pytest responses pytest-httpx ruff  # dev deps from pyproject.toml [dependency-groups.dev]

# Run one sync manually
.venv/bin/aw-notion sync
.venv/bin/aw-notion sync --dry-run
.venv/bin/aw-notion sync --since 2026-04-01T00:00:00

# Tests + lint
.venv/bin/pytest                                              # full suite
.venv/bin/pytest tests/test_blocks.py                         # one file
.venv/bin/pytest tests/test_blocks.py::test_afk_event_filters_window_events  # one test
.venv/bin/ruff check aw_notion tests                          # lint
.venv/bin/ruff format aw_notion tests                         # format

# Scheduled agent controls — macOS (launchd)
launchctl unload ~/Library/LaunchAgents/com.aw-notion.sync.plist
launchctl load ~/Library/LaunchAgents/com.aw-notion.sync.plist
tail -f ~/Library/Logs/aw-notion/sync.log

# Scheduled agent controls — Linux (systemd --user)
systemctl --user disable --now aw-notion.timer
systemctl --user enable --now aw-notion.timer
journalctl --user -u aw-notion.service -f
```

Python 3.11+ is required (uses `tomllib` and `zoneinfo`).

## Architecture

Pipeline for `aw-notion sync` (see `aw_notion/cli.py`):

```
ActivityWatch REST  ->  compute_focus_blocks  ->  NotionTimeLogClient
  (activitywatch.py)       (blocks.py)              (notion.py)
                              |                         |
                              v                         v
                        AFK-aware grouping        State.notion_entries
                        + block signature         (dedupe by signature)
```

**Key data flows and invariants that span files:**

1. **Web watcher enriches window watcher with URLs; it does NOT replace it.** In `ActivityWatchClient.get_all_events` (`activitywatch.py`), window-watcher is the sole source of truth for `app`, `title`, and `duration`. Web-watcher events are read first into a sorted list of `(start, end, url)` intervals; then, for each window event, `_find_url_by_overlap` looks up the URL of any web event whose interval temporally overlaps `[window.start, window.end]`. **App-name filtering is load-bearing**: only window events whose app is in `browser_apps` get URL lookups — otherwise web-watcher's background tab heartbeats (which keep firing even when the browser is not in the foreground) would leak onto unrelated terminal/editor window events. This design intentionally has **no hard-coded browser-app-name map** — it works for any chromium-based browser (Comet, Arc, Brave, Vivaldi, Chrome Canary, …) via the configurable `browser_apps` list. Zero-duration web events (emitted on navigation) are treated as having a tiny epsilon span so they still match.

   **Note enrichment follows the exact same pattern.** Events from `aw-watcher-ax_*` buckets (produced by the separate [aw-watcher-ax](https://github.com/breakneck-git/aw-watcher-ax) macOS package) are read into a sorted list of `(start, end, app, context)` intervals. For each window event, `_find_note_by_overlap` looks up the context of any ax event whose interval temporally overlaps AND whose `app` field matches the window event's app. The app filter here serves the same leak-prevention role that `browser_apps` does for URLs. aw-notion has **zero knowledge** of ax-watcher beyond the bucket name prefix — they communicate only via the AW bucket namespace.

2. **AFK is filtered two ways in `compute_focus_blocks` (`blocks.py`).** First, any window event whose timestamp falls inside an `afk` interval is dropped entirely (soft filter). Second, when iterating sorted active events, a gap greater than `afk_threshold_sec` between the current block and the next event forces a hard block boundary. Both must stay consistent, and `afk_threshold_sec` is derived from config's `afk_threshold_min * 60` in `cli.py`.

3. **Block merging has three branches** in the loop in `compute_focus_blocks`: (a) gap > afk threshold → close + start new; (b) same app/title and gap ≤ `merge_gap_sec` → extend current; (c) otherwise → close + start new. Blocks shorter than `min_duration_sec` (active time, not wall time) are dropped on close. The final block is flushed after the loop.

4. **Idempotency is keyed on `FocusBlock.signature()`** — a 16-char SHA256 prefix of `app|title|start_utc.isoformat()`. `State.notion_entries` (loaded from/saved to `~/.config/aw-notion/state.json`) maps signature → `{page_id, created_at}`. The sync loop skips any signature already present. The state file also tracks `last_sync`, which the next run rewinds by 30 minutes to catch late-arriving events. Entries older than `last_sync - 1 day` are pruned on save.

5. **Incremental vs initial sync.** When `state.last_sync` is `None` (first run or missing state file), `cli.sync` fetches `cfg.sync.initial_sync_days` of history. Otherwise it fetches from `last_sync - 30 minutes`. The 30-minute overlap is safe because of signature dedup. `--since ISO8601` overrides this for forced backfill.

6. **Error handling intentionally fails fast.** If Notion page creation raises, `cli.sync` saves state (so successful entries up to that point are recorded) and calls `sys.exit(1)`. The scheduled agent (launchd on macOS, systemd timer on Linux) then retries on its next interval. Do not wrap this in broad `try/except` that swallows failures — the fail-fast behavior is load-bearing for debugging.

7. **Concurrency safety.** `cli.sync` acquires `~/.config/aw-notion/sync.lock` via `fcntl.flock(LOCK_EX | LOCK_NB)` before any Notion writes. A manual `aw-notion sync` running while the scheduled agent fires will exit cleanly with "another sync in progress, skipping" instead of duplicating Notion entries.

8. **Atomic state writes.** `State.save` writes to `state.json.tmp` then `os.replace`s. A SIGKILL mid-write cannot leave a half-written `state.json`.

9. **Note enrichment is feature-gated and fallback-layered.** `NotionFieldsConfig.note` defaults to `None`; when `None`, `NotionTimeLogClient.create_entry` never writes the Note property regardless of what's on the block. Sources are applied in priority order: (a) `AWEvent.note` is set from ax-watcher bucket in `activitywatch.py`, propagated to `FocusBlock.note` on the first merged event (same pattern as URL backfill), and (b) `cli._run_sync` runs a git reflog fallback (`git_context.find_git_branch`) for any block whose `note is None` AND whose title starts with `~/` or `/`. The fallback is **never** applied on top of an ax-sourced note. `FocusBlock.signature()` does **not** include `note` — it's enrichment, not identity; including it would break idempotency dedup when the note changes retroactively.

## Config and state

- **Config**: `~/.config/aw-notion/config.toml`. Template at repo root in `config.toml.example`. Loaded by `aw_notion/config.py` into dataclasses. `timezone` is an IANA zone name; `ZoneInfo(cfg.timezone)` is used to localize block start/end before writing to Notion.
- **State**: `~/.config/aw-notion/state.json`. Managed by `State` in `aw_notion/state.py`. The lock file (`sync.lock`) sits next to it.
- **Secrets**: the Notion integration token lives in `config.toml`, which is outside the repo. `.gitignore` defensively excludes `config.toml` at the repo root.

## Notion database schema expectations

`NotionTimeLogClient.create_entry` (`aw_notion/notion.py`) writes these properties — they must exist on the target database with these types. **Names are configurable** via `[notion.fields]` in `config.toml`; defaults shown below.

| Default name | Type | Notes |
| --- | --- | --- |
| `Entry` | Title | Block title, truncated to 100 chars |
| `Start` | Date | Local-time ISO string |
| `End` | Date | Local-time ISO string |
| `Duration` | Number | Active minutes (integer) |
| `App` | Select | Application name |
| `URL` | URL | Optional, set only when block has a URL (browser blocks) |
| `Sorted` | Checkbox | Always written as `false` |
| `Note` | Rich text | **Opt-in.** Only written when `notion.fields.note` is set. Source: ax-watcher context or git reflog fallback. Truncated to 2000 chars. |

For non-English Notion databases, override field names in `[notion.fields]` — e.g. `duration_minutes = "Время"`. See `config.toml.example`.

## Testing notes

- HTTP is mocked with `responses` (ActivityWatch) — see `tests/test_activitywatch.py`. No test hits a live ActivityWatch instance.
- `tests/test_blocks.py` uses the `dt(offset_sec)` / `win(...)` / `afk(...)` helpers for readable scenarios — reuse them when adding cases rather than hand-constructing datetimes.
- There is no network-backed Notion test; `tests/test_notion.py` mocks the Notion client via `pytest-httpx`. Do not introduce a live Notion test without a dedicated sandbox database.
- `tests/test_cli.py` exercises the lock + dry-run + since logic with monkeypatched `LOCK_PATH` and a fake AW/Notion fixture. The git-reflog fallback path is tested by monkeypatching `find_git_branch` — tests don't create real `.git` dirs.
- `tests/test_git_context.py` creates real `.git/logs/HEAD` files under `tmp_path` and asserts reflog parsing against synthetic entries (checkout, rebase finish, commit-only, detached HEAD). Extend this file when touching `git_context.py` instead of inventing a new harness.
