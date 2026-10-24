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


def server_name(name_or_target: str) -> str:
    """Use a dedicated tmux server for each OneCo company session."""
    return name_or_target.split(":", 1)[0]


def server_command(name_or_target: str, *arguments: str) -> list[str]:
    return [require_tmux(), "-L", server_name(name_or_target), *arguments]


def has_session(name: str) -> bool:
    return (
        subprocess.run(
            server_command(name, "has-session", "-t", name), capture_output=True, check=False
        ).returncode
        == 0
    )


def ensure_session(name: str, root: Path) -> None:
    if has_session(name):
        return
    command = shlex.join([*oneco_command(), "board", "--root", str(root)])
    result = subprocess.run(
        server_command(
            name, "new-session", "-d", "-s", name, "-n", "board", "-c", str(root), command
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise WorkspaceError(result.stderr.strip() or "failed to create tmux company session")


def ensure_window(name: str, window: str, cwd: Path, command: list[str]) -> str:
    target = f"{name}:{window}"
    exists = subprocess.run(
        server_command(name, "list-windows", "-t", name, "-F", "#{window_name}"),
        capture_output=True,
        text=True,
        check=False,
    )
    if exists.returncode == 0 and window in exists.stdout.splitlines():
        return target
    result = subprocess.run(
        server_command(
            name,
            "new-window",
            "-d",
            "-t",
            name,
            "-n",
            window,
            "-c",
            str(cwd),
            shlex.join(command),
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise WorkspaceError(result.stderr.strip() or f"failed to create tmux window {window}")
    return target


def select_window(name: str, window: str) -> None:
    target = f"{name}:{window}"
    subprocess.run(server_command(target, "select-window", "-t", target), check=False)


def notify(target: str | None, text: str) -> bool:
    if not target:
        return False
    compact = re.sub(r"\s+", " ", text).strip()[:240]
    result = subprocess.run(
        server_command(target, "display-message", "-t", target, compact),
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def target_alive(target: str) -> bool:
    """Return whether a tmux target exists and its pane process is still running."""
    result = subprocess.run(
        server_command(target, "display-message", "-p", "-t", target, "#{pane_dead}"),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "0"


def ensure_owner_target(name: str, window: str, cwd: Path, command: list[str]) -> str:
    """Create or respawn an observable atomic Owner worker window."""
    if not has_session(name):
        result = subprocess.run(
            server_command(
                name,
                "new-session",
                "-d",
                "-s",
                name,
                "-n",
                window,
                "-c",
                str(cwd),
                shlex.join(command),
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise WorkspaceError(result.stderr.strip() or "failed to create Owner yard")
        subprocess.run(
            server_command(name, "set-option", "-t", name, "remain-on-exit", "on"),
            check=False,
        )
        return f"{name}:{window}"
    windows = subprocess.run(
        server_command(name, "list-windows", "-t", name, "-F", "#{window_name}:#{pane_dead}"),
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
            server_command(
                target,
                "respawn-window",
                "-k",
                "-t",
                target,
                "-c",
                str(cwd),
                shlex.join(command),
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise WorkspaceError(result.stderr.strip() or f"failed to respawn {target}")
    return target
