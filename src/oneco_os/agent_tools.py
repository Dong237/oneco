"""Authorized operations shared by MCP and the local desktop."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from .authority import AgentContext, public_context, require_canonical, require_owner
from .models import Instruction, InstructionAction
from .services import create_action_request, decide_action, record_checkpoint, record_instruction
from .util import slugify


def company_snapshot(context: AgentContext) -> dict[str, Any]:
    workspace = context.workspace
    runtime = workspace.runtime.snapshot(extended=True)
    projects = []
    for project in workspace.portfolio().projects:
        ready, reasons = workspace.readiness(project.id)
        projects.append(
            {
                **project.model_dump(mode="json"),
                "ready": ready,
                "readiness_reasons": reasons,
                "launch_packet": workspace.runtime.launch_packet(project.id),
            }
        )
    return {
        "company": workspace.manifest().model_dump(mode="json", exclude_none=True),
        "self": public_context(context),
        "projects": projects,
        **runtime,
    }


def project_get(context: AgentContext, project_id: str) -> dict[str, Any]:
    project = context.workspace.find_project(project_id)
    path = context.workspace.project_path(project_id)
    ready, reasons = context.workspace.readiness(project_id)
    return {
        **project.model_dump(mode="json"),
        "ready": ready,
        "readiness_reasons": reasons,
        "brief": (path / "BRIEF.md").read_text(encoding="utf-8") if (path / "BRIEF.md").is_file() else "",
        "launch_packet": context.workspace.runtime.launch_packet(project_id),
    }


def project_create(
    context: AgentContext, name: str, slug: str | None = None, *, initialize_git: bool = True
) -> dict[str, Any]:
    require_canonical(context, "CEO", "CTO")
    target = Path(slug) if slug else Path(slugify(name))
    project = context.workspace.register_project(name, target, initialize_git=initialize_git)
    return project.model_dump(mode="json")


def project_endorse(context: AgentContext, project_id: str, note: str) -> dict[str, Any]:
    role = require_canonical(context, "CEO", "CTO")
    path = context.workspace.endorse_launch(project_id, role, note)
    ready, reasons = context.workspace.readiness(project_id)
    return {"path": str(path), "ready": ready, "reasons": reasons}


def project_launch(context: AgentContext, project_id: str) -> dict[str, Any]:
    role = require_canonical(context, "CEO", "CTO")
    override = None
    packet = context.workspace.runtime.launch_packet(project_id) or {}
    digest = context.workspace.launch_digest(project_id)
    if not (
        packet.get("ceo_endorsed_digest") == digest
        and packet.get("cto_endorsed_digest") == digest
    ):
        if not context.provider_thread_id:
            raise ValueError("missing provider thread identity for Board override verification")
        override = context.workspace.runtime.consume_launch_override(
            project_id, context.provider_thread_id
        )
    project = context.workspace.activate_from_launch(
        project_id, actor=role, board_override_turn_id=override
    )
    message_id = context.workspace.runtime.send_message(
        role,
        f"{project_id}:owner",
        "launch",
        "Launch the approved first vertical Spec. Read the full launch packet, use Spec Kit, and execute continuously.",
        project_id=project_id,
        requires_ack=True,
        idempotency_key=f"launch:{project_id}:{context.workspace.launch_digest(project_id)}",
    )
    work_id = context.workspace.runtime.enqueue_work(
        f"{project_id}:owner", project_id, message_id=message_id, priority=10
    )
    from .orchestrator import wake_owner

    wake_owner(context.workspace, project_id)
    return {"project": project.model_dump(mode="json"), "work_id": work_id, "override": override}


def inbox_list(context: AgentContext, unread_only: bool = True) -> list[dict[str, Any]]:
    return context.workspace.runtime.inbox(context.logical_id, unread_only=unread_only)


def message_send(
    context: AgentContext,
    recipient: str,
    kind: str,
    body: str,
    project_id: str | None = None,
    requires_ack: bool = False,
) -> dict[str, Any]:
    if context.authority_kind in {"secretary", "candidate"}:
        parent = context.logical_id.split(":", 1)[0]
        if recipient != parent:
            raise PermissionError(f"{context.authority_kind} may message only canonical {parent}")
    message_id = context.workspace.runtime.send_message(
        context.logical_id,
        recipient,
        kind,
        body,
        project_id=project_id,
        requires_ack=requires_ack,
    )
    return {"message_id": message_id}


def message_ack(context: AgentContext, message_id: str) -> dict[str, str]:
    context.workspace.runtime.acknowledge(message_id, context.logical_id)
    return {"message_id": message_id, "status": "acknowledged"}


def owner_instruct(
    context: AgentContext,
    project_id: str,
    action: str,
    spec_id: str,
    task_id: str,
    observed: str,
    evidence: str,
    required_action: str,
    do_not: str,
    acceptance_delta: str = "none",
    requires_spec_revision: bool = False,
) -> dict[str, Any]:
    role = require_canonical(context, "CEO", "CTO")
    instruction = Instruction(
        instruction_id=f"inst_{uuid.uuid4().hex}",
        sender_authority=role,
        action=InstructionAction(action.upper()),
        project_id=project_id,
        spec_id=spec_id,
        task_id=task_id,
        observed=observed,
        evidence=evidence,
        required_action=required_action,
        do_not=do_not,
        acceptance_delta=acceptance_delta,
        requires_spec_revision=requires_spec_revision,
    )
    path = record_instruction(context.workspace, instruction)
    priority = 20 if instruction.action in {InstructionAction.CORRECT, InstructionAction.NARROW, InstructionAction.SIMPLIFY} else 100
    message = context.workspace.runtime.inbox(f"{project_id}:owner")[-1]
    work_id = context.workspace.runtime.enqueue_work(
        f"{project_id}:owner", project_id, message_id=message["id"], priority=priority
    )
    from .orchestrator import wake_owner

    wake_owner(context.workspace, project_id)
    return {"instruction_id": instruction.instruction_id, "path": str(path), "work_id": work_id}


def checkpoint_record(
    context: AgentContext,
    project_id: str,
    spec_id: str,
    task_id: str,
    result: str,
    verification: list[str],
    next_task: str,
    key_files: list[str] | None = None,
    risk_or_blocker: str = "none",
) -> dict[str, Any]:
    require_owner(context, project_id)
    checkpoint, path = record_checkpoint(
        context.workspace,
        project_id,
        spec_id,
        task_id,
        result,
        verification,
        next_task,
        actor=context.logical_id,
        session_id=context.session_id,
        epoch=context.epoch,
        key_files=key_files or [],
        risk_or_blocker=risk_or_blocker,
    )
    return {"checkpoint": checkpoint.model_dump(mode="json"), "path": str(path)}


def blocker_record(
    context: AgentContext, project_id: str, summary: str, evidence: str, requested_decision: str
) -> dict[str, Any]:
    require_owner(context, project_id)
    body = f"Blocker: {summary}\nEvidence: {evidence}\nDecision needed: {requested_decision}"
    ids = []
    for recipient in ("CEO", "CTO"):
        ids.append(
            context.workspace.runtime.send_message(
                context.logical_id,
                recipient,
                "blocker",
                body,
                project_id=project_id,
                requires_ack=True,
            )
        )
    context.workspace.runtime.touch(context.session_id, context.epoch, "blocked")
    return {"message_ids": ids}


def action_request(
    context: AgentContext,
    project_id: str,
    domain: str,
    summary: str,
    effect: str,
    reversible: bool,
) -> dict[str, Any]:
    if context.authority_kind not in {"owner", "canonical"}:
        raise PermissionError("only canonical executives and Owners may request external actions")
    action = create_action_request(
        context.workspace, project_id, context.logical_id, domain, summary, effect, reversible
    )
    return action.model_dump(mode="json")


def action_decide(
    context: AgentContext, action_id: str, decision: str, note: str = ""
) -> dict[str, Any]:
    role = require_canonical(context, "CEO", "CTO")
    status, path = decide_action(context.workspace, action_id, role, decision, note)
    return {"status": status, "path": str(path)}


def executive_branch(
    context: AgentContext, purpose: str, *, recovery_candidate: bool = False
) -> dict[str, Any]:
    role = require_canonical(context, "CEO", "CTO")
    from .orchestrator import branch_executive

    return branch_executive(
        context.workspace, role, purpose, candidate=recovery_candidate
    )


def executive_focus(context: AgentContext, role: str) -> dict[str, Any]:
    require_canonical(context, "CEO", "CTO")
    from .orchestrator import focus_executive

    return focus_executive(context.workspace, role)


def succession_request(context: AgentContext, role: str) -> dict[str, Any]:
    if context.authority_kind != "candidate":
        raise PermissionError("only a recovery candidate can request succession")
    if not context.logical_id.startswith(role.upper() + ":candidate:"):
        raise PermissionError(f"this candidate is not a {role.upper()} recovery branch")
    phrase = context.workspace.runtime.request_succession(role, context.session_id)
    return {
        "confirmation_phrase": phrase,
        "instruction": "Ask the human Board to type this exact phrase in this candidate chat within 10 minutes.",
    }


def owner_watch(context: AgentContext, project_id: str) -> dict[str, str]:
    require_canonical(context, "CEO", "CTO")
    from .orchestrator import watch_owner

    return watch_owner(context.workspace, project_id)
