"""One-off cleanup: archive existing Notion timelog entries matching
sync.exclude_apps / sync.exclude_url_substrings in ~/.config/aw-notion/config.toml.

After deploying the exclusion filter we still had historical entries from
before the filter existed. This script queries Notion directly (not the
notion2git cache, which is 7-day-windowed) and archives matching pages.

Idempotent — re-running finds 0 entries (already archived).
"""
import os
import sys
import time
import tomllib
from pathlib import Path

CONFIG = Path.home() / ".config/aw-notion/config.toml"
cfg = tomllib.loads(CONFIG.read_text())

sys.path.insert(0, str(Path.home() / "code/aw-notion/.venv/lib/python3.11/site-packages"))
from notion_client import Client

client = Client(auth=cfg["notion"]["token"], notion_version="2022-06-28")
db = cfg["notion"]["timelog_db"]
exclude_apps = cfg["sync"].get("exclude_apps", [])
exclude_url_subs = cfg["sync"].get("exclude_url_substrings", [])
app_field = cfg.get("notion", {}).get("fields", {}).get("app", "App")
url_field = cfg.get("notion", {}).get("fields", {}).get("url", "URL")

if not exclude_apps and not exclude_url_subs:
    sys.exit("No exclusion rules in config; nothing to purge.")

print(f"Rules: apps={exclude_apps}  url_substrings={exclude_url_subs}")

filters = []
for app in exclude_apps:
    filters.append({"property": app_field, "select": {"equals": app}})
for sub in exclude_url_subs:
    filters.append({"property": url_field, "url": {"contains": sub}})

query = {"or": filters} if len(filters) > 1 else filters[0]

print(f"Querying Notion DB {db} ...")
to_archive = []
cursor = None
while True:
    body = {"filter": query, "page_size": 100}
    if cursor:
        body["start_cursor"] = cursor
    # v3.0.0 client.databases.query is gone; use the low-level request API
    # which still hits POST /v1/databases/{id}/query (Notion supports legacy).
    resp = client.request(path=f"databases/{db}/query", method="POST", body=body)
    for page in resp["results"]:
        if page.get("archived"):
            continue
        to_archive.append(page["id"])
    if not resp.get("has_more"):
        break
    cursor = resp.get("next_cursor")

print(f"\nFound {len(to_archive)} non-archived matching pages.")
if not to_archive:
    sys.exit(0)

resp = input("Archive all of them? [y/N] ").strip().lower()
if resp != "y":
    sys.exit("aborted.")

errors = 0
for i, pid in enumerate(to_archive, 1):
    try:
        client.pages.update(page_id=pid, archived=True)
        print(f"  [{i}/{len(to_archive)}] archived {pid}")
        time.sleep(0.35)  # Notion rate limit
    except Exception as e:
        print(f"  [{i}/{len(to_archive)}] FAILED {pid}: {e}")
        errors += 1

print(f"\nDone. {len(to_archive) - errors} archived, {errors} errors.")
