"""Host capability discovery without changing the user's Trae profile."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .adapters import ghostty, trae
from .errors import WorkspaceError
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


def _skill_health(path: Path, expected_name: str) -> dict[str, str]:
    if not path.is_file():
        return {
            "state": "missing",
            "diagnostic": f"required external skill contract not found: {path}",
        }
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"state": "incompatible", "diagnostic": f"cannot read {path}: {exc}"}
    frontmatter = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|\Z)", text, re.DOTALL)
    name_match = (
        re.search(r"(?m)^name:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", frontmatter.group(1))
        if frontmatter
        else None
    )
    declared = name_match.group(1).strip() if name_match else None
    if declared != expected_name:
        return {
            "state": "incompatible",
            "diagnostic": (
                f"expected external skill {expected_name}, found {declared or 'no declared name'} "
                f"at {path}"
            ),
        }
    body = text[frontmatter.end() :] if frontmatter else text
    description_match = (
        re.search(r"(?m)^description:\s*(?:>|\|)?\s*\S", frontmatter.group(1))
        if frontmatter
        else None
    )
    substantive = re.sub(r"\s+", "", body)
    if not description_match or len(substantive) < 40 or not re.search(r"(?m)^#\s+\S", body):
        return {
            "state": "incompatible",
            "diagnostic": (
                f"external skill contract needs a description and substantive Markdown body: {path}"
            ),
        }
    return {"state": "ok", "diagnostic": str(path)}


def collect(workspace: Workspace, *, persist: bool = True) -> list[dict[str, Any]]:
    checks: dict[str, tuple[str, dict[str, str]]] = {
        "ghostty": ("host", ghostty.health()),
        "tmux": ("host", _command_health("tmux")),
        "trae": ("host", _command_health("traecli")),
        "specify": ("host", _command_health("specify")),
        "agent-reach": (
            "external-skill",
            _skill_health(
                Path.home() / ".agents" / "skills" / "agent-reach" / "SKILL.md",
                "agent-reach",
            ),
        ),
        "analyzing-a-product-skill": (
            "external-skill",
            _skill_health(
                Path.home() / ".trae" / "skills" / "analyzing-a-product-skill" / "SKILL.md",
                "analyzing-a-product-skill",
            ),
        ),
        "analyzing-a-business-system": (
            "external-skill",
            _skill_health(
                Path.home() / ".trae" / "skills" / "analyzing-a-business-system" / "SKILL.md",
                "analyzing-a-business-system",
            ),
        ),
        "proposing-a-product-bet": (
            "external-skill",
            _skill_health(
                Path.home() / ".trae" / "skills" / "proposing-a-product-bet" / "SKILL.md",
                "proposing-a-product-bet",
            ),
        ),
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
    except (OSError, ValueError, WorkspaceError) as exc:
        diagnostic = {"state": "degraded", "diagnostic": str(exc)}
        checks["trae"] = ("host", diagnostic)
        checks["oneco-plugin"] = ("trae", diagnostic)
        checks["oneco-mcp-session-env"] = ("trae", diagnostic)
        checks["user-mcp-profile"] = ("trae", diagnostic)
    rows = []
    for name, (source, result) in checks.items():
        if persist:
            workspace.runtime.set_capability_health(
                name, source, result["state"], result["diagnostic"]
            )
        rows.append({"name": name, "source": source, **result})
    return rows
