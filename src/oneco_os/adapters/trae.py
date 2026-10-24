"""TraeCode capability discovery and launch command construction."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
import uuid
from collections.abc import Mapping
from pathlib import Path

from ..errors import WorkspaceError

DEFAULT_MODEL = "GPT-5.6-Sol"
DEFAULT_PERMISSION_MODE = "bypass_permissions"


def managed_environment(updates: Mapping[str, object] | None = None) -> dict[str, str]:
    """Build an agent environment pinned to this OneCo Python distribution."""
    environment = os.environ.copy()
    executable = sys.executable
    executable_dir = str(Path(executable).parent)
    inherited_path = environment.get("PATH", "")
    path_parts = [part for part in inherited_path.split(os.pathsep) if part != executable_dir]
    environment["PATH"] = os.pathsep.join([executable_dir, *path_parts])
    environment["ONECO_PYTHON"] = executable
    if updates:
        environment.update({key: str(value) for key, value in updates.items()})
    return environment


def unattended_permission_mode(permission: str) -> str:
    """Keep OneCo agents out of approval-loop mode."""
    return (
        permission if permission in {DEFAULT_PERMISSION_MODE, "auto"} else DEFAULT_PERMISSION_MODE
    )


def permission_argv(permission: str) -> list[str]:
    """Return a Trae permission override that cannot conflict with approval reviewers.

    Trae rejects ``--permission-mode`` when the user's configuration also sets an
    approvals reviewer. Its dedicated full-access flag is intentionally compatible
    with that configuration and is therefore the canonical OneCo unattended mode.
    """
    permission = unattended_permission_mode(permission)
    if permission == DEFAULT_PERMISSION_MODE:
        return ["--dangerously-bypass-approvals-and-sandbox"]
    return ["--permission-mode", permission]


def isolated_config_argv(cwd: Path | None = None) -> list[str]:
    """Trust the OneCo workspace and isolate its Trae role processes.

    OneCo keeps the user's normal Trae configuration intact. Per-process overrides stop
    unrelated integrations from delaying or breaking persistent company roles, keep the
    installed OneCo plugin enabled, and prevent a fresh company from stopping at Trae's
    separate workspace-trust prompt.
    """
    if os.environ.get("ONECO_TRAE_ISOLATE", "1").lower() in {"0", "false", "no"}:
        return []
    home = Path(os.environ.get("TRAE_HOME", Path.home() / ".trae"))
    config_path = home / "traecli.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        config = {}
    overrides: list[str] = []
    for plugin_id in sorted(config.get("plugins", {})):
        if plugin_id != "oneco@local":
            # Trae's dotted override parser treats quotes in a key segment
            # literally. Plugin ids containing ``@`` or ``-`` must therefore
            # be passed as an unquoted segment.
            overrides.extend(["-c", f"plugins.{plugin_id}.enabled=false"])
    # OneCo's MCP server is supplied by the installed plugin. Disable every
    # user-configured server individually; an empty table override is merged
    # with the base config and therefore does not remove existing servers.
    for server_id in sorted(config.get("mcp_servers", {})):
        overrides.extend(["-c", f"mcp_servers.{server_id}.enabled=false"])
    for marketplace in sorted(config.get("marketplaces", {})):
        if marketplace != "local":
            overrides.extend(["-c", f"marketplaces.{marketplace}.auto_update=false"])

    # A user's granular "approve" setting otherwise overrides Full Access for
    # individual OneCo tools. Route those checks through Trae's automatic
    # reviewer so a role never blocks waiting for a person to approve its own
    # protocol operations.
    oneco_plugin = config.get("plugins", {}).get("oneco@local", {})
    oneco_server = oneco_plugin.get("mcp_servers", {}).get("oneco", {})
    overrides.extend(
        [
            "-c",
            "plugins.oneco@local.mcp_servers.oneco.default_tools_approval_mode=\"auto\"",
        ]
    )
    for tool_id in sorted(oneco_server.get("tools", {})):
        overrides.extend(
            [
                "-c",
                f"plugins.oneco@local.mcp_servers.oneco.tools.{tool_id}.approval_mode=\"auto\"",
            ]
        )

    if cwd is not None:
        trusted_path = json.dumps(str(cwd.resolve()))
        overrides.extend(
            ["-c", f'projects={{{trusted_path}={{trust_level="trusted"}}}}']
        )
    return overrides


def find_trae() -> Path:
    override = os.environ.get("ONECO_TRAE_BIN")
    if override and Path(override).is_file():
        return Path(override).resolve()
    for name in ("trae", "traex", "traecli"):
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    release_root = Path.home() / ".local" / "share" / "traex" / "releases"
    candidates = sorted(
        release_root.glob("*/traex"), key=lambda path: path.stat().st_mtime, reverse=True
    )
    if candidates:
        return candidates[0].resolve()
    raise WorkspaceError("TraeCode CLI was not found; set ONECO_TRAE_BIN to its executable")


def available_models(binary: Path | None = None) -> list[str]:
    binary = binary or find_trae()
    result = subprocess.run(
        [str(binary), "models", "--json"], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise WorkspaceError(result.stderr.strip() or "failed to query Trae models")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise WorkspaceError("Trae returned invalid model metadata") from exc
    return [
        str(item.get("name") or item.get("id"))
        for item in payload
        if item.get("name") or item.get("id")
    ]


def require_model(model: str, binary: Path | None = None) -> Path:
    binary = binary or find_trae()
    models = available_models(binary)
    if model not in models:
        raise WorkspaceError(
            f"required Trae model {model!r} is unavailable; configure a model explicitly, never silently substitute"
        )
    return binary


def role_prompt(logical_id: str, company_root: Path, project_path: Path | None = None) -> str:
    if logical_id == "CPO":
        return (
            "Use $oneco:oneco-protocol and $oneco:oneco-cpo. Read COMPANY.md, roles/CPO.md, "
            "the Signal and Product Bet indexes, then call oneco_company_snapshot. "
            "Report only: CPO online, assigned Signals, Bets awaiting evidence or Board decision, "
            "and Awaiting Board direction. Do not initiate unassigned research."
        )
    if logical_id == "CEO":
        return (
            "Use $oneco:oneco-protocol and $oneco:oneco-ceo. Read COMPANY.md, roles/CEO.md, "
            "portfolio/projects.json, then run `oneco inbox list CEO --root "
            f"{company_root} --json`. Report only: CEO online, company, "
            "project counts, decision queue, and Awaiting Board direction. Do not initiate work."
        )
    if logical_id == "CTO":
        return (
            "Use $oneco:oneco-protocol and $oneco:oneco-cto. Read COMPANY.md, roles/CTO.md, "
            "portfolio/projects.json, then run `oneco inbox list CTO --root "
            f"{company_root} --json`. Report only: CTO online, technical "
            "context status, open technical decisions, and Awaiting Board direction. Do not initiate work."
        )
    if logical_id.endswith(":owner") and project_path:
        return (
            "Use $oneco:oneco-protocol and $oneco:oneco-owner. Read AGENTS.md, project.json, BRIEF.md, "
            "the active Spec Kit artifacts, then run `oneco inbox list "
            f"{logical_id} --root {company_root} --json`. State project, stage, mandate, "
            "write boundary, and next action; then continuously execute the approved task queue."
        )
    raise WorkspaceError(f"unsupported logical identity: {logical_id}")


def oneco_command() -> list[str]:
    return [sys.executable, "-m", "oneco_os.cli"]


def executive_command(
    company_root: Path,
    logical_id: str,
    session_id: str,
    epoch: int,
    *,
    model: str,
    permission: str = DEFAULT_PERMISSION_MODE,
    provider_session_id: str | None = None,
    fork_from: str | None = None,
    advisory_prompt: str | None = None,
    launch_nonce: str | None = None,
) -> list[str]:
    permission = unattended_permission_mode(permission)
    command = [
        *oneco_command(),
        "internal",
        "executive-run",
        logical_id,
        "--root",
        str(company_root),
        "--session-id",
        session_id,
        "--epoch",
        str(epoch),
        "--model",
        model,
        "--permission",
        permission,
    ]
    if provider_session_id:
        command += ["--resume", provider_session_id]
    if fork_from:
        command += ["--fork-from", fork_from]
    if advisory_prompt:
        command += ["--advisory-prompt", advisory_prompt]
    if launch_nonce:
        command += ["--launch-nonce", launch_nonce]
    return command


def exec_argv(
    binary: Path,
    *,
    cwd: Path,
    model: str,
    permission: str,
    prompt: str,
    provider_session_id: str | None = None,
) -> list[str]:
    permission = unattended_permission_mode(permission)
    if provider_session_id:
        return [
            str(binary),
            *isolated_config_argv(cwd),
            "--dangerously-bypass-hook-trust",
            "exec",
            "resume",
            "--model",
            model,
            *permission_argv(permission),
            "--json",
            provider_session_id,
            prompt,
        ]
    return [
        str(binary),
        *isolated_config_argv(cwd),
        "--dangerously-bypass-hook-trust",
        "exec",
        "--cd",
        str(cwd),
        "--model",
        model,
        *permission_argv(permission),
        "--json",
        prompt,
    ]


def branch_identity(role: str, purpose: str, *, candidate: bool = False) -> str:
    label = "candidate" if candidate else "secretary"
    compact = "-".join(purpose.lower().split())[:24].strip("-") or "branch"
    return f"{role.upper()}:{label}:{compact}-{uuid.uuid4().hex[:6]}"
