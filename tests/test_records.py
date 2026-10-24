from __future__ import annotations

import json
import subprocess
from pathlib import Path

from oneco_os.records import commit_exact, latest_committed_launch_record


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def test_commit_exact_creates_initial_commit_for_untracked_record(tmp_path: Path) -> None:
    repo = tmp_path / "company"
    _git(tmp_path, "init", "-q", str(repo))
    record = repo / "signals" / "SIG-0001" / "SIGNAL.md"
    record.parent.mkdir(parents=True)
    record.write_text("signal\n", encoding="utf-8")

    revision = commit_exact(repo, [record], "oneco(board): assign SIG-0001")

    assert revision == _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "show", "--pretty=", "--name-only", "HEAD") == (
        "signals/SIG-0001/SIGNAL.md"
    )


def test_commit_exact_preserves_unrelated_staged_work(tmp_path: Path) -> None:
    repo = tmp_path / "company"
    _git(tmp_path, "init", "-q", str(repo))
    baseline = repo / "README.md"
    baseline.write_text("baseline\n", encoding="utf-8")
    commit_exact(repo, [baseline], "initial")
    unrelated = repo / "user-draft.md"
    unrelated.write_text("draft\n", encoding="utf-8")
    _git(repo, "add", "user-draft.md")
    record = repo / "bets" / "BET-0001" / "bet.json"
    record.parent.mkdir(parents=True)
    record.write_text("{}\n", encoding="utf-8")

    commit_exact(repo, [record], "oneco(cpo): record Bet")

    assert _git(repo, "diff", "--cached", "--name-only") == "user-draft.md"
    assert _git(repo, "show", "--pretty=", "--name-only", "HEAD") == "bets/BET-0001/bet.json"


def test_latest_launch_record_uses_timestamp_and_ignores_prefix_collisions(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "product"
    _git(tmp_path, "init", "-q", str(repo))
    decisions = repo / "decisions"
    decisions.mkdir()
    older = {
        "launch_digest": "a" * 64,
        "launched_by": "CEO",
        "created_at": "2026-09-29T09:00:00+00:00",
    }
    newer = {
        "launch_digest": "b" * 64,
        "launched_by": "CEO",
        "created_at": "2026-09-29T10:00:00+00:00",
    }
    paths = {
        decisions / "launch-z-last-by-name.json": older,
        decisions / "launch-a-first-by-name.json": newer,
        decisions / "launch-endorsements.json": {"CEO": True, "CTO": True},
        decisions / "launch-invalid.json": {
            "launch_digest": "not-a-digest",
            "created_at": "2026-09-29T11:00:00+00:00",
        },
    }
    for path, value in paths.items():
        path.write_text(json.dumps(value), encoding="utf-8")
    commit_exact(repo, paths, "test: committed launch history")

    assert latest_committed_launch_record(repo, decisions) == newer
