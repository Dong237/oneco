"""Application services that promote runtime events into durable project records."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import AuthorizationError, OneCoError, ProjectError
from .models import ActionRequest, Checkpoint, Instruction, InstructionAction, ProjectState
from .util import atomic_json, load_json
from .workspace import Workspace


def notify_live_identity(workspace: Workspace, logical_id: str, text: str) -> None:
    """Best-effort terminal notification after durable persistence."""
    from .adapters import tmux

    target: str | None = None
    transport: str | None = None
    with workspace.runtime.connect() as connection:
        row = connection.execute(
            """SELECT s.tmux_target, s.terminal_transport
            FROM identities i LEFT JOIN sessions s ON s.id=i.active_session_id
            WHERE i.logical_id=? AND s.lifecycle='live'""",
            (logical_id,),
        ).fetchone()
        if row is not None:
            target = row["tmux_target"]
            transport = row["terminal_transport"]
    if target:
        try:
            tmux.notify(target, text)
        except (OneCoError, OSError):
            pass
    elif transport == "ghostty":
        try:
            from .notifications import notify

            notify(f"OneCo · {logical_id}", text)
        except OSError:
            pass


def record_checkpoint(
    workspace: Workspace,
    project_id: str,
    spec_id: str,
    task_id: str,
    result: str,
    verification: list[str],
    next_task: str,
    *,
    actor: str,
    session_id: str | None = None,
    epoch: int | None = None,
    commit: str | None = None,
    key_files: list[str] | None = None,
    spec_deviation: str = "none",
    complexity_change: str = "none",
    dependency_change: str = "none",
    risk_or_blocker: str = "none",
    reuse_candidate: str | None = None,
) -> tuple[Checkpoint, Path]:
    if actor != f"{project_id}:owner":
        raise AuthorizationError(f"only {project_id}:owner can checkpoint this project")
    if (session_id is None) != (epoch is None):
        raise AuthorizationError("session_id and epoch must be provided together")
    if session_id is not None and epoch is not None:
        session = workspace.runtime.assert_current(session_id, epoch)
        if session["logical_id"] != actor:
            raise AuthorizationError(f"session {session_id} does not own identity {actor}")
    project = workspace.find_project(project_id)
    if project.state not in {ProjectState.ACTIVE, ProjectState.BLOCKED_EXTERNAL}:
        raise ProjectError(f"{project_id} is {project.state}; an Owner cannot checkpoint it")
    checkpoint = Checkpoint(
        checkpoint_id=f"cp_{uuid.uuid4().hex}",
        project_id=project_id,
        spec_id=spec_id,
        task_id=task_id,
        result=result,
        commit=commit,
        key_files=key_files or [],
        verification=verification,
        spec_deviation=spec_deviation,
        complexity_change=complexity_change,
        dependency_change=dependency_change,
        risk_or_blocker=risk_or_blocker,
        next_task=next_task,
        reuse_candidate=reuse_candidate,
    )
    target = workspace.project_path(project_id)
    filename = f"{checkpoint.created_at.strftime('%Y%m%dT%H%M%SZ')}-{checkpoint.checkpoint_id}.json"
    path = target / "checkpoints" / filename
    atomic_json(path, checkpoint.model_dump(mode="json"))
    workspace.runtime.index_checkpoint(
        checkpoint.checkpoint_id,
        project_id,
        path,
        spec_id,
        task_id,
        session_id=session_id,
        epoch=epoch,
    )
    project_file = target / "project.json"
    data = load_json(project_file)
    data.update(
        {
            "current_spec": spec_id,
            "current_task": next_task,
            "latest_checkpoint": path.relative_to(target).as_posix(),
        }
    )
    atomic_json(project_file, data)
    summary = (
        f"{project_id} checkpoint {task_id}: {result}\n"
        f"Verification: {'; '.join(verification)}\nNext: {next_task}"
    )
    for recipient in ("CEO", "CTO"):
        workspace.runtime.send_message(
            actor,
            recipient,
            "checkpoint",
            summary,
            project_id=project_id,
            idempotency_key=f"{checkpoint.checkpoint_id}:{recipient}",
        )
        notify_live_identity(workspace, recipient, summary)
    workspace.runtime.event(
        "checkpoint.created",
        actor,
        {"checkpoint_id": checkpoint.checkpoint_id, "path": str(path)},
        project_id,
    )
    return checkpoint, path


def record_instruction(workspace: Workspace, instruction: Instruction) -> Path:
    project = workspace.find_project(instruction.project_id)
    if project.state not in {ProjectState.ACTIVE, ProjectState.BLOCKED_EXTERNAL}:
        raise ProjectError(
            f"{instruction.project_id} is {project.state}; instructions require a launched Owner"
        )
    target = workspace.project_path(instruction.project_id)
    filename = (
        f"{instruction.created_at.strftime('%Y%m%dT%H%M%SZ')}-{instruction.instruction_id}.json"
    )
    path = target / "instructions" / filename
    atomic_json(path, instruction.model_dump(mode="json"))
    body = (
        f"{instruction.action} for {instruction.spec_id}/{instruction.task_id}\n"
        f"Observed: {instruction.observed}\nRequired: {instruction.required_action}\n"
        f"Do not: {instruction.do_not}\nEffective: {instruction.effective_boundary}"
    )
    message_id = workspace.runtime.send_message(
        instruction.sender_authority.upper(),
        f"{instruction.project_id}:owner",
        "instruction",
        body,
        project_id=instruction.project_id,
        requires_ack=True,
        idempotency_key=instruction.instruction_id,
    )
    workspace.runtime.enqueue_work(
        f"{instruction.project_id}:owner",
        instruction.project_id,
        message_id=message_id,
        priority=(
            20
            if instruction.action
            in {InstructionAction.CORRECT, InstructionAction.NARROW, InstructionAction.SIMPLIFY}
            else 100
        ),
    )
    notify_live_identity(
        workspace,
        f"{instruction.project_id}:owner",
        f"{instruction.sender_authority.upper()} {instruction.action}: {instruction.required_action}",
    )
    workspace.runtime.event(
        "instruction.created",
        instruction.sender_authority.upper(),
        {"instruction_id": instruction.instruction_id, "action": instruction.action},
        instruction.project_id,
    )
    return path


def create_action_request(
    workspace: Workspace,
    project_id: str,
    requester: str,
    domain: str,
    summary: str,
    effect: str,
    reversible: bool,
) -> ActionRequest:
    workspace.find_project(project_id)
    action = ActionRequest(
        action_id=f"act_{uuid.uuid4().hex}",
        requester=requester,
        project_id=project_id,
        domain=domain,
        summary=summary,
        effect=effect,
        reversible=reversible,
    )
    workspace.runtime.create_action(action.model_dump(mode="json"))
    recipients = {"product": ("CEO",), "technical": ("CTO",), "cross_domain": ("CEO", "CTO")}[
        action.domain
    ]
    for recipient in recipients:
        workspace.runtime.send_message(
            requester,
            recipient,
            "action_request",
            f"{summary}\nEffect: {effect}",
            project_id=project_id,
            requires_ack=True,
            idempotency_key=f"{action.action_id}:{recipient}",
        )
        notify_live_identity(workspace, recipient, f"Approval requested: {summary}")
    request_path = workspace.project_path(project_id) / "decisions" / f"{action.action_id}.json"
    atomic_json(request_path, {**action.model_dump(mode="json"), "status": "pending"})
    return action


def decide_action(
    workspace: Workspace, action_id: str, approver: str, decision: str, note: str = ""
) -> tuple[str, Path]:
    """Apply an approval and mirror the complete approval trail into durable project truth."""
    status = workspace.runtime.decide_action(action_id, approver, decision, note)
    runtime_record = workspace.runtime.action(action_id)
    project_id = runtime_record["project_id"]
    path = workspace.project_path(project_id) / "decisions" / f"{action_id}.json"
    durable = load_json(path)
    durable["status"] = status
    durable["approvals"] = runtime_record["approvals"]
    atomic_json(path, durable)
    workspace.runtime.event(
        "action.decided",
        approver.upper(),
        {"action_id": action_id, "decision": decision, "status": status},
        project_id,
    )
    return status, path


def record_decision(
    workspace: Workspace,
    title: str,
    decision: str,
    rationale: str,
    *,
    actor: str,
    project_id: str | None = None,
) -> Path:
    if actor.upper() not in {"BOARD", "CEO", "CTO"}:
        raise AuthorizationError("only BOARD, CEO, or CTO can record a company decision")
    identifier = f"dec_{uuid.uuid4().hex}"
    created = datetime.now(UTC)
    data: dict[str, Any] = {
        "decision_id": identifier,
        "title": title,
        "decision": decision,
        "rationale": rationale,
        "actor": actor.upper(),
        "project_id": project_id,
        "created_at": created.isoformat(),
    }
    path = (
        workspace.root
        / "portfolio"
        / "decisions"
        / f"{created.strftime('%Y%m%dT%H%M%SZ')}-{identifier}.json"
    )
    atomic_json(path, data)
    workspace.runtime.event("decision.recorded", actor.upper(), data, project_id)
    return path


def rebuild_runtime(workspace: Workspace) -> dict[str, int]:
    """Rebuild only indexes and messages derivable from durable project records."""
    workspace.runtime.reset_indexes()
    counts = {"checkpoints": 0, "instructions": 0, "actions": 0}
    for project in workspace.portfolio().projects:
        target = workspace.project_path(project.id)
        for path in sorted((target / "checkpoints").glob("*.json")):
            checkpoint = Checkpoint.model_validate(json.loads(path.read_text(encoding="utf-8")))
            workspace.runtime.index_checkpoint(
                checkpoint.checkpoint_id, project.id, path, checkpoint.spec_id, checkpoint.task_id
            )
            counts["checkpoints"] += 1
        for path in sorted((target / "instructions").glob("*.json")):
            instruction = Instruction.model_validate(json.loads(path.read_text(encoding="utf-8")))
            message_id = workspace.runtime.send_message(
                instruction.sender_authority.upper(),
                f"{project.id}:owner",
                "instruction",
                f"{instruction.action}: {instruction.required_action}",
                project_id=project.id,
                requires_ack=True,
                idempotency_key=instruction.instruction_id,
            )
            workspace.runtime.enqueue_work(
                f"{project.id}:owner",
                project.id,
                message_id=message_id,
                priority=(
                    20
                    if instruction.action
                    in {
                        InstructionAction.CORRECT,
                        InstructionAction.NARROW,
                        InstructionAction.SIMPLIFY,
                    }
                    else 100
                ),
            )
            counts["instructions"] += 1
        for path in sorted((target / "decisions").glob("act_*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            action = ActionRequest.model_validate(
                {k: v for k, v in data.items() if k not in {"status", "approvals"}}
            )
            workspace.runtime.create_action(action.model_dump(mode="json"))
            for approval in data.get("approvals", []):
                workspace.runtime.decide_action(
                    action.action_id,
                    approval["approver"],
                    approval["decision"],
                    approval.get("note", ""),
                )
            counts["actions"] += 1
    workspace.runtime.event("runtime.rebuilt", "BOARD", counts)
    return counts
