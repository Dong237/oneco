"""Application services that promote runtime events into durable project records."""

from __future__ import annotations

import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import AuthorizationError, ConflictError, OneCoError, ProjectError
from .models import (
    ActionRequest,
    BetRecord,
    BetRevisionManifest,
    BlockerRecord,
    BlockerResolutionRecord,
    BoardDecision,
    BoardDecisionKind,
    Checkpoint,
    Instruction,
    InstructionAction,
    ProjectState,
    PromotionRecord,
    ReleaseRecord,
    ReleaseState,
    SignalRecord,
    SignalStatus,
)
from .records import (
    commit_exact,
    committed_file_matches_head,
    composite_digest,
    file_digest,
    latest_committed_launch_record,
    load_committed_json,
    operation_lock,
)
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


def reconcile_owner_work(workspace: Workspace, project_id: str) -> list[str]:
    """Retire queue rows whose requested durable outcome already exists."""
    owner = f"{project_id}:owner"
    target = workspace.project_path(project_id)
    checkpoints = [
        durable
        for path in sorted((target / "checkpoints").glob("*.json"))
        if (durable := load_committed_json(target, path)) is not None
    ]
    instructions = {
        str(item["instruction_id"]): item
        for path in sorted((target / "instructions").glob("*.json"))
        if (item := load_committed_json(target, path)) is not None
        and item.get("instruction_id")
    }
    reviews = [
        durable
        for path in sorted(
            (workspace.root / "operations" / project_id / "reviews").glob("*-cto.json")
        )
        if (durable := load_committed_json(workspace.root, path)) is not None
    ]
    candidate_rows = [
        (path, ReleaseRecord.model_validate(load_json(path)))
        for path in (
            workspace.root / "operations" / project_id / "releases"
        ).glob("rel_*/candidate.json")
        if committed_file_matches_head(workspace.root, path)
    ]
    candidates = [path for path, _ in candidate_rows]
    review_by_id = {
        str(item.get("review_id")): item for item in reviews if item.get("review_id")
    }
    with workspace.runtime.connect() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """SELECT w.id AS work_id, m.* FROM work_queue w
                LEFT JOIN messages m ON m.id=w.message_id
                WHERE w.recipient=? AND w.status<>'done' ORDER BY w.sequence""",
                (owner,),
            )
        ]
    resolved: list[str] = []
    for row in rows:
        kind = str(row.get("kind") or "")
        object_id = str(row.get("object_id") or "")
        created_at = str(row.get("created_at") or "")
        satisfied = False
        if kind == "production_release":
            result_path = (
                workspace.root
                / "operations"
                / project_id
                / "releases"
                / object_id
                / "result.json"
            )
            satisfied = committed_file_matches_head(workspace.root, result_path)
        elif kind == "release_candidate":
            satisfied = any(path.parent.name == object_id for path in candidates)
        elif kind == "instruction":
            instruction_id = object_id or str(row.get("idempotency_key") or "").removesuffix(
                ":owner"
            )
            instruction = instructions.get(instruction_id)
            satisfied = any(
                item.get("source_instruction_id") == instruction_id
                if item.get("source_instruction_id")
                else bool(
                    instruction
                    and item.get("project_id") == instruction.get("project_id")
                    and item.get("spec_id") == instruction.get("spec_id")
                    and item.get("task_id") == instruction.get("task_id")
                    and (checkpoint_time := _record_datetime(item)) is not None
                    and (instruction_time := _record_datetime(instruction)) is not None
                    and checkpoint_time >= instruction_time
                )
                for item in checkpoints
            )
        elif kind == "technical_correction":
            satisfied = any(
                str(item.get("created_at") or "") >= created_at
                and item.get("verdict") == "on_track"
                for item in reviews
            )
        elif kind == "launch":
            satisfied = bool(checkpoints)
        elif kind == "technical_clearance":
            review = review_by_id.get(object_id)
            review_created_at = _record_datetime(review) if review else None
            satisfied = bool(
                review_created_at
                and any(candidate.created_at >= review_created_at for _, candidate in candidate_rows)
            )
        if satisfied and row.get("id"):
            workspace.runtime.resolve_messages(owner, message_id=str(row["id"]))
            resolved.append(str(row["work_id"]))
    return resolved


def terminal_release_result(workspace: Workspace, project_id: str) -> dict[str, Any] | None:
    """Return a result only when it still describes the current delivery cycle."""
    release_root = workspace.root / "operations" / project_id / "releases"
    candidates = [
        (path, ReleaseRecord.model_validate(load_json(path)))
        for path in release_root.glob("rel_*/candidate.json")
        if committed_file_matches_head(workspace.root, path)
    ]
    if not candidates:
        return None
    candidate_path, candidate = max(
        candidates, key=lambda item: (item[1].created_at, item[1].release_id)
    )
    result_path = candidate_path.parent / "result.json"
    if not committed_file_matches_head(workspace.root, result_path):
        return None
    result = load_json(result_path)
    result_created_at = _record_datetime(result)
    if result_created_at is None:
        return result

    project_root = workspace.project_path(project_id)
    activity_paths = [
        *((project_root / "checkpoints").glob("*.json")),
        *((project_root / "instructions").glob("*.json")),
        *((project_root / "blockers").glob("*.json")),
        *((project_root / "blockers" / "resolutions").glob("*.json")),
        *((workspace.root / "operations" / project_id / "reviews").glob("*.json")),
    ]
    for path in activity_paths:
        repository = project_root if path == project_root or project_root in path.parents else workspace.root
        if not committed_file_matches_head(repository, path):
            continue
        created_at = _record_datetime(load_json(path))
        if created_at is not None and created_at > result_created_at:
            return None
    return result


def _record_datetime(value: dict[str, Any] | None) -> datetime | None:
    if not value or not value.get("created_at"):
        return None
    parsed = datetime.fromisoformat(str(value["created_at"]).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


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
    source_instruction_id: str | None = None,
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
    if commit is None and (workspace.project_path(project_id) / ".git").exists():
        head = subprocess.run(
            ["git", "-C", str(workspace.project_path(project_id)), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        commit = head.stdout.strip() if head.returncode == 0 else None
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
        source_instruction_id=source_instruction_id,
    )
    target = workspace.project_path(project_id)
    filename = f"{checkpoint.created_at.strftime('%Y%m%dT%H%M%SZ')}-{checkpoint.checkpoint_id}.json"
    path = target / "checkpoints" / filename
    atomic_json(path, checkpoint.model_dump(mode="json"))
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
    commit_exact(
        target,
        [path, project_file],
        f"oneco(owner): checkpoint {project_id} {task_id}",
    )
    workspace.runtime.index_checkpoint(
        checkpoint.checkpoint_id,
        project_id,
        path,
        spec_id,
        task_id,
        session_id=session_id,
        epoch=epoch,
    )
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


def record_blocker(
    workspace: Workspace,
    project_id: str,
    *,
    actor: str,
    summary: str,
    evidence: str,
    requested_decision: str,
    session_id: str | None = None,
    epoch: int | None = None,
) -> tuple[BlockerRecord, Path]:
    if actor != f"{project_id}:owner":
        raise AuthorizationError(f"only {project_id}:owner can report this blocker")
    if (session_id is None) != (epoch is None):
        raise AuthorizationError("session_id and epoch must be provided together")
    if session_id is not None and epoch is not None:
        session = workspace.runtime.assert_current(session_id, epoch)
        if session["logical_id"] != actor:
            raise AuthorizationError(f"session {session_id} does not own identity {actor}")
    project = workspace.find_project(project_id)
    if project.state not in {ProjectState.ACTIVE, ProjectState.BLOCKED_EXTERNAL}:
        raise ProjectError(f"{project_id} is {project.state}; an Owner cannot report a blocker")
    blocker = BlockerRecord(
        blocker_id=f"block_{uuid.uuid4().hex}",
        project_id=project_id,
        reported_by=actor,
        recorded_session_id=session_id,
        recorded_epoch=epoch,
        summary=summary,
        evidence=evidence,
        requested_decision=requested_decision,
    )
    target = workspace.project_path(project_id)
    path = (
        target
        / "blockers"
        / (f"{blocker.created_at.strftime('%Y%m%dT%H%M%S%fZ')}-{blocker.blocker_id}.json")
    )
    atomic_json(path, blocker.model_dump(mode="json"))
    commit_exact(target, [path], f"oneco(owner): report blocker {blocker.blocker_id}")
    workspace.runtime.index_record(
        blocker.blocker_id,
        "blocker",
        project_id,
        path,
        file_digest(path),
        blocker.status,
    )
    body = f"Blocker: {summary}\nEvidence: {evidence}\nDecision needed: {requested_decision}"
    for recipient in ("CEO", "CTO"):
        workspace.runtime.send_message(
            actor,
            recipient,
            "blocker",
            body,
            project_id=project_id,
            requires_ack=True,
            idempotency_key=f"{blocker.blocker_id}:{recipient}",
            subject=f"Owner blocker: {project_id}",
            thread_key=project_id,
            object_id=blocker.blocker_id,
            artifact_digest=file_digest(path),
        )
    workspace.runtime.event("blocker.recorded", actor, blocker.model_dump(mode="json"), project_id)
    return blocker, path


def resolve_blocker(
    workspace: Workspace,
    project_id: str,
    blocker_id: str,
    checkpoint_id: str,
    evidence: str,
    *,
    actor: str,
    session_id: str | None = None,
    epoch: int | None = None,
) -> tuple[BlockerResolutionRecord, Path]:
    if actor != f"{project_id}:owner":
        raise AuthorizationError(f"only {project_id}:owner can resolve this blocker")
    if (session_id is None) != (epoch is None):
        raise AuthorizationError("session_id and epoch must be provided together")
    if session_id is not None and epoch is not None:
        session = workspace.runtime.assert_current(session_id, epoch)
        if session["logical_id"] != actor:
            raise AuthorizationError(f"session {session_id} does not own identity {actor}")
    project = workspace.find_project(project_id)
    if project.state not in {ProjectState.ACTIVE, ProjectState.BLOCKED_EXTERNAL}:
        raise ProjectError(f"{project_id} is {project.state}; an Owner cannot resolve a blocker")

    target = workspace.project_path(project_id)
    blocker_paths = list((target / "blockers").glob(f"*-{blocker_id}.json"))
    if len(blocker_paths) != 1:
        raise ConflictError(f"unknown blocker for {project_id}: {blocker_id}")
    blocker_path = blocker_paths[0]
    if not committed_file_matches_head(target, blocker_path):
        raise ConflictError(f"blocker is not committed durable truth: {blocker_id}")
    blocker = BlockerRecord.model_validate(load_json(blocker_path))
    if blocker.project_id != project_id or blocker.reported_by != actor:
        raise AuthorizationError(f"{actor} cannot resolve blocker {blocker_id}")

    checkpoint_paths = list((target / "checkpoints").glob(f"*-{checkpoint_id}.json"))
    if len(checkpoint_paths) != 1:
        raise ConflictError(f"blocker resolution requires a known checkpoint: {checkpoint_id}")
    checkpoint_path = checkpoint_paths[0]
    if not committed_file_matches_head(target, checkpoint_path):
        raise ConflictError(
            f"blocker resolution requires a committed checkpoint: {checkpoint_id}"
        )
    checkpoint = Checkpoint.model_validate(load_json(checkpoint_path))
    if checkpoint.project_id != project_id or checkpoint.created_at < blocker.created_at:
        raise ConflictError("blocker resolution requires a checkpoint recorded after the blocker")
    path = target / "blockers" / "resolutions" / f"{blocker_id}.json"
    became_durable = False
    if path.is_file():
        resolution = BlockerResolutionRecord.model_validate(load_json(path))
        if (
            resolution.project_id != project_id
            or resolution.blocker_id != blocker_id
            or resolution.resolved_by != actor
            or resolution.checkpoint_id != checkpoint_id
            or resolution.evidence != evidence
        ):
            raise ConflictError(f"blocker {blocker_id} already has a different resolution")
    else:
        resolution = BlockerResolutionRecord(
            resolution_id=f"resolve_{uuid.uuid4().hex}",
            blocker_id=blocker_id,
            project_id=project_id,
            resolved_by=actor,
            recorded_session_id=session_id,
            recorded_epoch=epoch,
            checkpoint_id=checkpoint_id,
            evidence=evidence,
        )
        atomic_json(path, resolution.model_dump(mode="json"))
    if not committed_file_matches_head(target, path):
        commit_exact(target, [path], f"oneco(owner): resolve blocker {blocker_id}")
        became_durable = True
    digest = file_digest(path)
    workspace.runtime.index_record(
        blocker.blocker_id,
        "blocker",
        project_id,
        blocker_path,
        file_digest(blocker_path),
        "resolved",
    )
    workspace.runtime.index_record(
        resolution.resolution_id,
        "blocker_resolution",
        blocker_id,
        path,
        digest,
        "resolved",
    )
    workspace.runtime.mark_object_messages_stale(blocker_id, digest)
    body = (
        f"Resolved blocker: {blocker.summary}\n"
        f"Checkpoint: {checkpoint_id}\nEvidence: {evidence}"
    )
    for recipient in ("CEO", "CTO"):
        workspace.runtime.send_message(
            actor,
            recipient,
            "blocker_resolved",
            body,
            project_id=project_id,
            idempotency_key=f"{resolution.resolution_id}:{recipient}",
            subject=f"Owner blocker resolved: {project_id}",
            thread_key=project_id,
            object_id=blocker_id,
            artifact_digest=digest,
        )
    if became_durable:
        workspace.runtime.event(
            "blocker.resolved", actor, resolution.model_dump(mode="json"), project_id
        )
    return resolution, path


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
    commit_exact(
        target,
        [path],
        f"oneco({instruction.sender_authority.lower()}): instruct {instruction.project_id}",
    )
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
        subject=f"{instruction.sender_authority.upper()} instruction: {instruction.project_id}",
        thread_key=instruction.project_id,
        object_id=instruction.instruction_id,
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
    request_path = workspace.project_path(project_id) / "decisions" / f"{action.action_id}.json"
    atomic_json(request_path, {**action.model_dump(mode="json"), "status": "pending"})
    commit_exact(
        workspace.project_path(project_id),
        [request_path],
        f"oneco({requester.lower()}): request action {action.action_id}",
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
    return action


def decide_action(
    workspace: Workspace, action_id: str, approver: str, decision: str, note: str = ""
) -> tuple[str, Path]:
    """Apply an approval and mirror the complete approval trail into durable project truth."""
    with operation_lock(workspace.root, f"action:{action_id}"):
        return _decide_action_unlocked(workspace, action_id, approver, decision, note)


def _decide_action_unlocked(
    workspace: Workspace, action_id: str, approver: str, decision: str, note: str
) -> tuple[str, Path]:
    runtime_record = workspace.runtime.action(action_id)
    workspace.runtime.validate_action_decision(action_id, approver, decision)
    project_id = runtime_record["project_id"]
    project_root = workspace.project_path(project_id)
    path = project_root / "decisions" / f"{action_id}.json"
    durable = load_committed_json(project_root, path)
    if not durable or durable.get("action_id") != action_id:
        raise ProjectError("action request is missing from committed project records")
    legacy_approvals = (
        runtime_record.get("approvals", [])
        if durable.get("status") in {"approved", "rejected"}
        else []
    )
    approvals = {
        item["approver"]: item for item in durable.get("approvals", legacy_approvals)
    }
    # A committed approval survives a later SQLite failure. Restore that
    # coordination state before applying the next executive's decision.
    for item in approvals.values():
        workspace.runtime.decide_action(
            action_id, item["approver"], item["decision"], item.get("note", "")
        )
    approvals[approver.upper()] = {
        "approver": approver.upper(),
        "decision": decision,
        "note": note,
        "created_at": datetime.now(UTC).isoformat(),
    }
    domain = durable["domain"]
    decisions = {role: item["decision"] for role, item in approvals.items()}
    if "reject" in decisions.values():
        status = "rejected"
    elif domain == "cross_domain":
        status = (
            "approved" if decisions.get("CEO") == decisions.get("CTO") == "approve" else "pending"
        )
    else:
        status = "approved"
    durable["status"] = status
    durable["approvals"] = sorted(approvals.values(), key=lambda item: item["approver"])
    atomic_json(path, durable)
    commit_exact(
        workspace.project_path(project_id),
        [path],
        f"oneco({approver.lower()}): decide action {action_id}",
    )
    runtime_status = workspace.runtime.decide_action(action_id, approver, decision, note)
    if runtime_status != status:
        raise ProjectError("durable and runtime action status diverged")
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
    commit_exact(
        workspace.root,
        [path],
        f"oneco({actor.lower()}): record decision {identifier}",
    )
    workspace.runtime.event("decision.recorded", actor.upper(), data, project_id)
    return path


def rebuild_runtime(workspace: Workspace) -> dict[str, int]:
    """Rebuild indexes, queues, and notifications from versioned human-readable truth."""
    workspace.runtime.reset_indexes()
    counts = {
        "signals": 0,
        "bet_revisions": 0,
        "board_decisions": 0,
        "promotions": 0,
        "checkpoints": 0,
        "instructions": 0,
        "actions": 0,
        "reviews": 0,
        "releases": 0,
    }
    bets: dict[str, BetRecord] = {}
    durable_bet_rows = []
    for bet_path in sorted((workspace.root / "bets").glob("BET-*/bet.json")):
        durable_bet = load_committed_json(workspace.root, bet_path)
        if durable_bet is not None:
            durable_bet_rows.append((bet_path, durable_bet))
    linked_signal_ids = {
        signal_id
        for _, durable_bet in durable_bet_rows
        for signal_id in BetRecord.model_validate(durable_bet).signal_ids
    }
    for path in sorted((workspace.root / "signals").glob("SIG-*/signal.json")):
        durable_signal = load_committed_json(workspace.root, path)
        if durable_signal is None:
            continue
        signal = SignalRecord.model_validate(durable_signal)
        workspace.runtime.index_record(
            signal.signal_id,
            "signal",
            signal.signal_id,
            path,
            file_digest(path),
            signal.status,
        )
        if signal.signal_id not in linked_signal_ids and signal.status not in {
            SignalStatus.CLOSED,
            SignalStatus.LINKED,
        }:
            signal_root = path.parent
            message_id = workspace.runtime.send_message(
                "BOARD",
                "CPO",
                "signal_assignment",
                f"Investigate {signal.signal_id}: {signal.title}\n{signal.summary}",
                requires_ack=True,
                idempotency_key=f"signal:{signal.signal_id}:cpo",
                subject=f"Assigned signal {signal.signal_id}",
                thread_key=signal.signal_id,
                object_id=signal.signal_id,
                artifact_digest=composite_digest(
                    signal_root,
                    [signal_root / "SIGNAL.md", signal_root / "sources.manifest.json"],
                ),
            )
            workspace.runtime.enqueue_role_work(
                "CPO",
                "signal_assignment",
                {"signal_id": signal.signal_id, "message_id": message_id},
                object_id=signal.signal_id,
                priority=20,
                idempotency_key=f"signal:{signal.signal_id}:work",
            )
        counts["signals"] += 1
    for path, durable_bet in durable_bet_rows:
        bet = BetRecord.model_validate(durable_bet)
        bets[bet.bet_id] = bet
        for manifest_path in sorted((path.parent / "revisions").glob("R*/packet.manifest.json")):
            if not committed_file_matches_head(workspace.root, manifest_path):
                continue
            manifest = BetRevisionManifest.model_validate(load_json(manifest_path))
            workspace.runtime.index_record(
                f"{manifest.bet_id}:R{manifest.revision:03d}",
                "bet_revision",
                manifest.bet_id,
                manifest_path,
                file_digest(manifest_path),
                manifest.state,
            )
            counts["bet_revisions"] += 1
        decisions: list[BoardDecision] = []
        for decision_path in sorted((path.parent / "decisions").glob("*.json")):
            if not committed_file_matches_head(workspace.root, decision_path):
                continue
            decision = BoardDecision.model_validate(load_json(decision_path))
            decisions.append(decision)
            workspace.runtime.index_record(
                decision.decision_id,
                "board_bet_decision",
                bet.bet_id,
                decision_path,
                file_digest(decision_path),
                decision.kind,
            )
            counts["board_decisions"] += 1
        latest_recorded = decisions[-1] if decisions else None
        revision_root = path.parent / "revisions" / f"R{bet.current_revision:03d}"
        manifest_path = revision_root / "packet.manifest.json"
        manifest = (
            BetRevisionManifest.model_validate(load_json(manifest_path))
            if manifest_path.is_file()
            and committed_file_matches_head(workspace.root, manifest_path)
            else None
        )
        latest = (
            latest_recorded
            if manifest
            and latest_recorded
            and latest_recorded.revision == manifest.revision
            and latest_recorded.packet_digest == manifest.composite_digest
            else None
        )
        if manifest and latest is None:
            workspace.runtime.send_message(
                "CPO",
                "BOARD",
                "bet_ready",
                f"{bet.bet_id} R{bet.current_revision:03d} is ready for Board decision.",
                requires_ack=True,
                idempotency_key=f"bet:{bet.bet_id}:R{bet.current_revision:03d}:ready",
                subject=f"Product Bet ready: {bet.title}",
                thread_key=bet.bet_id,
                object_id=bet.bet_id,
                artifact_digest=manifest.composite_digest,
            )
        elif latest and latest.kind in {BoardDecisionKind.MORE_EVIDENCE, BoardDecisionKind.REOPEN}:
            message_id = workspace.runtime.send_message(
                "BOARD",
                "CPO",
                "bet_decision",
                f"{latest.kind}: {bet.bet_id}\n{latest.evidence_gap or latest.reason}",
                requires_ack=True,
                idempotency_key=f"{latest.decision_id}:CPO",
                subject=f"Board decision: {latest.kind} {bet.bet_id}",
                thread_key=bet.bet_id,
                object_id=bet.bet_id,
                artifact_digest=latest.packet_digest,
            )
            workspace.runtime.enqueue_role_work(
                "CPO",
                "bet_decision",
                {"decision_id": latest.decision_id, "bet_id": bet.bet_id},
                object_id=bet.bet_id,
                priority=10,
                idempotency_key=f"{latest.decision_id}:CPO:work",
            )
            _ = message_id
        elif (
            latest
            and latest.kind == BoardDecisionKind.SELECT
            and not any(item.source_bet_id == bet.bet_id for item in workspace.portfolio().projects)
        ):
            message_id = workspace.runtime.send_message(
                "BOARD",
                "CEO",
                "bet_decision",
                f"select: {bet.bet_id} R{latest.revision:03d}\nReason: {latest.reason}",
                requires_ack=True,
                idempotency_key=f"{latest.decision_id}:CEO",
                subject=f"Board selected: {bet.title}",
                thread_key=bet.bet_id,
                object_id=bet.bet_id,
                artifact_digest=latest.packet_digest,
            )
            workspace.runtime.enqueue_role_work(
                "CEO",
                "bet_decision",
                {"decision_id": latest.decision_id, "bet_id": bet.bet_id},
                object_id=bet.bet_id,
                priority=10,
                idempotency_key=f"{latest.decision_id}:CEO:work",
            )
            _ = message_id
    for path in sorted((workspace.root / "portfolio" / "decisions").glob("prom_*.json")):
        durable_promotion = load_committed_json(workspace.root, path)
        if durable_promotion is None:
            continue
        promotion = PromotionRecord.model_validate(durable_promotion)
        workspace.runtime.index_record(
            promotion.promotion_id,
            "promotion",
            promotion.project_id,
            path,
            file_digest(path),
            "promoted",
        )
        promoted_project = workspace.find_project(promotion.project_id)
        if promoted_project.state in {ProjectState.REGISTERED, ProjectState.SHAPING}:
            message_id = workspace.runtime.send_message(
                "CEO",
                "CTO",
                "technical_shaping_request",
                (
                    f"Shape the minimum sound technical path for {promotion.project_id} "
                    f"from {promotion.bet_id} R{promotion.revision:03d}."
                ),
                project_id=promotion.project_id,
                requires_ack=True,
                idempotency_key=f"{promotion.promotion_id}:cto",
                subject=f"Technical shaping: {promotion.project_id}",
                thread_key=promotion.project_id,
                object_id=promotion.project_id,
                artifact_digest=promotion.packet_digest,
            )
            workspace.runtime.enqueue_role_work(
                "CTO",
                "technical_shaping_request",
                {
                    "promotion_id": promotion.promotion_id,
                    "project_id": promotion.project_id,
                    "message_id": message_id,
                },
                object_id=promotion.project_id,
                priority=10,
                idempotency_key=f"{promotion.promotion_id}:cto:work",
            )
        counts["promotions"] += 1
    for project in workspace.portfolio().projects:
        target = workspace.project_path(project.id)
        endorsement_path = target / "decisions" / "launch-endorsements.json"
        durable_endorsements = load_committed_json(target, endorsement_path)
        if durable_endorsements is not None:
            endorsements = durable_endorsements
            digest = workspace.launch_digest(project.id)
            for role in ("CEO", "CTO"):
                value = endorsements.get(role)
                if isinstance(value, dict) and value.get("digest") == digest:
                    workspace.runtime.record_launch_endorsement(project.id, role, digest)
        if project.state == ProjectState.ACTIVE:
            policy_path = workspace.root / "operations" / "supervision-policy.json"
            policy = load_committed_json(workspace.root, policy_path) or load_json(policy_path)
            workspace.runtime.set_supervision_due(
                project.id,
                next_review_at=(
                    datetime.now(UTC) + timedelta(minutes=int(policy["interval_minutes"]))
                ).isoformat(),
            )
        completed_instruction_ids: set[str] = set()
        checkpoint_paths = [
            path
            for path in sorted((target / "checkpoints").glob("*.json"))
            if committed_file_matches_head(target, path)
        ]
        for path in checkpoint_paths:
            checkpoint = Checkpoint.model_validate(load_committed_json(target, path))
            workspace.runtime.index_checkpoint(
                checkpoint.checkpoint_id, project.id, path, checkpoint.spec_id, checkpoint.task_id
            )
            if checkpoint.source_instruction_id:
                completed_instruction_ids.add(checkpoint.source_instruction_id)
            counts["checkpoints"] += 1
        for path in sorted((target / "instructions").glob("*.json")):
            durable_instruction = load_committed_json(target, path)
            if durable_instruction is None:
                continue
            instruction = Instruction.model_validate(durable_instruction)
            if instruction.instruction_id in completed_instruction_ids:
                counts["instructions"] += 1
                continue
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
        blocker_resolutions: dict[str, tuple[BlockerResolutionRecord, Path]] = {}
        for path in sorted((target / "blockers" / "resolutions").glob("*.json")):
            durable_resolution = load_committed_json(target, path)
            if durable_resolution is None:
                continue
            resolution = BlockerResolutionRecord.model_validate(durable_resolution)
            if resolution.project_id != project.id:
                raise ConflictError(
                    f"blocker resolution {resolution.resolution_id} has the wrong project"
                )
            if resolution.blocker_id in blocker_resolutions:
                raise ConflictError(
                    f"blocker {resolution.blocker_id} has multiple durable resolutions"
                )
            blocker_resolutions[resolution.blocker_id] = (resolution, path)
        for path in sorted((target / "blockers").glob("*.json")):
            durable_blocker = load_committed_json(target, path)
            if durable_blocker is None:
                continue
            blocker = BlockerRecord.model_validate(durable_blocker)
            digest = file_digest(path)
            resolution_entry = blocker_resolutions.pop(blocker.blocker_id, None)
            effective_status = "resolved" if resolution_entry else blocker.status
            workspace.runtime.index_record(
                blocker.blocker_id,
                "blocker",
                project.id,
                path,
                digest,
                effective_status,
            )
            if resolution_entry:
                resolution, resolution_path = resolution_entry
                if resolution.resolved_by != blocker.reported_by:
                    raise ConflictError(
                        f"blocker resolution {resolution.resolution_id} has the wrong Owner"
                    )
                checkpoint_paths = list(
                    (target / "checkpoints").glob(f"*-{resolution.checkpoint_id}.json")
                )
                if len(checkpoint_paths) != 1:
                    raise ConflictError(
                        f"blocker resolution {resolution.resolution_id} references an unknown checkpoint"
                    )
                checkpoint_data = load_committed_json(target, checkpoint_paths[0])
                if checkpoint_data is None:
                    raise ConflictError(
                        f"blocker resolution {resolution.resolution_id} references an uncommitted checkpoint"
                    )
                checkpoint = Checkpoint.model_validate(checkpoint_data)
                if (
                    checkpoint.project_id != project.id
                    or checkpoint.created_at < blocker.created_at
                    or resolution.created_at < checkpoint.created_at
                ):
                    raise ConflictError(
                        f"blocker resolution {resolution.resolution_id} has invalid chronology"
                    )
                resolution_digest = file_digest(resolution_path)
                workspace.runtime.index_record(
                    resolution.resolution_id,
                    "blocker_resolution",
                    blocker.blocker_id,
                    resolution_path,
                    resolution_digest,
                    "resolved",
                )
                body = (
                    f"Resolved blocker: {blocker.summary}\n"
                    f"Checkpoint: {resolution.checkpoint_id}\nEvidence: {resolution.evidence}"
                )
                for recipient in ("CEO", "CTO"):
                    workspace.runtime.send_message(
                        resolution.resolved_by,
                        recipient,
                        "blocker_resolved",
                        body,
                        project_id=project.id,
                        idempotency_key=f"{resolution.resolution_id}:{recipient}",
                        subject=f"Owner blocker resolved: {project.id}",
                        thread_key=project.id,
                        object_id=blocker.blocker_id,
                        artifact_digest=resolution_digest,
                    )
            elif blocker.status == "open":
                body = (
                    f"Blocker: {blocker.summary}\nEvidence: {blocker.evidence}\n"
                    f"Decision needed: {blocker.requested_decision}"
                )
                for recipient in ("CEO", "CTO"):
                    workspace.runtime.send_message(
                        blocker.reported_by,
                        recipient,
                        "blocker",
                        body,
                        project_id=project.id,
                        requires_ack=True,
                        idempotency_key=f"{blocker.blocker_id}:{recipient}",
                        subject=f"Owner blocker: {project.id}",
                        thread_key=project.id,
                        object_id=blocker.blocker_id,
                        artifact_digest=digest,
                    )
        if blocker_resolutions:
            unresolved = ", ".join(sorted(blocker_resolutions))
            raise ConflictError(f"blocker resolutions reference unknown blockers: {unresolved}")
        for path in sorted((target / "decisions").glob("act_*.json")):
            data = load_committed_json(target, path)
            if data is None:
                continue
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
        review_root = workspace.root / "operations" / project.id / "reviews"
        review_rows = [
            (path, load_json(path))
            for path in sorted(review_root.glob("*.json"))
            if committed_file_matches_head(workspace.root, path)
        ]
        completed_review_ids = {
            str(data["review_id"]) for _, data in review_rows if data.get("verdict")
        }
        technical_rows = [(path, data) for path, data in review_rows if data.get("verdict")]
        latest_technical = technical_rows[-1] if technical_rows else None
        project_terminal = terminal_release_result(workspace, project.id)
        candidate_paths = [
            path
            for path in sorted(
                (workspace.root / "operations" / project.id / "releases").glob(
                    "rel_*/candidate.json"
                )
            )
            if committed_file_matches_head(workspace.root, path)
        ]
        for path, data in review_rows:
            record_id = f"{data['review_id']}:{path.stem}"
            workspace.runtime.index_record(
                record_id,
                "technical_review" if "verdict" in data else "review_request",
                project.id,
                path,
                file_digest(path),
                data.get("verdict") or data.get("status"),
            )
            if data.get("next_review_at"):
                workspace.runtime.set_supervision_due(
                    project.id,
                    last_review_at=data.get("created_at"),
                    next_review_at=data["next_review_at"],
                    verdict=data.get("verdict"),
                )
            if (
                not data.get("verdict")
                and data.get("status") == "open"
                and data["review_id"] not in completed_review_ids
                and project_terminal is None
            ):
                message_id = workspace.runtime.send_message(
                    data["requested_by"],
                    "CTO",
                    "review_request",
                    f"{data['reason']}\nEvidence: {data['evidence']}",
                    project_id=project.id,
                    requires_ack=True,
                    idempotency_key=f"{data['review_id']}:cto",
                    subject=f"Gate review: {project.id}",
                    thread_key=project.id,
                    object_id=data["review_id"],
                    artifact_digest=file_digest(path),
                )
                workspace.runtime.enqueue_role_work(
                    "CTO",
                    "gate_review",
                    {
                        "review_id": data["review_id"],
                        "project_id": project.id,
                        "message_id": message_id,
                    },
                    object_id=data["review_id"],
                    priority=5,
                    idempotency_key=f"{data['review_id']}:cto:work",
                )
            counts["reviews"] += 1
        if latest_technical and project_terminal is None:
            path, data = latest_technical
            digest = file_digest(path)
            needs_discussion = data["verdict"] != "on_track"
            message_id = workspace.runtime.send_message(
                "CTO",
                "CEO",
                "technical_review",
                (
                    f"{project.id}: {data['verdict']}\n{data['evidence']}\n"
                    f"Recommended: {data['required_action']}"
                ),
                project_id=project.id,
                requires_ack=needs_discussion,
                idempotency_key=f"{data['review_id']}:ceo",
                subject=f"CTO review: {project.id}",
                thread_key=project.id,
                object_id=data["review_id"],
                artifact_digest=digest,
            )
            if needs_discussion:
                workspace.runtime.enqueue_role_work(
                    "CEO",
                    "review_discussion",
                    {
                        "review_id": data["review_id"],
                        "project_id": project.id,
                        "message_id": message_id,
                    },
                    object_id=data["review_id"],
                    priority=10,
                    idempotency_key=f"{data['review_id']}:ceo:work",
                )
            owner = f"{project.id}:owner"
            if data["verdict"] in {"needs_correction", "blocked"}:
                owner_kind = "technical_correction"
                owner_body = (
                    f"{project.id}: {data['verdict']}\n"
                    f"Affected scope: {data['affected_scope']}\n"
                    f"Evidence: {data['evidence']}\n"
                    f"Required: {data['required_action']}\n"
                    "Continue compatible work outside the affected scope."
                )
                priority = 5 if data["verdict"] == "blocked" else 20
            elif data["verdict"] == "on_track" and not candidate_paths:
                owner_kind = "technical_clearance"
                owner_body = f"{project.id}: technical review passed. {data['required_action']}"
                priority = 20
            else:
                owner_kind = ""
                owner_body = ""
                priority = 20
            if owner_kind:
                owner_message = workspace.runtime.send_message(
                    "CTO",
                    owner,
                    owner_kind,
                    owner_body,
                    project_id=project.id,
                    requires_ack=True,
                    idempotency_key=(
                        f"{data['review_id']}:owner-clearance"
                        if owner_kind == "technical_clearance"
                        else f"{data['review_id']}:owner"
                    ),
                    subject=(
                        f"Technical clearance: {project.id}"
                        if owner_kind == "technical_clearance"
                        else f"Technical correction: {project.id}"
                    ),
                    thread_key=project.id,
                    object_id=data["review_id"],
                    artifact_digest=digest,
                )
                workspace.runtime.enqueue_work(
                    owner, project.id, message_id=owner_message, priority=priority
                )
        release_root = workspace.root / "operations" / project.id / "releases"
        for candidate_path in sorted(release_root.glob("rel_*/candidate.json")):
            if not committed_file_matches_head(workspace.root, candidate_path):
                continue
            release = ReleaseRecord.model_validate(load_json(candidate_path))
            root = candidate_path.parent
            endorsements = {
                "CEO": root / "ceo-endorsement.json",
                "CTO": root / "cto-endorsement.json",
            }
            from .company_services import validate_release_candidate

            try:
                validate_release_candidate(workspace, release)
                current_candidate = True
            except ConflictError:
                current_candidate = False
            state = ReleaseState.CANDIDATE if current_candidate else ReleaseState.STALE
            if current_candidate and all(
                item.is_file()
                and committed_file_matches_head(workspace.root, item)
                and load_json(item).get("release_digest") == release.release_digest
                for item in endorsements.values()
            ):
                state = ReleaseState.READY
            decision_path = root / "board-decision.json"
            if (
                current_candidate
                and decision_path.is_file()
                and committed_file_matches_head(workspace.root, decision_path)
            ):
                decision = BoardDecision.model_validate(load_json(decision_path))
                workspace.runtime.index_record(
                    decision.decision_id,
                    "board_release_decision",
                    project.id,
                    decision_path,
                    file_digest(decision_path),
                    decision.kind,
                )
                counts["board_decisions"] += 1
                state = (
                    ReleaseState.QUEUED
                    if decision.kind == BoardDecisionKind.APPROVE_RELEASE
                    else ReleaseState.REJECTED
                )
            result_path = root / "result.json"
            if result_path.is_file() and committed_file_matches_head(workspace.root, result_path):
                result = load_json(result_path)
                if (
                    result.get("release_id") == release.release_id
                    and result.get("release_digest") == release.release_digest
                ):
                    state = (
                        ReleaseState.RELEASED if result.get("succeeded") else ReleaseState.FAILED
                    )
            workspace.runtime.index_release(
                release.release_id, project.id, release.release_digest, state, candidate_path
            )
            workspace.runtime.index_record(
                release.release_id,
                "release",
                project.id,
                candidate_path,
                file_digest(candidate_path),
                state,
            )
            if state == ReleaseState.CANDIDATE:
                for recipient in ("CEO", "CTO"):
                    endorsement = endorsements[recipient]
                    if (
                        endorsement.is_file()
                        and committed_file_matches_head(workspace.root, endorsement)
                        and load_json(endorsement).get("release_digest") == release.release_digest
                    ):
                        continue
                    message_id = workspace.runtime.send_message(
                        f"{project.id}:owner",
                        recipient,
                        "release_candidate",
                        f"Review release {release.release_id} for commit {release.product_commit}.",
                        project_id=project.id,
                        requires_ack=True,
                        idempotency_key=f"{release.release_id}:{recipient}",
                        subject=f"Release candidate: {project.id}",
                        thread_key=project.id,
                        object_id=release.release_id,
                        artifact_digest=release.release_digest,
                    )
                    workspace.runtime.enqueue_role_work(
                        recipient,
                        "release_candidate",
                        {
                            "release_id": release.release_id,
                            "project_id": project.id,
                            "message_id": message_id,
                        },
                        object_id=release.release_id,
                        priority=10,
                        idempotency_key=f"{release.release_id}:{recipient}:work",
                    )
            elif state == ReleaseState.READY:
                workspace.runtime.send_message(
                    "CEO",
                    "BOARD",
                    "release_ready",
                    f"Release {release.release_id} is ready for Board decision.",
                    project_id=project.id,
                    requires_ack=True,
                    idempotency_key=f"{release.release_id}:board",
                    subject=f"Production decision: {project.id}",
                    thread_key=project.id,
                    object_id=release.release_id,
                    artifact_digest=release.release_digest,
                )
            elif state == ReleaseState.QUEUED:
                message_id = workspace.runtime.send_message(
                    "BOARD",
                    f"{project.id}:owner",
                    "production_release",
                    f"Release approved for exact digest {release.release_digest}. Execute once and record evidence.",
                    project_id=project.id,
                    requires_ack=True,
                    idempotency_key=f"{release.release_id}:owner",
                    subject=f"Production approved: {project.id}",
                    thread_key=project.id,
                    object_id=release.release_id,
                    artifact_digest=release.release_digest,
                )
                workspace.runtime.enqueue_work(
                    f"{project.id}:owner", project.id, message_id=message_id, priority=1
                )
            counts["releases"] += 1
        if (
            project.state == ProjectState.ACTIVE
            and project_terminal is None
            and not candidate_paths
            and not checkpoint_paths
            and not review_rows
            and not any(
                committed_file_matches_head(target, path)
                for path in (target / "instructions").glob("*.json")
            )
        ):
            launch = latest_committed_launch_record(target, target / "decisions")
            if launch is not None:
                launch_digest = str(launch["launch_digest"])
                message_id = workspace.runtime.send_message(
                    str(launch.get("launched_by") or "CEO"),
                    f"{project.id}:owner",
                    "launch",
                    "Launch the approved first vertical Spec. Read the full launch packet, use Spec Kit, and execute continuously.",
                    project_id=project.id,
                    requires_ack=True,
                    idempotency_key=f"launch:{project.id}:{launch_digest}",
                    subject=f"Owner launch: {project.id}",
                    thread_key=project.id,
                    object_id=project.id,
                    artifact_digest=launch_digest,
                )
                workspace.runtime.enqueue_work(
                    f"{project.id}:owner", project.id, message_id=message_id, priority=10
                )
    workspace.runtime.event("runtime.rebuilt", "BOARD", counts)
    for project in workspace.portfolio().projects:
        reconcile_owner_work(workspace, project.id)
    return counts
