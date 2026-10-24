"""Desktop lifecycle and atomic Owner-turn orchestration."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any

from .adapters import ghostty, tmux, trae
from .errors import ProjectError, StaleSessionError, WorkspaceError
from .models import AuthorityKind, ProjectState, WorkState
from .records import operation_lock
from .services import reconcile_owner_work
from .workspace import Workspace

_LAUNCH_LOCKS: dict[str, threading.Lock] = {}
_LAUNCH_LOCKS_GUARD = threading.Lock()
EXECUTIVE_START_TIMEOUT_SECONDS = 5.0


def _process_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def executive_session_alive(session: dict[str, Any]) -> bool:
    """Use the process handshake for new launches; preserve legacy window-only sessions."""

    window_alive = ghostty.is_alive(session.get("terminal_handle"))
    if session.get("launch_nonce"):
        return window_alive and _process_alive(session.get("pid"))
    return window_alive


def _wait_for_executive_process(
    workspace: Workspace,
    session_id: str,
    epoch: int,
    launch_nonce: str,
    *,
    timeout: float = EXECUTIVE_START_TIMEOUT_SECONDS,
) -> bool:
    try:
        logical_id = str(workspace.runtime.assert_current(session_id, epoch)["logical_id"])
    except StaleSessionError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = workspace.runtime.current_session(logical_id)
        if (
            current
            and current.get("lifecycle") == "live"
            and current.get("id") == session_id
            and int(current.get("epoch", -1)) == int(epoch)
            and current.get("launch_nonce") == launch_nonce
            and _process_alive(current.get("pid"))
        ):
            return True
        if not current or current.get("lifecycle") != "live":
            return False
        time.sleep(0.05)
    return False


@contextmanager
def _executive_launch_lock(workspace: Workspace, role: str):
    key = f"{workspace.root.resolve()}:{role}"
    with _LAUNCH_LOCKS_GUARD:
        thread_lock = _LAUNCH_LOCKS.setdefault(key, threading.Lock())
    with thread_lock:
        lock_path = workspace.root / ".oneco" / "locks" / f"launch-{role.lower()}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def owner_yard_name(workspace: Workspace) -> str:
    root_digest = hashlib.sha256(str(workspace.root.resolve()).encode()).hexdigest()[:10]
    return tmux.session_name(workspace.manifest().company_name) + f"-{root_digest}-owners"


def owner_target(workspace: Workspace, project_id: str) -> str:
    return f"{owner_yard_name(workspace)}:{project_id.lower()}"


def open_executive(
    workspace: Workspace,
    role: str,
    *,
    permission: str = trae.DEFAULT_PERMISSION_MODE,
    resume: bool = True,
) -> dict[str, Any]:
    role = role.upper()
    permission = trae.unattended_permission_mode(permission)
    if role not in {"CPO", "CEO", "CTO"}:
        raise WorkspaceError("persistent role must be CPO, CEO, or CTO")
    with _executive_launch_lock(workspace, role):
        authority = AuthorityKind.ADVISORY if role == "CPO" else AuthorityKind.CANONICAL
        current = workspace.runtime.current_session(role)
        if (
            current
            and current["lifecycle"] == "live"
            and executive_session_alive(current)
        ):
            ghostty.focus(str(current["terminal_handle"]))
            return {"status": "focused", "session": current}

        if (
            current
            and current["lifecycle"] == "live"
            and current.get("launch_nonce")
            and current.get("terminal_handle")
        ):
            try:
                ghostty.close_window(str(current["terminal_handle"]))
            except WorkspaceError:
                pass
            workspace.runtime.mark_lost(str(current["id"]), int(current["epoch"]))
            current = workspace.runtime.current_session(role)

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
                authority_kind=authority,
                terminal_transport="ghostty",
            )
        else:
            session = workspace.runtime.register_session(
                role,
                workspace.root,
                provider="trae",
                authority_kind=authority,
                terminal_transport="ghostty",
            )
        launch_nonce = secrets.token_urlsafe(24)
        workspace.runtime.prepare_process_launch(
            str(session["session_id"]), int(session["epoch"]), launch_nonce
        )
        command = trae.executive_command(
            workspace.root,
            role,
            str(session["session_id"]),
            int(session["epoch"]),
            model=workspace.manifest().model,
            permission=permission,
            provider_session_id=provider_id,
            launch_nonce=launch_nonce,
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
        if not _wait_for_executive_process(
            workspace,
            str(session["session_id"]),
            int(session["epoch"]),
            launch_nonce,
        ):
            try:
                ghostty.close_window(handle["window_id"])
            finally:
                workspace.runtime.mark_lost(
                    str(session["session_id"]), int(session["epoch"])
                )
            raise WorkspaceError(
                f"{role} window opened but its OneCo process did not start; "
                "wake or unlock the display and retry"
            )
        return {"status": "opened", "session": session, "terminal": handle}


def focus_executive(
    workspace: Workspace,
    role: str,
    *,
    permission: str = trae.DEFAULT_PERMISSION_MODE,
) -> dict[str, Any]:
    return open_executive(workspace, role, permission=permission)


def branch_executive(
    workspace: Workspace,
    role: str,
    purpose: str,
    *,
    candidate: bool = False,
    permission: str = trae.DEFAULT_PERMISSION_MODE,
) -> dict[str, Any]:
    role = role.upper()
    permission = trae.unattended_permission_mode(permission)
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
    launch_nonce = secrets.token_urlsafe(24)
    workspace.runtime.prepare_process_launch(
        str(session["session_id"]), int(session["epoch"]), launch_nonce
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
        launch_nonce=launch_nonce,
    )
    branch_label = "Recovery candidate" if candidate else "Research delegate"
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
    if not _wait_for_executive_process(
        workspace,
        str(session["session_id"]),
        int(session["epoch"]),
        launch_nonce,
    ):
        try:
            ghostty.close_window(handle["window_id"])
        finally:
            workspace.runtime.mark_lost(str(session["session_id"]), int(session["epoch"]))
        raise WorkspaceError("research delegate window opened but its OneCo process did not start")
    return {"logical_id": logical_id, "session": session, "terminal": handle}


def wake_owner(
    workspace: Workspace,
    project_id: str,
    *,
    permission: str = trae.DEFAULT_PERMISSION_MODE,
) -> dict[str, Any]:
    with operation_lock(workspace.root, f"owner-launch:{project_id}"):
        return _wake_owner_unlocked(workspace, project_id, permission=permission)


def _wake_owner_unlocked(
    workspace: Workspace,
    project_id: str,
    *,
    permission: str,
) -> dict[str, Any]:
    permission = trae.unattended_permission_mode(permission)
    project = workspace.find_project(project_id)
    if project.state is not ProjectState.ACTIVE:
        raise ProjectError(f"{project_id} is {project.state}; Owner wake requires active")
    logical_id = f"{project_id}:owner"
    reconcile_owner_work(workspace, project_id)
    target = owner_target(workspace, project_id)
    current = workspace.runtime.current_session(logical_id)
    worker_alive = tmux.target_alive(target) if current and current["lifecycle"] == "live" else False
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
    if not any(item["status"] == "queued" for item in workspace.runtime.queued_work(logical_id)):
        return {"status": "idle", "session": current, "tmux_target": target}
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


def _owner_outcome_paths(
    workspace: Workspace, project_id: str, session_id: str, epoch: int
) -> set[str]:
    project_root = workspace.project_path(project_id)
    outcomes: set[str] = set()
    with workspace.runtime.connect() as connection:
        outcomes.update(
            str(row["file_path"])
            for row in connection.execute(
                """SELECT file_path FROM checkpoint_index
                WHERE project_id=? AND session_id=? AND epoch=?""",
                (project_id, session_id, epoch),
            )
        )
    for path in (project_root / "blockers").glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("recorded_session_id") == session_id and data.get("recorded_epoch") == epoch:
            outcomes.add(str(path.resolve()))
    release_root = workspace.root / "operations" / project_id / "releases"
    for path in release_root.glob("rel_*/result.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("recorded_session_id") == session_id and data.get("recorded_epoch") == epoch:
            outcomes.add(str(path.resolve()))
    return outcomes


def run_owner_turn(
    workspace: Workspace,
    project_id: str,
    session_id: str,
    epoch: int,
    *,
    permission: str = trae.DEFAULT_PERMISSION_MODE,
) -> int:
    logical_id = f"{project_id}:owner"
    session = workspace.runtime.assert_current(session_id, epoch)
    if session["logical_id"] != logical_id:
        raise WorkspaceError(f"{session_id} does not own {logical_id}")
    binary = trae.require_model(workspace.manifest().model)
    exit_code = 0
    try:
        while True:
            reconcile_owner_work(workspace, project_id)
            work = workspace.runtime.claim_work(logical_id)
            if work is None:
                break
            token = str(work["claim_token"])
            workspace.runtime.start_work(str(work["id"]), token)
            workspace.runtime.touch(session_id, epoch, WorkState.WORKING)
            messages = []
            if work.get("message_id"):
                messages = [
                    row
                    for row in workspace.runtime.inbox(logical_id)
                    if row["id"] == work["message_id"]
                ]
            instruction = (
                messages[0]["body"] if messages else "Continue the next approved Spec task."
            )
            outcomes_before = _owner_outcome_paths(workspace, project_id, session_id, epoch)
            prompt = (
                "Use $oneco:oneco-protocol and $oneco:oneco-owner. This is one atomic Owner turn. "
                "Read AGENTS.md, project.json, BRIEF.md, active Spec Kit files, and your OneCo inbox. "
                f"Work item:\n{instruction}\n"
                "Execute the smallest complete vertical result, verify it, record a checkpoint or blocker "
                "through OneCo MCP, then end this turn. Do not wait for routine confirmation. "
                "Do not invoke a generic test-generation workflow unless this work item explicitly "
                "requires creating or revising tests."
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
            environment = trae.managed_environment(
                {
                    "ONECO_ROOT": workspace.root,
                    "ONECO_IDENTITY": logical_id,
                    "ONECO_SESSION_ID": session_id,
                    "ONECO_EPOCH": epoch,
                    "ONECO_AUTHORITY_KIND": AuthorityKind.OWNER,
                    "ONECO_SESSION_MODE": "atomic_owner",
                }
            )
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=workspace.project_path(project_id),
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except OSError as error:
                exit_code = 1
                retrying = workspace.runtime.retry_work(
                    str(work["id"]), token, f"Trae could not start: {error}"
                )
                workspace.runtime.touch(
                    session_id, epoch, WorkState.IDLE if retrying else WorkState.BLOCKED
                )
                break
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
            status = workspace.runtime.work_status(str(work["id"]))
            if status == "done":
                workspace.runtime.touch(session_id, epoch, WorkState.IDLE)
                continue
            if exit_code:
                retrying = workspace.runtime.retry_work(
                    str(work["id"]), token, f"Trae exited with status {exit_code}"
                )
                workspace.runtime.touch(
                    session_id, epoch, WorkState.IDLE if retrying else WorkState.BLOCKED
                )
                break
            if _owner_outcome_paths(workspace, project_id, session_id, epoch) == outcomes_before:
                exit_code = 1
                retrying = workspace.runtime.retry_work(
                    str(work["id"]), token, "Owner turn exited without a durable outcome"
                )
                workspace.runtime.touch(
                    session_id, epoch, WorkState.IDLE if retrying else WorkState.BLOCKED
                )
                break
            workspace.runtime.finish_work(str(work["id"]), token)
            if messages and messages[0]["acked_at"] is None:
                workspace.runtime.acknowledge(str(messages[0]["id"]), logical_id)
            workspace.runtime.touch(session_id, epoch, WorkState.IDLE)
    finally:
        try:
            workspace.runtime.mark_lost(session_id, epoch)
        except Exception:
            pass
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
    launch_nonce: str | None = None,
) -> None:
    permission = trae.unattended_permission_mode(permission)
    workspace.runtime.bind_process(
        session_id,
        epoch,
        os.getpid(),
        launch_nonce=launch_nonce,
    )
    binary = trae.require_model(model)
    environment = trae.managed_environment(
        {
            "ONECO_ROOT": workspace.root,
            "ONECO_IDENTITY": logical_id,
            "ONECO_SESSION_ID": session_id,
            "ONECO_EPOCH": epoch,
        }
    )
    common = [
        str(binary),
        *trae.isolated_config_argv(workspace.root),
        "--no-daemon",
        "--dangerously-bypass-hook-trust",
        "--cd",
        str(workspace.root),
        "--model",
        model,
        *trae.permission_argv(permission),
    ]
    if fork_from:
        argv = [*common, "fork", fork_from, advisory_prompt or ""]
    elif resume:
        argv = [*common, "resume", resume]
    else:
        prompt = advisory_prompt or trae.role_prompt(logical_id, workspace.root)
        argv = [*common, prompt]
    os.execvpe(str(binary), argv, environment)


def python_command() -> list[str]:
    return [sys.executable, "-m", "oneco_os.cli"]
