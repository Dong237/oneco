"""Native Ghostty window transport for macOS executives and Owner observation."""

from __future__ import annotations

import ctypes
import json
import platform
import plistlib
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ..errors import WorkspaceError

GHOSTTY_APP = Path("/Applications/Ghostty.app")
APPLESCRIPT_TIMEOUT_SECONDS = 10
NEW_WINDOW_DISCOVERY_TIMEOUT_SECONDS = 2.0
SPACE_MOVE_TIMEOUT_SECONDS = 2.0


def available() -> bool:
    return platform.system() == "Darwin" and GHOSTTY_APP.exists() and shutil.which("osascript") is not None


def _run_applescript(source: str, *arguments: str) -> str:
    if not available():
        raise WorkspaceError("Ghostty with AppleScript support is required on macOS")
    try:
        result = subprocess.run(
            ["osascript", "-", *arguments],
            input=source,
            capture_output=True,
            text=True,
            check=False,
            timeout=APPLESCRIPT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(
            f"Ghostty AppleScript request timed out after {APPLESCRIPT_TIMEOUT_SECONDS}s"
        ) from exc
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

CLOSE_WINDOW_SCRIPT = r'''
on run argv
  set wantedId to item 1 of argv
  tell application "Ghostty"
    repeat with candidate in windows
      if (id of candidate as text) is wantedId then
        close window candidate
        return "closed"
      end if
    end repeat
  end tell
  return "missing"
end run
'''


def _titled_command(command: list[str], title: str | None) -> str:
    joined = shlex.join(command)
    if not title:
        return joined
    # OSC 0 gives each native Ghostty window a stable human-readable role title.
    script = "printf '\\033]0;%s\\007' " + shlex.quote(title) + "; exec " + joined
    return shlex.join(["/bin/sh", "-lc", script])


def _active_space_id() -> int:
    try:
        skylight = ctypes.CDLL("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight")
        skylight.SLSMainConnectionID.argtypes = []
        skylight.SLSMainConnectionID.restype = ctypes.c_uint32
        skylight.SLSGetActiveSpace.argtypes = [ctypes.c_uint32]
        skylight.SLSGetActiveSpace.restype = ctypes.c_uint64
        connection = skylight.SLSMainConnectionID()
        space_id = int(skylight.SLSGetActiveSpace(connection))
    except (AttributeError, OSError) as exc:
        raise WorkspaceError("cannot determine the current macOS Space") from exc
    if space_id <= 0:
        raise WorkspaceError("cannot determine the current macOS Space")
    return space_id


def _space_type(space_id: int) -> int:
    try:
        from Quartz import SLSBridgedSpaceGetTypeOperation

        result = (
            SLSBridgedSpaceGetTypeOperation.alloc()
            .initWithSpaceID_(space_id)
            .performWithWMBridgeDelegate()
        )
        return int(result.workspaceType())
    except (AttributeError, ImportError, OSError, TypeError, ValueError) as exc:
        raise WorkspaceError("cannot determine whether the current macOS Space is usable") from exc


def _ghostty_window_ids() -> set[int]:
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowLayer,
            kCGWindowListOptionAll,
            kCGWindowNumber,
            kCGWindowOwnerName,
        )
    except ImportError as exc:
        raise WorkspaceError("cannot inspect Ghostty windows on this Mac") from exc
    rows = CGWindowListCopyWindowInfo(kCGWindowListOptionAll, kCGNullWindowID) or []
    return {
        int(row[kCGWindowNumber])
        for row in rows
        if row.get(kCGWindowOwnerName) == "Ghostty"
        and int(row.get(kCGWindowLayer, -1)) == 0
        and row.get(kCGWindowNumber) is not None
    }


def _wait_for_new_ghostty_window(previous: set[int]) -> int:
    deadline = time.monotonic() + NEW_WINDOW_DISCOVERY_TIMEOUT_SECONDS
    while True:
        created = _ghostty_window_ids() - previous
        if len(created) == 1:
            return next(iter(created))
        if len(created) > 1:
            raise WorkspaceError(
                "cannot identify the exact new Ghostty window; no window was moved"
            )
        if time.monotonic() >= deadline:
            raise WorkspaceError("cannot locate the new Ghostty window on the current macOS Space")
        time.sleep(0.05)


def _move_window_to_space(window_id: int, space_id: int) -> None:
    try:
        from Quartz import SLSBridgedMoveWindowsToManagedSpaceOperation

        operation = (
            SLSBridgedMoveWindowsToManagedSpaceOperation.alloc().initWithWindows_spaceID_(
                [window_id], space_id
            )
        )
        operation.performWithWMBridgeDelegate()
    except (AttributeError, ImportError, OSError, TypeError) as exc:
        raise WorkspaceError("cannot move the executive window to the current macOS Space") from exc
    deadline = time.monotonic() + SPACE_MOVE_TIMEOUT_SECONDS
    while space_id not in _window_space_ids(window_id):
        if time.monotonic() >= deadline:
            raise WorkspaceError(
                "macOS did not place the executive window on the requested current Space"
            )
        time.sleep(0.05)


def _window_space_ids(window_id: int) -> set[int]:
    try:
        import objc
        from Foundation import NSArray

        skylight = ctypes.CDLL("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight")
        skylight.SLSMainConnectionID.argtypes = []
        skylight.SLSMainConnectionID.restype = ctypes.c_uint32
        skylight.SLSCopySpacesForWindows.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        skylight.SLSCopySpacesForWindows.restype = ctypes.c_void_p
        connection = skylight.SLSMainConnectionID()
        windows = NSArray.arrayWithObject_(window_id)
        result = skylight.SLSCopySpacesForWindows(
            connection,
            0x7,
            objc.pyobjc_id(windows),
        )
        if not result:
            return set()
        spaces = objc.objc_object(c_void_p=result)
        return {int(value) for value in spaces}
    except (AttributeError, ImportError, OSError, TypeError, ValueError) as exc:
        raise WorkspaceError("cannot verify the executive window's macOS Space") from exc


def open_window(cwd: Path, command: list[str], *, title: str | None = None) -> dict[str, str]:
    target_space = _active_space_id()
    if _space_type(target_space) != 0:
        raise WorkspaceError(
            "The current macOS Space cannot host a Ghostty executive window. "
            "Exit full screen or switch to a normal desktop, then retry; OneCo created no window."
        )
    previous_windows = _ghostty_window_ids()
    output = _run_applescript(
        OPEN_WINDOW_SCRIPT, str(cwd.resolve()), _titled_command(command, title)
    )
    try:
        handle = {str(key): str(value) for key, value in json.loads(output).items()}
    except json.JSONDecodeError as exc:
        raise WorkspaceError("Ghostty returned an invalid window handle") from exc
    window_id = handle.get("window_id")
    if not window_id:
        raise WorkspaceError("Ghostty returned an incomplete window handle")
    try:
        native_window_id = _wait_for_new_ghostty_window(previous_windows)
        _move_window_to_space(native_window_id, target_space)
        _run_applescript(FOCUS_SCRIPT, window_id)
    except WorkspaceError:
        try:
            _run_applescript(CLOSE_WINDOW_SCRIPT, window_id)
        except WorkspaceError:
            pass
        raise
    return handle


def focus(window_id: str) -> None:
    _run_applescript(FOCUS_SCRIPT, window_id)


def close_window(window_id: str) -> None:
    """Close one exact OneCo-owned Ghostty window without touching other windows."""

    _run_applescript(CLOSE_WINDOW_SCRIPT, window_id)


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
    command = [tmux, "-L", target.split(":", 1)[0], "attach-session", "-r", "-t", target]
    return open_window(cwd, command, title=title)


def health() -> dict[str, Any]:
    if not available():
        return {"state": "missing", "diagnostic": "Ghostty 1.3+ and osascript are required"}
    try:
        with (GHOSTTY_APP / "Contents" / "Info.plist").open("rb") as handle:
            metadata = plistlib.load(handle)
        version = str(metadata.get("CFBundleShortVersionString") or "installed")
    except (OSError, plistlib.InvalidFileException) as exc:
        return {"state": "degraded", "diagnostic": f"cannot read Ghostty bundle: {exc}"}
    return {
        "state": "ok",
        "diagnostic": (
            f"Ghostty {version}; Automation permission is checked when OneCo opens a role window"
        ),
    }
