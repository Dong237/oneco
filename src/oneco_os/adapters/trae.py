"""TraeCode capability discovery and launch command construction."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from ..errors import WorkspaceError

DEFAULT_MODEL = "GPT-5.6-Sol"


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
    if logical_id == "CEO":
        return (
            "Use $oneco-protocol and $oneco-ceo. Read COMPANY.md, roles/CEO.md, "
            "portfolio/projects.json, then run `oneco inbox list CEO --root "
            f"{company_root} --json`. Report only: CEO online, company, "
            "project counts, decision queue, and Awaiting Board direction. Do not initiate work."
        )
    if logical_id == "CTO":
        return (
            "Use $oneco-protocol and $oneco-cto. Read COMPANY.md, roles/CTO.md, "
            "portfolio/projects.json, then run `oneco inbox list CTO --root "
            f"{company_root} --json`. Report only: CTO online, technical "
            "context status, open technical decisions, and Awaiting Board direction. Do not initiate work."
        )
    if logical_id.endswith(":owner") and project_path:
        return (
            "Use $oneco-protocol and $oneco-owner. Read AGENTS.md, project.json, BRIEF.md, "
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
    permission: str = "default",
    provider_session_id: str | None = None,
    fork_from: str | None = None,
    advisory_prompt: str | None = None,
) -> list[str]:
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
    if provider_session_id:
        return [
            str(binary),
            "exec",
            "resume",
            "--model",
            model,
            "--json",
            provider_session_id,
            prompt,
        ]
    return [
        str(binary),
        "exec",
        "--cd",
        str(cwd),
        "--model",
        model,
        "--permission-mode",
        permission,
        "--json",
        prompt,
    ]


def branch_identity(role: str, purpose: str, *, candidate: bool = False) -> str:
    label = "candidate" if candidate else "secretary"
    compact = "-".join(purpose.lower().split())[:24].strip("-") or "branch"
    return f"{role.upper()}:{label}:{compact}-{uuid.uuid4().hex[:6]}"
