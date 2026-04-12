from datetime import UTC, datetime
from pathlib import Path

from aw_notion.git_context import find_git_branch


def _write_reflog(git_dir: Path, lines: list[str]) -> None:
    logs = git_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "HEAD").write_text("\n".join(lines) + "\n")


def _write_head(git_dir: Path, ref: str) -> None:
    (git_dir / "HEAD").write_text(f"ref: {ref}\n")


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def test_returns_none_for_non_git_directory(tmp_path):
    result = find_git_branch(str(tmp_path), datetime(2026, 4, 11, tzinfo=UTC))
    assert result is None


def test_returns_branch_from_reflog_checkout(tmp_path):
    repo = tmp_path / "aw-notion"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    _write_head(git_dir, "refs/heads/main")

    ts = _epoch(datetime(2026, 4, 11, 9, 0, tzinfo=UTC))
    _write_reflog(
        git_dir,
        [
            f"0 abc Alice <a@x> {ts} +0000\tcommit (initial): init",
            f"abc def Alice <a@x> {ts + 100} +0000\tcheckout: moving from main to feature-x",
        ],
    )

    block_end = datetime(2026, 4, 11, 10, 0, tzinfo=UTC)
    result = find_git_branch(str(repo), block_end)
    assert result == "aw-notion @ feature-x"


def test_tracks_multiple_checkouts_in_order(tmp_path):
    repo = tmp_path / "proj"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    _write_head(git_dir, "refs/heads/main")

    ts = _epoch(datetime(2026, 4, 11, 8, 0, tzinfo=UTC))
    _write_reflog(
        git_dir,
        [
            f"0 abc Alice <a@x> {ts} +0000\tcheckout: moving from main to feature-a",
            f"abc def Alice <a@x> {ts + 100} +0000\tcommit: work on A",
            f"def ghi Alice <a@x> {ts + 200} +0000\tcheckout: moving from feature-a to feature-b",
            f"ghi jkl Alice <a@x> {ts + 300} +0000\tcommit: work on B",
        ],
    )

    block_end = datetime(2026, 4, 11, 10, 0, tzinfo=UTC)
    result = find_git_branch(str(repo), block_end)
    assert result == "proj @ feature-b"


def test_ignores_entries_newer_than_block_end(tmp_path):
    repo = tmp_path / "proj"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    _write_head(git_dir, "refs/heads/main")

    early = _epoch(datetime(2026, 4, 11, 8, 0, tzinfo=UTC))
    late = _epoch(datetime(2026, 4, 11, 12, 0, tzinfo=UTC))
    _write_reflog(
        git_dir,
        [
            f"0 abc Alice <a@x> {early} +0000\tcheckout: moving from main to early-branch",
            f"abc def Alice <a@x> {late} +0000\tcheckout: moving from early-branch to late-branch",
        ],
    )

    block_end = datetime(2026, 4, 11, 10, 0, tzinfo=UTC)
    result = find_git_branch(str(repo), block_end)
    assert result == "proj @ early-branch"


def test_rebase_finish_updates_branch(tmp_path):
    repo = tmp_path / "proj"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    _write_head(git_dir, "refs/heads/main")

    ts = _epoch(datetime(2026, 4, 11, 8, 0, tzinfo=UTC))
    finish_msg = "rebase (finish): returning to refs/heads/feature"
    _write_reflog(
        git_dir,
        [
            f"0 abc Alice <a@x> {ts} +0000\tcheckout: moving from main to feature",
            f"abc def Alice <a@x> {ts + 100} +0000\trebase (start): checkout main",
            f"def ghi Alice <a@x> {ts + 200} +0000\t{finish_msg}",
        ],
    )

    block_end = datetime(2026, 4, 11, 10, 0, tzinfo=UTC)
    result = find_git_branch(str(repo), block_end)
    assert result == "proj @ feature"


def test_falls_back_to_head_when_no_checkout_in_reflog(tmp_path):
    """
    Reflog exists but has no checkout entries before block_end (repo created,
    only commits on the default branch). Fall back to reading .git/HEAD.
    """
    repo = tmp_path / "proj"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    _write_head(git_dir, "refs/heads/main")

    ts = _epoch(datetime(2026, 4, 11, 8, 0, tzinfo=UTC))
    _write_reflog(
        git_dir,
        [
            f"0 abc Alice <a@x> {ts} +0000\tcommit (initial): init",
            f"abc def Alice <a@x> {ts + 100} +0000\tcommit: more work",
        ],
    )

    block_end = datetime(2026, 4, 11, 10, 0, tzinfo=UTC)
    result = find_git_branch(str(repo), block_end)
    assert result == "proj @ main"


def test_falls_back_to_head_when_no_reflog_file(tmp_path):
    repo = tmp_path / "proj"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    _write_head(git_dir, "refs/heads/develop")

    block_end = datetime(2026, 4, 11, 10, 0, tzinfo=UTC)
    result = find_git_branch(str(repo), block_end)
    assert result == "proj @ develop"


def test_returns_none_when_head_detached(tmp_path):
    """Detached HEAD (raw SHA in .git/HEAD) with no reflog → None."""
    repo = tmp_path / "proj"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("abc123def456\n")

    block_end = datetime(2026, 4, 11, 10, 0, tzinfo=UTC)
    result = find_git_branch(str(repo), block_end)
    assert result is None


def test_expands_tilde_in_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myproj"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    _write_head(git_dir, "refs/heads/main")

    block_end = datetime(2026, 4, 11, 10, 0, tzinfo=UTC)
    result = find_git_branch("~/myproj", block_end)
    assert result == "myproj @ main"


def test_skips_malformed_reflog_lines(tmp_path):
    repo = tmp_path / "proj"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    _write_head(git_dir, "refs/heads/main")

    ts = _epoch(datetime(2026, 4, 11, 8, 0, tzinfo=UTC))
    _write_reflog(
        git_dir,
        [
            "garbage line with no tab",
            "0 abc Alice <a@x> not-a-number +0000\tcheckout: moving from main to x",
            f"abc def Alice <a@x> {ts} +0000\tcheckout: moving from main to feature",
        ],
    )

    block_end = datetime(2026, 4, 11, 10, 0, tzinfo=UTC)
    result = find_git_branch(str(repo), block_end)
    assert result == "proj @ feature"
