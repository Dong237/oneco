"""Host capability discovery without changing the user's Trae profile."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from .adapters import ghostty, trae
from .workspace import Workspace

ONECO_MCP_SESSION_ENV = {
    "ONECO_ROOT",
    "ONECO_IDENTITY",
    "ONECO_SESSION_ID",
    "ONECO_EPOCH",
    "ONECO_PYTHON",
}


def _command_health(command: str) -> dict[str, str]:
    path = shutil.which(command)
    return (
        {"state": "ok", "diagnostic": path or command}
        if path
        else {"state": "missing", "diagnostic": f"{command} not found on PATH"}
    )


def collect(workspace: Workspace, *, persist: bool = True) -> list[dict[str, Any]]:
    checks: dict[str, tuple[str, dict[str, str]]] = {
        "ghostty": ("host", ghostty.health()),
        "tmux": ("host", _command_health("tmux")),
        "trae": ("host", _command_health("traecli")),
        "specify": ("host", _command_health("specify")),
    }
    try:
        binary = trae.find_trae()
        result = subprocess.run(
            [str(binary), "plugin", "list"],
            capture_output=True,
            text=True,
            check=False,
        )
        installed = result.returncode == 0 and any(
            line.strip().lower() in {"oneco", "\u2713 oneco"}
            or "oneco@local" in line.lower()
            for line in result.stdout.splitlines()
        )
        checks["oneco-plugin"] = (
            "trae",
            {
                "state": "ok" if installed else "missing",
                "diagnostic": (
                    "installed"
                    if installed
                    else result.stderr.strip() or "run oneco host install-trae --yes"
                ),
            },
        )
        session_env_result = subprocess.run(
            [str(binary), "mcp", "get", "oneco", "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        if session_env_result.returncode == 0:
            try:
                payload = json.loads(session_env_result.stdout)
                configured = set(payload.get("transport", {}).get("env_vars", []))
                missing = sorted(ONECO_MCP_SESSION_ENV - configured)
                session_env = {
                    "state": "degraded" if missing else "ok",
                    "diagnostic": (
                        "missing MCP environment passthrough: " + ", ".join(missing)
                        if missing
                        else "ONECO session environment passthrough configured"
                    ),
                }
            except (AttributeError, TypeError, ValueError):
                session_env = {
                    "state": "degraded",
                    "diagnostic": "invalid OneCo MCP configuration reported by Trae",
                }
        else:
            session_env = {
                "state": "degraded",
                "diagnostic": (
                    session_env_result.stderr.strip()
                    or "Trae could not inspect the OneCo MCP configuration"
                ),
            }
        checks["oneco-mcp-session-env"] = ("trae", session_env)
        mcp_result = subprocess.run(
            [str(binary), "mcp", "list", "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        checks["user-mcp-profile"] = (
            "trae",
            {
                "state": "ok" if mcp_result.returncode == 0 else "degraded",
                "diagnostic": "inherited from the normal Trae profile"
                if mcp_result.returncode == 0
                else (mcp_result.stderr.strip() or "Trae MCP discovery failed"),
            },
        )
    except (OSError, ValueError) as exc:
        diagnostic = {"state": "degraded", "diagnostic": str(exc)}
        checks["oneco-plugin"] = ("trae", diagnostic)
        checks["oneco-mcp-session-env"] = ("trae", diagnostic)
    rows = []
    for name, (source, result) in checks.items():
        if persist:
            workspace.runtime.set_capability_health(
                name, source, result["state"], result["diagnostic"]
            )
        rows.append({"name": name, "source": source, **result})
    return rows
