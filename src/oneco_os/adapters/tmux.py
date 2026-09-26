"""tmux transport. Delivery remains best-effort; SQLite is authoritative."""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from ..errors import WorkspaceError
from ..util import slugify


def require_tmux() -> str:
    binary = shutil.which("tmux")
    if not binary:
        raise WorkspaceError("tmux is required for the tmux terminal adapter")
    return binary


def session_name(company: str) -> str:
    return "oneco-" + slugify(company)[:40]


def oneco_command() -> list[str]:
    return [sys.executable, "-m", "oneco_os.cli"]


def has_session(name: str) -> bool:
    return (
        subprocess.run(
            [require_tmux(), "has-session", "-t", name], capture_output=True, check=False
        ).returncode
        == 0
    )


def ensure_session(name: str, root: Path) -> None:
    if has_session(name):
        return
    command = shlex.join([*oneco_command(), "board", "--root", str(root)])
    result = subprocess.run(
        [require_tmux(), "new-session", "-d", "-s", name, "-n", "board", "-c", str(root), command],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise WorkspaceError(result.stderr.strip() or "failed to create tmux company session")


def ensure_window(name: str, window: str, cwd: Path, command: list[str]) -> str:
    tmux = require_tmux()
    target = f"{name}:{window}"
    exists = subprocess.run(
        [tmux, "list-windows", "-t", name, "-F", "#{window_name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if exists.returncode == 0 and window in exists.stdout.splitlines():
        return target
    result = subprocess.run(
        [tmux, "new-window", "-d", "-t", name, "-n", window, "-c", str(cwd), shlex.join(command)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise WorkspaceError(result.stderr.strip() or f"failed to create tmux window {window}")
    return target


def select_window(name: str, window: str) -> None:
    subprocess.run([require_tmux(), "select-window", "-t", f"{name}:{window}"], check=False)


def notify(target: str | None, text: str) -> bool:
    if not target:
        return False
    compact = re.sub(r"\s+", " ", text).strip()[:240]
    result = subprocess.run(
        [require_tmux(), "display-message", "-t", target, compact], capture_output=True, check=False
    )
    return result.returncode == 0


def target_alive(target: str) -> bool:
    """Return whether a tmux target exists and its pane process is still running."""
    result = subprocess.run(
        [require_tmux(), "display-message", "-p", "-t", target, "#{pane_dead}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "0"


def ensure_owner_target(name: str, window: str, cwd: Path, command: list[str]) -> str:
    """Create or respawn an observable atomic Owner worker window."""
    tmux = require_tmux()
    if not has_session(name):
        result = subprocess.run(
            [tmux, "new-session", "-d", "-s", name, "-n", window, "-c", str(cwd), shlex.join(command)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise WorkspaceError(result.stderr.strip() or "failed to create Owner yard")
        subprocess.run([tmux, "set-option", "-t", name, "remain-on-exit", "on"], check=False)
        return f"{name}:{window}"
    windows = subprocess.run(
        [tmux, "list-windows", "-t", name, "-F", "#{window_name}:#{pane_dead}"],
        capture_output=True,
        text=True,
        check=False,
    )
    states = dict(line.rsplit(":", 1) for line in windows.stdout.splitlines() if ":" in line)
    target = f"{name}:{window}"
    if window not in states:
        ensure_window(name, window, cwd, command)
    elif states[window] == "1":
        result = subprocess.run(
            [tmux, "respawn-window", "-k", "-t", target, "-c", str(cwd), shlex.join(command)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise WorkspaceError(result.stderr.strip() or f"failed to respawn {target}")
    return target
