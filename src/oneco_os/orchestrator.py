"""Desktop lifecycle and atomic Owner-turn orchestration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from .adapters import ghostty, tmux, trae
from .errors import ProjectError, WorkspaceError
from .models import AuthorityKind, ProjectState, WorkState
from .workspace import Workspace


def owner_yard_name(workspace: Workspace) -> str:
    return tmux.session_name(workspace.manifest().company_name) + "-owners"


def owner_target(workspace: Workspace, project_id: str) -> str:
    return f"{owner_yard_name(workspace)}:{project_id.lower()}"


def open_executive(
    workspace: Workspace,
    role: str,
    *,
    permission: str = "default",
    resume: bool = True,
) -> dict[str, Any]:
    role = role.upper()
    if role not in {"CEO", "CTO"}:
        raise WorkspaceError("executive role must be CEO or CTO")
    current = workspace.runtime.current_session(role)
    if (
        current
        and current["lifecycle"] == "live"
        and ghostty.is_alive(current.get("terminal_handle"))
    ):
        ghostty.focus(str(current["terminal_handle"]))
        return {"status": "focused", "session": current}

    provider_id = current.get("provider_session_id") if current and resume else None
    if (
        current
        and current["terminal_transport"] == "ghostty"
        and current["lifecycle"] in {"lost", "live"}
    ):
        session = workspace.runtime.resume_session(
            str(current["id"]), int(current["epoch"]), terminal_transport="ghostty"
        )
    elif current:
        session = workspace.runtime.register_session(
            role,
            workspace.root,
            provider="trae",
            replace=True,
            authority_kind=AuthorityKind.CANONICAL,
            terminal_transport="ghostty",
        )
    else:
        session = workspace.runtime.register_session(
            role,
            workspace.root,
            provider="trae",
            authority_kind=AuthorityKind.CANONICAL,
            terminal_transport="ghostty",
        )
    command = trae.executive_command(
        workspace.root,
        role,
        str(session["session_id"]),
        int(session["epoch"]),
        model=workspace.manifest().model,
        permission=permission,
        provider_session_id=provider_id,
    )
    handle = ghostty.open_window(
        workspace.root,
        command,
        title=f"OneCo \u00b7 {workspace.manifest().company_name} \u00b7 {role}",
    )
    workspace.runtime.update_terminal(
        str(session["session_id"]),
        int(session["epoch"]),
        transport="ghostty",
        handle=handle["window_id"],
    )
    return {"status": "opened", "session": session, "terminal": handle}


def focus_executive(workspace: Workspace, role: str) -> dict[str, Any]:
    return open_executive(workspace, role)


def branch_executive(
    workspace: Workspace,
    role: str,
    purpose: str,
    *,
    candidate: bool = False,
    permission: str = "default",
) -> dict[str, Any]:
    role = role.upper()
    parent = workspace.runtime.current_session(role)
    if not parent or not parent.get("provider_session_id"):
        raise WorkspaceError(f"{role} has no bound Trae thread to fork")
    logical_id = trae.branch_identity(role, purpose, candidate=candidate)
    kind = AuthorityKind.CANDIDATE if candidate else AuthorityKind.SECRETARY
    session = workspace.runtime.register_session(
        logical_id,
        workspace.root,
        provider="trae",
        authority_kind=kind,
        terminal_transport="ghostty",
        parent_session_id=str(parent["id"]),
    )
    prompt = (
        f"You are an advisory {role} Secretary for: {purpose}. You inherit the exact parent "
        f"conversation, but you are not canonical {role}. Inspect, research, and recommend. "
        f"Send findings only to {role} through OneCo. Do not endorse, launch, instruct Owners, "
        "or approve organizational actions."
        if not candidate
        else f"You are a recovery candidate for {role}. Inspect the inherited context and request succession; you have no canonical authority until the human confirms it in this chat."
    )
    command = trae.executive_command(
        workspace.root,
        logical_id,
        str(session["session_id"]),
        int(session["epoch"]),
        model=workspace.manifest().model,
        permission=permission,
        fork_from=str(parent["provider_session_id"]),
        advisory_prompt=prompt,
    )
    branch_label = "Recovery candidate" if candidate else "Secretary"
    handle = ghostty.open_window(
        workspace.root,
        command,
        title=f"OneCo \u00b7 {role} {branch_label} \u00b7 {purpose}",
    )
    workspace.runtime.update_terminal(
        str(session["session_id"]),
        int(session["epoch"]),
        transport="ghostty",
        handle=handle["window_id"],
    )
    return {"logical_id": logical_id, "session": session, "terminal": handle}


def wake_owner(workspace: Workspace, project_id: str, *, permission: str = "default") -> dict[str, Any]:
    project = workspace.find_project(project_id)
    if project.state is not ProjectState.ACTIVE:
        raise ProjectError(f"{project_id} is {project.state}; Owner wake requires active")
    logical_id = f"{project_id}:owner"
    target = owner_target(workspace, project_id)
    current = workspace.runtime.current_session(logical_id)
    worker_alive = tmux.target_alive(target) if current else False
    if current and worker_alive:
        # The existing worker owns the current atomic turn. New work remains durable in SQLite
        # and will be claimed at its next boundary; never inject keystrokes or spawn a peer writer.
        return {
            "status": "queued" if current["work_state"] == WorkState.WORKING else "awake",
            "session": current,
            "tmux_target": target,
        }
    if current:
        workspace.runtime.requeue_interrupted_work(logical_id)
    if current and current["lifecycle"] in {"live", "lost"}:
        session = workspace.runtime.resume_session(
            str(current["id"]),
            int(current["epoch"]),
            terminal_transport="tmux",
            terminal_handle=target,
        )
    elif current:
        session = workspace.runtime.register_session(
            logical_id,
            workspace.project_path(project_id),
            provider="trae",
            tmux_target=target,
            replace=True,
            authority_kind=AuthorityKind.OWNER,
            terminal_transport="tmux",
            terminal_handle=target,
        )
    else:
        session = workspace.runtime.register_session(
            logical_id,
            workspace.project_path(project_id),
            provider="trae",
            tmux_target=target,
            authority_kind=AuthorityKind.OWNER,
            terminal_transport="tmux",
            terminal_handle=target,
        )
    workspace.runtime.acquire_writer(project_id, str(session["session_id"]), int(session["epoch"]))
    command = [
        *tmux.oneco_command(),
        "internal",
        "owner-turn",
        project_id,
        "--root",
        str(workspace.root),
        "--session-id",
        str(session["session_id"]),
        "--epoch",
        str(session["epoch"]),
        "--permission",
        permission,
    ]
    tmux.ensure_owner_target(
        owner_yard_name(workspace), project_id.lower(), workspace.project_path(project_id), command
    )
    return {"status": "started", "session": session, "tmux_target": target}


def _provider_id_from_event(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("thread_id", "threadId", "session_id", "sessionId"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        for value in payload.values():
            result = _provider_id_from_event(value)
            if result:
                return result
    if isinstance(payload, list):
        for value in payload:
            result = _provider_id_from_event(value)
            if result:
                return result
    return None


def run_owner_turn(
    workspace: Workspace,
    project_id: str,
    session_id: str,
    epoch: int,
    *,
    permission: str = "default",
) -> int:
    logical_id = f"{project_id}:owner"
    session = workspace.runtime.assert_current(session_id, epoch)
    if session["logical_id"] != logical_id:
        raise WorkspaceError(f"{session_id} does not own {logical_id}")
    binary = trae.require_model(workspace.manifest().model)
    exit_code = 0
    while True:
        work = workspace.runtime.claim_work(logical_id)
        if work is None:
            break
        token = str(work["claim_token"])
        workspace.runtime.start_work(str(work["id"]), token)
        workspace.runtime.touch(session_id, epoch, WorkState.WORKING)
        messages = []
        if work.get("message_id"):
            messages = [
                row for row in workspace.runtime.inbox(logical_id) if row["id"] == work["message_id"]
            ]
        instruction = messages[0]["body"] if messages else "Continue the next approved Spec task."
        prompt = (
            "Use $oneco-protocol and $oneco-owner. This is one atomic Owner turn. "
            "Read AGENTS.md, project.json, BRIEF.md, active Spec Kit files, and your OneCo inbox. "
            f"Work item:\n{instruction}\n"
            "Execute the smallest complete vertical result, verify it, record a checkpoint or blocker "
            "through OneCo MCP, then end this turn. Do not wait for routine confirmation."
        )
        current = workspace.runtime.assert_current(session_id, epoch)
        argv = trae.exec_argv(
            binary,
            cwd=workspace.project_path(project_id),
            model=workspace.manifest().model,
            permission=permission,
            prompt=prompt,
            provider_session_id=current["provider_session_id"],
        )
        environment = os.environ.copy()
        environment.update(
            {
                "ONECO_ROOT": str(workspace.root),
                "ONECO_IDENTITY": logical_id,
                "ONECO_SESSION_ID": session_id,
                "ONECO_EPOCH": str(epoch),
                "ONECO_AUTHORITY_KIND": AuthorityKind.OWNER,
                "ONECO_PYTHON": sys.executable,
            }
        )
        process = subprocess.Popen(
            argv,
            cwd=workspace.project_path(project_id),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        provider_id = current["provider_session_id"]
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            try:
                provider_id = provider_id or _provider_id_from_event(json.loads(line))
            except json.JSONDecodeError:
                pass
        exit_code = process.wait()
        if provider_id and not current["provider_session_id"]:
            workspace.runtime.bind_provider_session(session_id, epoch, str(provider_id))
        if exit_code:
            workspace.runtime.finish_work(
                str(work["id"]), token, error=f"Trae exited with status {exit_code}"
            )
            workspace.runtime.touch(session_id, epoch, WorkState.BLOCKED)
            break
        workspace.runtime.finish_work(str(work["id"]), token)
        if messages and messages[0]["acked_at"] is None:
            workspace.runtime.acknowledge(str(messages[0]["id"]), logical_id)
        workspace.runtime.touch(session_id, epoch, WorkState.IDLE)
    return exit_code


def watch_owner(workspace: Workspace, project_id: str) -> dict[str, str]:
    project = workspace.find_project(project_id)
    target = owner_target(workspace, project_id)
    return ghostty.watch_tmux(
        workspace.project_path(project_id), target, title=f"{project.id} — {project.name}"
    )


def run_executive(
    workspace: Workspace,
    logical_id: str,
    session_id: str,
    epoch: int,
    *,
    model: str,
    permission: str,
    resume: str | None,
    fork_from: str | None,
    advisory_prompt: str | None,
) -> None:
    workspace.runtime.assert_current(session_id, epoch)
    binary = trae.require_model(model)
    environment = os.environ.copy()
    environment.update(
        {
            "ONECO_ROOT": str(workspace.root),
            "ONECO_IDENTITY": logical_id,
            "ONECO_SESSION_ID": session_id,
            "ONECO_EPOCH": str(epoch),
            "ONECO_PYTHON": sys.executable,
        }
    )
    common = ["--cd", str(workspace.root), "--model", model]
    if fork_from:
        argv = [str(binary), "fork", *common, fork_from, advisory_prompt or ""]
    elif resume:
        argv = [str(binary), "resume", *common, resume]
    else:
        prompt = advisory_prompt or trae.role_prompt(logical_id, workspace.root)
        argv = [str(binary), *common, "--permission-mode", permission, prompt]
    os.execvpe(str(binary), argv, environment)


def python_command() -> list[str]:
    return [sys.executable, "-m", "oneco_os.cli"]
