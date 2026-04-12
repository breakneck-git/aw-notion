"""Git reflog enrichment for terminal focus blocks.

When a focus block's title looks like a filesystem path (e.g. `~/code/aw-notion`)
and that path is a git repository, find the branch active at the block's
end time by walking `.git/logs/HEAD`. Returns a `repo @ branch` string or None.

Only `checkout` entries (and `rebase (finish): returning to refs/heads/X`)
change the tracked branch. Commit/pull/merge entries stay on the same branch,
so we carry forward the most recent known branch. If no checkout appears in
the reflog before the block, fall back to reading `.git/HEAD` directly.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)


def find_git_branch(path: str, block_end: datetime) -> str | None:
    repo = Path(path).expanduser()
    git_dir = repo / ".git"
    if not git_dir.is_dir():
        return None

    branch = _branch_at(git_dir, block_end)
    if branch is None:
        return None
    return f"{repo.name} @ {branch}"


def _branch_at(git_dir: Path, block_end: datetime) -> str | None:
    logs_head = git_dir / "logs" / "HEAD"
    if not logs_head.is_file():
        return _read_head(git_dir)

    try:
        content = logs_head.read_text()
    except OSError as e:
        log.debug("cannot read %s: %s", logs_head, e)
        return _read_head(git_dir)

    branch: str | None = None
    for line in content.splitlines():
        tab = line.find("\t")
        if tab == -1:
            continue
        meta, message = line[:tab], line[tab + 1 :]
        parts = meta.rsplit(" ", 2)
        if len(parts) != 3:
            continue
        try:
            ts_epoch = int(parts[1])
        except ValueError:
            continue
        ts = datetime.fromtimestamp(ts_epoch, tz=UTC)
        if ts > block_end:
            break
        extracted = _extract_branch_from_message(message)
        if extracted:
            branch = extracted

    if branch is None:
        branch = _read_head(git_dir)
    return branch


def _extract_branch_from_message(message: str) -> str | None:
    if message.startswith("checkout: moving from "):
        _, _, target = message.partition(" to ")
        target = target.strip()
        return target or None
    if message.startswith("rebase (finish): returning to refs/heads/"):
        return message[len("rebase (finish): returning to refs/heads/") :].strip() or None
    return None


def _read_head(git_dir: Path) -> str | None:
    head = git_dir / "HEAD"
    if not head.is_file():
        return None
    try:
        content = head.read_text().strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    if content.startswith(prefix):
        return content[len(prefix) :]
    return None
