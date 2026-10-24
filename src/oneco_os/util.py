"""Small filesystem and serialization helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import WorkspaceError


def json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def atomic_json(path: Path, value: Any, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, default=json_default)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_text(path: Path, value: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            if value and not value.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkspaceError(f"missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise WorkspaceError(f"invalid JSON in {path}: {exc}") from exc


def slugify(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not result:
        # Preserve portable machine IDs while accepting a non-Latin human-facing name.
        result = f"oneco-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:10]}"
    return result


def company_root(start: Path | None = None) -> Path:
    candidate = (start or Path.cwd()).resolve()
    for path in (candidate, *candidate.parents):
        if (path / ".oneco" / "manifest.json").is_file():
            return path
    raise WorkspaceError("not inside a OneCo company; run `oneco company init` first")


def safe_child(root: Path, relative: str | Path) -> Path:
    root = root.resolve()
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise WorkspaceError(f"path escapes company root: {relative}")
    return path


def local_socket_path(root: Path, channel: str) -> Path:
    """Return a short, user-private Unix socket path for a company service."""
    if not re.fullmatch(r"[a-z][a-z0-9-]*", channel):
        raise ValueError(f"invalid socket channel: {channel}")
    user_id = os.getuid() if hasattr(os, "getuid") else 0
    socket_dir = Path("/tmp") / f"oneco-{user_id}"
    socket_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(socket_dir, 0o700)
    company_key = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    return socket_dir / f"{company_key}-{channel}.sock"
