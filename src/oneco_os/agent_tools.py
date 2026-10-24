"""Authorized operations shared by MCP and the local desktop."""

from __future__ import annotations

import uuid
from typing import Any

from .authority import (
    AgentContext,
    public_context,
    require_canonical,
    require_capability,
    require_owner,
    require_role,
    validate_message_route,
)
from .company_services import (
    configure_supervision,
    create_bet_draft,
    endorse_release,
    list_bets,
    list_signals,
    promote_selected_bet,
    record_release_candidate,
    record_release_result,
    record_technical_review,
    request_gate_review,
    revise_bet_draft,
    submit_bet,
)
from .errors import ConflictError
from .models import Instruction, InstructionAction
from .services import (
    create_action_request,
    decide_action,
    record_blocker,
    record_checkpoint,
    record_instruction,
    resolve_blocker,
)


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
        "signals": list_signals(workspace),
        "bets": list_bets(workspace),
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
    require_role(context, "CEO")
    raise ConflictError(
        "direct project creation is not available in OneCo v1; promote a Board-selected Bet"
    )


def bet_create(
    context: AgentContext,
    title: str,
    signal_ids: list[str],
    packet: str,
    *,
    variant_of: str | None = None,
    duplicate_of: str | None = None,
) -> dict[str, Any]:
    require_capability(context, "bet.write")
    return create_bet_draft(
        context.workspace,
        title,
        signal_ids,
        packet,
        variant_of=variant_of,
        duplicate_of=duplicate_of,
    ).model_dump(mode="json")


def bet_revise(context: AgentContext, bet_id: str, packet: str) -> dict[str, Any]:
    require_capability(context, "bet.write")
    return revise_bet_draft(context.workspace, bet_id, packet).model_dump(mode="json")


def bet_submit(
    context: AgentContext,
    bet_id: str,
    validation_state: str = "not_run",
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    require_capability(context, "bet.submit")
    return submit_bet(
        context.workspace,
        bet_id,
        validation_state=validation_state,
        evidence=evidence,
        submitted_by=context.logical_id,
    ).model_dump(mode="json")


def project_promote(
    context: AgentContext, bet_id: str, name: str, slug: str | None = None
) -> dict[str, Any]:
    require_capability(context, "project.promote")
    return promote_selected_bet(
        context.workspace, bet_id, name=name, slug=slug
    ).model_dump(mode="json")


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
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    canonical_recipient = validate_message_route(context, recipient, project_id=project_id)
    message_id = context.workspace.runtime.send_message(
        context.logical_id,
        canonical_recipient,
        kind,
        body,
        project_id=project_id,
        requires_ack=requires_ack,
        subject=kind.replace("_", " ").title(),
        thread_key=project_id or f"{context.logical_id}:{canonical_recipient}",
        object_id=project_id,
        sender_session_id=context.session_id,
        sender_epoch=context.epoch,
        idempotency_key=idempotency_key,
    )
    return {"message_id": message_id}


def message_ack(context: AgentContext, message_id: str) -> dict[str, str]:
    context.workspace.runtime.assert_current(context.session_id, context.epoch)
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
    with context.workspace.runtime.connect() as connection:
        work = connection.execute(
            """SELECT work_queue.id FROM work_queue
            JOIN messages ON messages.id=work_queue.message_id
            WHERE messages.idempotency_key=?""",
            (instruction.instruction_id,),
        ).fetchone()
    if work is None:
        raise ConflictError(f"instruction work was not queued: {instruction.instruction_id}")
    work_id = str(work["id"])
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
    source_instruction_id: str | None = None,
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
        source_instruction_id=source_instruction_id,
    )
    return {"checkpoint": checkpoint.model_dump(mode="json"), "path": str(path)}


def gate_review_request(
    context: AgentContext,
    project_id: str,
    reason: str,
    evidence: str,
    checkpoint_id: str | None = None,
    review_id: str | None = None,
) -> dict[str, Any]:
    owner = require_owner(context, project_id)
    return request_gate_review(
        context.workspace,
        project_id,
        requested_by=f"{owner}:owner",
        reason=reason,
        evidence=evidence,
        checkpoint_id=checkpoint_id,
        review_id=review_id,
    ).model_dump(mode="json")


def technical_review_record(
    context: AgentContext,
    project_id: str,
    verdict: str,
    evidence: str,
    required_action: str,
    affected_scope: str,
    checkpoint_id: str | None = None,
    review_id: str | None = None,
) -> dict[str, Any]:
    require_capability(context, "owner.review")
    return record_technical_review(
        context.workspace,
        project_id,
        verdict=verdict,
        evidence=evidence,
        required_action=required_action,
        affected_scope=affected_scope,
        checkpoint_id=checkpoint_id,
        review_id=review_id,
    ).model_dump(mode="json")


def supervision_configure(
    context: AgentContext, interval_minutes: int, reason: str
) -> dict[str, Any]:
    role = require_capability(context, "supervision.configure")
    return configure_supervision(
        context.workspace, interval_minutes, changed_by=role, reason=reason
    ).model_dump(mode="json")


def release_candidate_record(
    context: AgentContext,
    project_id: str,
    product_commit: str,
    acceptance_evidence: list[str],
) -> dict[str, Any]:
    require_owner(context, project_id)
    return record_release_candidate(
        context.workspace,
        project_id,
        product_commit=product_commit,
        acceptance_evidence=acceptance_evidence,
    ).model_dump(mode="json")


def release_endorse(
    context: AgentContext, release_id: str, note: str
) -> dict[str, Any]:
    role = require_role(context, "CEO", "CTO")
    require_capability(
        context,
        "release.endorse_product" if role == "CEO" else "release.endorse_technical",
    )
    path = endorse_release(context.workspace, release_id, role, note)
    return {"release_id": release_id, "role": role, "path": str(path)}


def release_result_record(
    context: AgentContext,
    release_id: str,
    project_id: str,
    succeeded: bool,
    evidence: list[str],
    note: str,
) -> dict[str, Any]:
    require_owner(context, project_id)
    path = record_release_result(
        context.workspace,
        release_id,
        actor=context.logical_id,
        succeeded=succeeded,
        evidence=evidence,
        note=note,
        session_id=context.session_id,
        epoch=context.epoch,
    )
    return {"release_id": release_id, "path": str(path)}


def blocker_record(
    context: AgentContext, project_id: str, summary: str, evidence: str, requested_decision: str
) -> dict[str, Any]:
    require_owner(context, project_id)
    blocker, path = record_blocker(
        context.workspace,
        project_id,
        actor=context.logical_id,
        summary=summary,
        evidence=evidence,
        requested_decision=requested_decision,
        session_id=context.session_id,
        epoch=context.epoch,
    )
    context.workspace.runtime.touch(context.session_id, context.epoch, "blocked")
    return {"blocker": blocker.model_dump(mode="json"), "path": str(path)}


def blocker_resolve(
    context: AgentContext,
    project_id: str,
    blocker_id: str,
    checkpoint_id: str,
    evidence: str,
) -> dict[str, Any]:
    require_owner(context, project_id)
    resolution, path = resolve_blocker(
        context.workspace,
        project_id,
        blocker_id,
        checkpoint_id,
        evidence,
        actor=context.logical_id,
        session_id=context.session_id,
        epoch=context.epoch,
    )
    blocker_ids = {
        path.stem[path.stem.index("block_") :]
        for path in (context.workspace.project_path(project_id) / "blockers").glob("*-block_*.json")
    }
    resolved_ids = {
        path.stem
        for path in (
            context.workspace.project_path(project_id) / "blockers" / "resolutions"
        ).glob("block_*.json")
    }
    context.workspace.runtime.touch(
        context.session_id,
        context.epoch,
        "blocked" if blocker_ids - resolved_ids else "idle",
    )
    return {"resolution": resolution.model_dump(mode="json"), "path": str(path)}


def action_request(
    context: AgentContext,
    project_id: str,
    domain: str,
    summary: str,
    effect: str,
    reversible: bool,
) -> dict[str, Any]:
    context.workspace.runtime.assert_current(context.session_id, context.epoch)
    if context.authority_kind == "owner":
        require_owner(context, project_id)
    elif context.persistent_role not in {"CEO", "CTO"}:
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
    role = require_role(context, "CPO", "CEO", "CTO")
    from .orchestrator import branch_executive

    return branch_executive(
        context.workspace, role, purpose, candidate=recovery_candidate
    )


def executive_focus(context: AgentContext, role: str) -> dict[str, Any]:
    require_role(context, "CPO", "CEO", "CTO")
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
