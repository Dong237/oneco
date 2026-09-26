"""Native Ghostty window transport for macOS executives and Owner observation."""

from __future__ import annotations

import json
import platform
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..errors import WorkspaceError

GHOSTTY_APP = Path("/Applications/Ghostty.app")


def available() -> bool:
    return platform.system() == "Darwin" and GHOSTTY_APP.exists() and shutil.which("osascript") is not None


def _run_applescript(source: str, *arguments: str) -> str:
    if not available():
        raise WorkspaceError("Ghostty with AppleScript support is required on macOS")
    result = subprocess.run(
        ["osascript", "-", *arguments],
        input=source,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or "Ghostty AppleScript request failed"
        if "not authorized" in detail.lower() or "-1743" in detail:
            detail += "; allow terminal automation in System Settings > Privacy & Security > Automation"
        raise WorkspaceError(detail)
    return result.stdout.strip()


OPEN_WINDOW_SCRIPT = r'''
on run argv
  set workDir to item 1 of argv
  set launchCommand to item 2 of argv
  tell application "Ghostty"
    set cfg to new surface configuration
    set initial working directory of cfg to workDir
    set command of cfg to launchCommand
    set wait after command of cfg to true
    set newWindow to new window with configuration cfg
    activate window newWindow
    set newTerminal to focused terminal of selected tab of newWindow
    return "{\"window_id\":\"" & (id of newWindow as text) & "\",\"terminal_id\":\"" & (id of newTerminal as text) & "\"}"
  end tell
end run
'''

FOCUS_SCRIPT = r'''
on run argv
  set wantedId to item 1 of argv
  tell application "Ghostty"
    repeat with candidate in windows
      if (id of candidate as text) is wantedId then
        activate window candidate
        activate
        return "focused"
      end if
    end repeat
  end tell
  error "Ghostty window not found"
end run
'''

WINDOW_EXISTS_SCRIPT = r'''
on run argv
  set wantedId to item 1 of argv
  tell application "Ghostty"
    repeat with candidate in windows
      if (id of candidate as text) is wantedId then return "true"
    end repeat
  end tell
  return "false"
end run
'''


def _titled_command(command: list[str], title: str | None) -> str:
    joined = shlex.join(command)
    if not title:
        return joined
    # OSC 0 gives each native Ghostty window a stable human-readable role title.
    script = "printf '\\033]0;%s\\007' " + shlex.quote(title) + "; exec " + joined
    return shlex.join(["/bin/sh", "-lc", script])


def open_window(cwd: Path, command: list[str], *, title: str | None = None) -> dict[str, str]:
    output = _run_applescript(
        OPEN_WINDOW_SCRIPT, str(cwd.resolve()), _titled_command(command, title)
    )
    try:
        return {str(key): str(value) for key, value in json.loads(output).items()}
    except json.JSONDecodeError as exc:
        raise WorkspaceError("Ghostty returned an invalid window handle") from exc


def focus(window_id: str) -> None:
    _run_applescript(FOCUS_SCRIPT, window_id)


def is_alive(window_id: str | None) -> bool:
    if not window_id or not available():
        return False
    try:
        return _run_applescript(WINDOW_EXISTS_SCRIPT, window_id) == "true"
    except WorkspaceError:
        return False


def watch_tmux(cwd: Path, target: str, *, title: str) -> dict[str, str]:
    tmux = shutil.which("tmux")
    if not tmux:
        raise WorkspaceError("tmux is required to watch an Owner")
    command = [tmux, "attach-session", "-r", "-t", target]
    return open_window(cwd, command, title=title)


def health() -> dict[str, Any]:
    if not available():
        return {"state": "missing", "diagnostic": "Ghostty 1.3+ and osascript are required"}
    result = subprocess.run(
        ["osascript", "-e", 'tell application "Ghostty" to get version'],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return {"state": "degraded", "diagnostic": result.stderr.strip()}
    return {"state": "ok", "diagnostic": f"Ghostty {result.stdout.strip()}"}
