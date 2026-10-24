"""Durable record hashing and narrowly scoped Git commits."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import ConflictError, WorkspaceError
from .util import atomic_json, atomic_text, safe_child


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_digest(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


@contextmanager
def operation_lock(root: Path, operation: str) -> Iterator[None]:
    """Serialize one durable operation across threads and processes."""
    path = root / ".oneco" / "locks" / f"operation-{sha256_bytes(operation.encode())}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def committed_file_matches_head(repo: Path, path: Path) -> bool:
    """Check that a durable path is tracked at HEAD with identical bytes."""
    repo = repo.resolve()
    path = path.resolve()
    if not (repo / ".git").exists():
        return path.is_file()
    if path != repo and repo not in path.parents:
        return False
    relative = path.relative_to(repo).as_posix()
    result = _git(repo, "show", f"HEAD:{relative}")
    return result.returncode == 0 and result.stdout.encode() == path.read_bytes()


def load_committed_text(repo: Path, path: Path) -> str | None:
    """Load text from ``HEAD`` so dirty or untracked working bytes never gain authority."""
    repo = repo.resolve()
    path = path.resolve()
    if path != repo and repo not in path.parents:
        return None
    if not (repo / ".git").exists():
        return path.read_text(encoding="utf-8") if path.is_file() else None
    relative = path.relative_to(repo).as_posix()
    result = _git(repo, "show", f"HEAD:{relative}")
    if result.returncode:
        return None
    return result.stdout


def load_committed_json(repo: Path, path: Path) -> Any | None:
    """Load JSON from ``HEAD`` so dirty or untracked working bytes never gain authority."""
    content = load_committed_text(repo, path)
    return json.loads(content) if content is not None else None


def latest_committed_launch_record(repo: Path, decisions: Path) -> dict[str, Any] | None:
    """Return the newest valid launch decision committed at ``repo`` HEAD.

    Launch endorsements deliberately share the ``launch-`` filename prefix, so
    callers must validate record shape rather than treating a glob match as a
    launch decision. Timestamps decide recency; paths provide a stable tie-break.
    """
    candidates: list[tuple[datetime, str, dict[str, Any]]] = []
    for path in decisions.glob("launch-*.json"):
        try:
            value = load_committed_json(repo, path)
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(value, dict):
            continue
        digest = value.get("launch_digest")
        created_at = value.get("created_at")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in digest)
            or not isinstance(created_at, str)
        ):
            continue
        try:
            timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if timestamp.tzinfo is None:
            continue
        candidates.append((timestamp, path.as_posix(), value))
    return max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def composite_digest(root: Path, paths: Iterable[Path]) -> str:
    """Hash stable relative paths and bytes, not timestamps or filesystem ordering."""
    root = root.resolve()
    digest = hashlib.sha256()
    for path in sorted((item.resolve() for item in paths), key=lambda item: str(item)):
        if path != root and root not in path.parents:
            raise WorkspaceError(f"digest path escapes root: {path}")
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def _git(
    repo: Path, *args: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    process_environment = os.environ.copy()
    if environment:
        process_environment.update(environment)
    process_environment.setdefault("GIT_AUTHOR_NAME", "OneCo")
    process_environment.setdefault("GIT_AUTHOR_EMAIL", "oneco@local")
    process_environment.setdefault("GIT_COMMITTER_NAME", "OneCo")
    process_environment.setdefault("GIT_COMMITTER_EMAIL", "oneco@local")
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env=process_environment,
    )


def commit_exact(repo: Path, paths: Iterable[Path], message: str) -> str | None:
    """Commit only supplied paths through an isolated index.

    The caller's real index is never read or written, so unrelated staged work survives
    unchanged. ``update-ref`` provides a compare-and-swap boundary if another writer moves
    HEAD while the durable commit is being assembled.
    """
    repo = repo.resolve()
    if not (repo / ".git").exists():
        return None
    worktree = _git(repo, "rev-parse", "--is-inside-work-tree")
    if worktree.returncode != 0 or worktree.stdout.strip() != "true":
        return None
    relative: list[str] = []
    for path in paths:
        resolved = path.resolve()
        if resolved != repo and repo not in resolved.parents:
            raise WorkspaceError(f"commit path escapes repository: {path}")
        relative.append(resolved.relative_to(repo).as_posix())
    if not relative:
        raise WorkspaceError("a durable commit requires at least one path")
    conflict = _git(repo, "diff", "--name-only", "--diff-filter=U", "--", *relative)
    if conflict.returncode:
        raise WorkspaceError(conflict.stderr.strip() or "failed to inspect Git conflicts")
    if conflict.stdout.strip():
        raise ConflictError(f"durable record path has unresolved conflict: {conflict.stdout.strip()}")
    last_conflict = "Git HEAD changed while recording durable truth"
    for attempt in range(3):
        head_result = _git(repo, "rev-parse", "--verify", "HEAD")
        old_head = head_result.stdout.strip() if head_result.returncode == 0 else None
        descriptor, index_name = tempfile.mkstemp(prefix="oneco-index-", dir=repo / ".git")
        os.close(descriptor)
        Path(index_name).unlink()
        isolated = {"GIT_INDEX_FILE": index_name}
        try:
            if old_head:
                result = _git(repo, "read-tree", old_head, environment=isolated)
                if result.returncode:
                    raise WorkspaceError(
                        result.stderr.strip() or "failed to seed durable Git index"
                    )
            result = _git(repo, "add", "-A", "--", *relative, environment=isolated)
            if result.returncode:
                raise WorkspaceError(result.stderr.strip() or "failed to stage durable record")
            tree = _git(repo, "write-tree", environment=isolated)
            if tree.returncode:
                raise WorkspaceError(tree.stderr.strip() or "failed to write durable Git tree")
            if old_head:
                unchanged = _git(repo, "diff-tree", "--quiet", old_head, tree.stdout.strip())
                if unchanged.returncode == 0:
                    return old_head
            commit_args = ["commit-tree", tree.stdout.strip(), "-m", message]
            if old_head:
                commit_args += ["-p", old_head]
            committed = _git(repo, *commit_args)
            if committed.returncode:
                raise WorkspaceError(
                    committed.stderr.strip() or "failed to create durable commit"
                )
            new_head = committed.stdout.strip()
            expected = old_head or ("0" * 40)
            updated = _git(repo, "update-ref", "HEAD", new_head, expected)
            if updated.returncode:
                last_conflict = updated.stderr.strip() or last_conflict
                if attempt < 2:
                    continue
                raise ConflictError(f"{last_conflict}; retry limit reached")
            refreshed = _git(repo, "reset", "-q", new_head, "--", *relative)
            if refreshed.returncode:
                raise WorkspaceError(
                    refreshed.stderr.strip() or "durable commit succeeded but index refresh failed"
                )
            return new_head
        finally:
            Path(index_name).unlink(missing_ok=True)
    raise ConflictError(f"{last_conflict}; retry limit reached")


def persist_json(
    root: Path,
    relative: str | Path,
    value: Any,
    *,
    actor: str,
    subject: str,
    commit: bool = True,
) -> tuple[Path, str | None]:
    path = safe_child(root, relative)
    if path.exists():
        raise ConflictError(f"append-only durable record already exists: {path.relative_to(root)}")
    atomic_json(path, value)
    revision = commit_exact(root, [path], f"oneco({actor.lower()}): {subject}") if commit else None
    return path, revision


def persist_text(
    root: Path,
    relative: str | Path,
    value: str,
    *,
    actor: str,
    subject: str,
    commit: bool = True,
) -> tuple[Path, str | None]:
    path = safe_child(root, relative)
    if path.exists():
        raise ConflictError(f"append-only durable record already exists: {path.relative_to(root)}")
    atomic_text(path, value)
    revision = commit_exact(root, [path], f"oneco({actor.lower()}): {subject}") if commit else None
    return path, revision
