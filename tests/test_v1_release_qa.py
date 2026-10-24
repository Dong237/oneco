from __future__ import annotations

import inspect
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaValidationError

from oneco_os import agent_tools, health, records
from oneco_os.authority import require_capability, resolve_agent_context
from oneco_os.bridge import BridgeAPI, run_bridge
from oneco_os.company_services import (
    board_decide_bet,
    board_decide_release,
    create_bet_draft,
    endorse_release,
    promote_selected_bet,
    record_release_candidate,
    record_release_result,
    record_signal,
    record_technical_review,
    request_gate_review,
    revise_bet_draft,
    submit_bet,
)
from oneco_os.errors import (
    AuthorizationError,
    ConflictError,
    ProjectError,
    StaleSessionError,
    WorkspaceError,
)
from oneco_os.models import AuthorityKind, IdentityKind, Instruction
from oneco_os.orchestrator import run_owner_turn
from oneco_os.records import commit_exact
from oneco_os.runtime import Runtime
from oneco_os.services import (
    create_action_request,
    decide_action,
    rebuild_runtime,
    record_checkpoint,
    record_instruction,
)
from oneco_os.supervisor import supervision_tick
from oneco_os.workspace import Workspace

PACKET = """# Product Bet Packet

## User and Job
Independent developers need a reliable release gate.

## Alternatives
Manual checklists and hosted project trackers.

## Source provenance and dates
Board interview, observed 2026-09-28.

## Counterevidence and unknowns
Teams may prefer an existing tracker.

## Product opportunity
A local evidence-bound release workflow.

## Business-system hypothesis
Paid local tooling for small AI-native teams.

## Maximum unknown
Whether teams repeat the workflow after one release.

## Validation result
Not run.

## Recommendation and confidence boundary
Investigate further; confidence is medium until repeat use is observed.
"""


@pytest.fixture
def runtime(tmp_path: Path) -> Runtime:
    value = Runtime(tmp_path / "runtime.sqlite")
    value.initialize()
    return value


def _company(tmp_path: Path) -> Workspace:
    return Workspace.initialize(tmp_path / "company", "QA Company", git_init=True)


def _submitted_bet(workspace: Workspace, *, evidence: list[dict[str, object]] | None = None):
    signal = record_signal(
        workspace,
        "Board signal",
        "A traceable workflow problem.",
        [{"kind": "board_idea", "title": "Board note", "uri": "board://qa"}],
    )
    bet = create_bet_draft(workspace, "Release confidence", [signal.signal_id], PACKET)
    return signal, bet, submit_bet(workspace, bet.bet_id, evidence=evidence)


def _active_release_project(workspace: Workspace, name: str = "Alpha"):
    project = workspace.register_project(name, Path(name.lower()), initialize_git=True)
    project_root = workspace.project_path(project.id)
    for relative in (
        "BRIEF.md",
        ".specify/memory/constitution.md",
        "spec-seeds/001-first-vertical.md",
    ):
        path = project_root / relative
        path.write_text(
            path.read_text(encoding="utf-8")
            .replace("TBD", "Defined")
            .replace("DRAFT —", "READY —"),
            encoding="utf-8",
        )
    commit_exact(
        project_root,
        [path for path in project_root.rglob("*") if path.is_file() and ".git" not in path.parts],
        "test: commit launch packet",
    )
    workspace.endorse_launch(project.id, "CEO", "Product ready")
    workspace.endorse_launch(project.id, "CTO", "Technically ready")
    workspace.activate_from_launch(project.id, actor="CEO")
    evidence = project_root / "evidence" / "acceptance.txt"
    evidence.write_text("acceptance passed\n", encoding="utf-8")
    product_commit = commit_exact(
        project_root,
        [path for path in project_root.rglob("*") if path.is_file() and ".git" not in path.parts],
        "test: verified increment",
    )
    assert product_commit is not None
    return project, project_root, product_commit


def _remove_runtime(workspace: Workspace) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(workspace.runtime.database) + suffix).unlink(missing_ok=True)
    workspace.runtime.initialize()


def _context(workspace: Workspace, logical_id: str, **session_values: object):
    session = workspace.runtime.register_session(logical_id, workspace.root, **session_values)
    context = resolve_agent_context(
        {"ONECO_ROOT": str(workspace.root), "ONECO_SESSION_ID": str(session["session_id"])}
    )
    return session, context


def test_persistent_roles_require_compatible_authority_kind(workspace: Workspace) -> None:
    _, context = _context(
        workspace,
        "CEO",
        authority_kind=AuthorityKind.ADVISORY,
        identity_kind=IdentityKind.PERSISTENT_ROLE,
    )

    with pytest.raises(AuthorizationError):
        require_capability(context, "project.promote")


def test_owner_messages_require_own_project_scope(workspace: Workspace) -> None:
    project = workspace.register_project("Alpha", Path("alpha"), initialize_git=False)
    _, context = _context(
        workspace,
        f"{project.id}:owner",
        authority_kind=AuthorityKind.OWNER,
        identity_kind=IdentityKind.OWNER,
    )

    with pytest.raises(AuthorizationError, match="project scope"):
        agent_tools.message_send(context, "CEO", "product_question", "Which outcome?", None)
    assert workspace.runtime.inbox("CEO") == []


def test_role_message_routes_are_canonical_and_closed(workspace: Workspace) -> None:
    _, context = _context(
        workspace,
        "CPO",
        authority_kind=AuthorityKind.ADVISORY,
        identity_kind=IdentityKind.PERSISTENT_ROLE,
    )

    result = agent_tools.message_send(context, "ceo", "advice", "Evidence changed")
    assert workspace.runtime.inbox("CEO")[0]["id"] == result["message_id"]
    with pytest.raises(AuthorizationError, match="message route"):
        agent_tools.message_send(context, "CFO", "advice", "No such role")


def test_public_message_tool_exposes_retry_key() -> None:
    from oneco_os.mcp_server import oneco_message_send

    assert "idempotency_key" in inspect.signature(oneco_message_send).parameters


def test_message_idempotency_rejects_conflicting_payload(runtime: Runtime) -> None:
    runtime.send_message("CEO", "CTO", "discussion", "First", idempotency_key="stable-message")

    with pytest.raises(ConflictError, match="idempotency"):
        runtime.send_message(
            "CEO", "CTO", "discussion", "Changed", idempotency_key="stable-message"
        )
    assert [row["body"] for row in runtime.inbox("CTO")] == ["First"]


@pytest.mark.parametrize("changed", ["packet", "evidence"])
def test_board_rejects_changed_submitted_bet_artifacts(tmp_path: Path, changed: str) -> None:
    workspace = _company(tmp_path)
    evidence_path = workspace.root / "research" / "interview.md"
    evidence_path.parent.mkdir()
    evidence_path.write_text("original evidence\n", encoding="utf-8")
    evidence = [
        {
            "evidence_id": "E-1",
            "title": "Interview",
            "path": "research/interview.md",
            "observed_at": datetime.now(UTC).isoformat(),
        }
    ]
    _, bet, manifest = _submitted_bet(workspace, evidence=evidence)
    target = (
        workspace.root / "bets" / bet.bet_id / "revisions" / "R001" / "PRODUCT_BET_PACKET.md"
        if changed == "packet"
        else evidence_path
    )
    target.write_text(target.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")

    with pytest.raises(ConflictError, match="changed|digest|stale"):
        board_decide_bet(
            workspace,
            bet.bet_id,
            manifest.revision,
            manifest.composite_digest,
            "select",
            "Proceed only with exact evidence.",
        )
    assert list((workspace.root / "bets" / bet.bet_id / "decisions").glob("*.json")) == []


def test_promotion_revalidates_selected_bet_bytes(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    _, bet, manifest = _submitted_bet(workspace)
    board_decide_bet(
        workspace,
        bet.bet_id,
        manifest.revision,
        manifest.composite_digest,
        "select",
        "Proceed.",
    )
    packet = workspace.root / "bets" / bet.bet_id / "revisions" / "R001" / "PRODUCT_BET_PACKET.md"
    packet.write_text(packet.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")

    with pytest.raises(ConflictError, match="changed|digest|stale"):
        promote_selected_bet(workspace, bet.bet_id, name="Must Not Exist")
    assert workspace.portfolio().projects == []


def test_more_evidence_authorizes_exactly_one_new_revision(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    _, bet, manifest = _submitted_bet(workspace)
    board_decide_bet(
        workspace,
        bet.bet_id,
        manifest.revision,
        manifest.composite_digest,
        "more_evidence",
        "Need repeat usage.",
        evidence_gap="Repeat usage after first success.",
    )
    revise_bet_draft(workspace, bet.bet_id, PACKET + "\nFirst authorized revision.\n")

    with pytest.raises(ConflictError, match="Board|decision"):
        revise_bet_draft(workspace, bet.bet_id, PACKET + "\nUnauthorized extra revision.\n")


def test_submit_retry_must_match_original_validation_and_evidence(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    _, bet, first = _submitted_bet(workspace)

    with pytest.raises(ConflictError, match="different|submitted"):
        submit_bet(workspace, bet.bet_id, validation_state="failed")
    assert first.validation_state == "not_run"


def test_product_bet_packet_requires_named_markdown_sections(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    signal = record_signal(
        workspace,
        "Signal",
        "Observed.",
        [{"kind": "board_idea", "title": "Board", "uri": "board://sections"}],
    )
    words_without_sections = (
        "# Product Bet Packet\n\n"
        "A user job has alternatives, counterevidence, unknown opportunity, business, "
        "recommendation and confidence. Source provenance date, maximum unknown, validation result.\n"
    )
    bet = create_bet_draft(workspace, "Incomplete", [signal.signal_id], words_without_sections)

    with pytest.raises(ConflictError, match="sections"):
        submit_bet(workspace, bet.bet_id)


def test_bet_creation_links_source_signal_durably(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    signal = record_signal(
        workspace,
        "Signal",
        "Observed.",
        [{"kind": "board_idea", "title": "Board", "uri": "board://link"}],
    )
    bet = create_bet_draft(workspace, "Linked Bet", [signal.signal_id], PACKET)

    data = json.loads(
        (workspace.root / "signals" / signal.signal_id / "signal.json").read_text(encoding="utf-8")
    )
    assert data["status"] == "linked"
    assert data["linked_bet_ids"] == [bet.bet_id]


def test_project_record_slug_matches_custom_path(workspace: Workspace) -> None:
    project = workspace.register_project(
        "Readable Product Name", Path("custom-product-slug"), initialize_git=False
    )

    assert project.slug == "custom-product-slug"


def test_technical_review_validates_project_and_notifies_scoped_owner(
    active_project: tuple[Workspace, str, Path],
) -> None:
    workspace, project_id, _ = active_project

    with pytest.raises(ProjectError, match="unknown project"):
        record_technical_review(
            workspace,
            "PROJ-999",
            verdict="blocked",
            evidence="Unsafe migration",
            required_action="Stop the migration",
            affected_scope="database migration",
        )

    review = record_technical_review(
        workspace,
        project_id,
        verdict="needs_correction",
        evidence="The retry path loses state.",
        required_action="Fix retry persistence.",
        affected_scope="retry path only",
    )
    owner = f"{project_id}:owner"
    messages = workspace.runtime.inbox(owner)
    assert len(messages) == 1
    assert messages[0]["kind"] == "technical_correction"
    assert messages[0]["object_id"] == review.review_id
    assert len(workspace.runtime.queued_work(owner)) == 1


def test_action_authorization_precedes_durable_mutation(
    active_project: tuple[Workspace, str, Path],
) -> None:
    workspace, project_id, project_root = active_project
    action = create_action_request(
        workspace,
        project_id,
        f"{project_id}:owner",
        "technical",
        "Rotate a key",
        "Changes infrastructure",
        True,
    )
    path = project_root / "decisions" / f"{action.action_id}.json"
    before = path.read_bytes()
    head = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()

    with pytest.raises(AuthorizationError, match="CEO cannot decide"):
        decide_action(workspace, action.action_id, "CEO", "approve", "Not my domain")
    assert path.read_bytes() == before
    assert (
        subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        == head
    )


def test_owner_instruction_returns_exact_existing_work_item(
    active_project: tuple[Workspace, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, project_id, _ = active_project
    _, context = _context(workspace, "CTO")
    original = agent_tools.record_instruction
    monkeypatch.setattr("oneco_os.orchestrator.wake_owner", lambda *_args, **_kwargs: {})

    def record_then_interleave(workspace: Workspace, instruction: Instruction) -> Path:
        path = original(workspace, instruction)
        workspace.runtime.send_message(
            "SYSTEM",
            f"{project_id}:owner",
            "noise",
            "Interleaved message",
            project_id=project_id,
        )
        return path

    monkeypatch.setattr(agent_tools, "record_instruction", record_then_interleave)
    result = agent_tools.owner_instruct(
        context,
        project_id,
        "HARDEN",
        "SPEC-001",
        "TASK-001",
        "Retry is unsafe",
        "Observed lost state",
        "Persist retry state",
        "Do not widen scope",
    )
    with workspace.runtime.connect() as connection:
        message = connection.execute(
            "SELECT id FROM messages WHERE idempotency_key=?", (result["instruction_id"],)
        ).fetchone()
        work = connection.execute(
            "SELECT message_id FROM work_queue WHERE id=?", (result["work_id"],)
        ).fetchone()
    assert work["message_id"] == message["id"]
    assert len(workspace.runtime.queued_work(f"{project_id}:owner")) == 1


def test_owner_turn_requires_a_durable_outcome(
    active_project: tuple[Workspace, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, project_id, project_root = active_project
    session = workspace.runtime.register_session(
        f"{project_id}:owner", project_root, authority_kind=AuthorityKind.OWNER
    )
    work_id = workspace.runtime.enqueue_work(f"{project_id}:owner", project_id)
    monkeypatch.setattr("oneco_os.orchestrator.trae.require_model", lambda model: Path("/bin/true"))
    monkeypatch.setattr("oneco_os.orchestrator.trae.exec_argv", lambda *args, **kwargs: ["true"])

    class Process:
        stdout: list[str] = []

        def wait(self) -> int:
            return 0

    monkeypatch.setattr("oneco_os.orchestrator.subprocess.Popen", lambda *args, **kwargs: Process())

    run_owner_turn(workspace, project_id, str(session["session_id"]), int(session["epoch"]))

    item = next(row for row in workspace.runtime.queued_work() if row["id"] == work_id)
    assert item["status"] == "queued"
    assert item["attempt_count"] == 1
    assert "durable outcome" in item["error"]


def test_owner_blocker_survives_runtime_rebuild(
    active_project: tuple[Workspace, str, Path],
) -> None:
    workspace, project_id, project_root = active_project
    _, context = _context(
        workspace,
        f"{project_id}:owner",
        authority_kind=AuthorityKind.OWNER,
        identity_kind=IdentityKind.OWNER,
    )
    result = agent_tools.blocker_record(
        context,
        project_id,
        "Unsafe schema change",
        "Migration cannot roll back",
        "Choose a rollback strategy",
    )
    blocker_path = Path(result["path"])
    assert blocker_path.is_file()
    assert project_root in blocker_path.parents

    _remove_runtime(workspace)
    rebuild_runtime(workspace)

    snapshot = BridgeAPI(workspace).snapshot()
    assert snapshot["projects"][0]["blockers"][0]["summary"] == "Unsafe schema change"
    assert {row["kind"] for row in workspace.runtime.inbox("CEO")} >= {"blocker"}
    assert {row["kind"] for row in workspace.runtime.inbox("CTO")} >= {"blocker"}


def test_owner_resolves_blocker_with_checkpoint_and_rebuild_keeps_it_resolved(
    active_project: tuple[Workspace, str, Path],
) -> None:
    workspace, project_id, _ = active_project
    _, context = _context(
        workspace,
        f"{project_id}:owner",
        authority_kind=AuthorityKind.OWNER,
        identity_kind=IdentityKind.OWNER,
    )
    opened = agent_tools.blocker_record(
        context,
        project_id,
        "Unsafe schema change",
        "Migration cannot roll back",
        "Choose a rollback strategy",
    )
    checkpoint = agent_tools.checkpoint_record(
        context,
        project_id,
        "SPEC-001",
        "TASK-001",
        "Rollback-safe migration verified",
        ["pytest tests/test_migration.py: passed", "manual rollback: passed"],
        "TASK-002",
    )["checkpoint"]

    resolved = agent_tools.blocker_resolve(
        context,
        project_id,
        opened["blocker"]["blocker_id"],
        checkpoint["checkpoint_id"],
        "The recovery checkpoint proves forward and rollback paths.",
    )
    assert Path(resolved["path"]).is_file()

    _remove_runtime(workspace)
    rebuild_runtime(workspace)

    snapshot = BridgeAPI(workspace).snapshot()
    blocker = snapshot["projects"][0]["blockers"][0]
    assert blocker["status"] == "resolved"
    assert blocker["resolution"]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert not any(
        item["kind"] == "blocker" and item.get("object_id") == blocker["blocker_id"]
        for item in snapshot["attention"]
    )
    for recipient in ("CEO", "CTO"):
        messages = workspace.runtime.inbox(recipient)
        assert not any(
            item["kind"] == "blocker" and item.get("object_id") == blocker["blocker_id"]
            for item in messages
        )
        assert any(
            item["kind"] == "blocker_resolved"
            and item.get("object_id") == blocker["blocker_id"]
            for item in messages
        )


def test_blocker_resolution_requires_current_owner_and_later_checkpoint(
    active_project: tuple[Workspace, str, Path],
) -> None:
    workspace, project_id, project_root = active_project
    _, owner_context = _context(
        workspace,
        f"{project_id}:owner",
        authority_kind=AuthorityKind.OWNER,
        identity_kind=IdentityKind.OWNER,
    )
    checkpoint = agent_tools.checkpoint_record(
        owner_context,
        project_id,
        "SPEC-001",
        "TASK-000",
        "Baseline captured",
        ["baseline check passed"],
        "TASK-001",
    )["checkpoint"]
    opened = agent_tools.blocker_record(
        owner_context,
        project_id,
        "Later blocker",
        "The baseline exposed a safety gap",
        "Choose the correction",
    )["blocker"]

    with pytest.raises(ConflictError, match="after the blocker"):
        agent_tools.blocker_resolve(
            owner_context,
            project_id,
            opened["blocker_id"],
            checkpoint["checkpoint_id"],
            "The earlier baseline is not resolution evidence.",
        )

    _, cpo_context = _context(
        workspace,
        "CPO",
        authority_kind=AuthorityKind.ADVISORY,
        identity_kind=IdentityKind.PERSISTENT_ROLE,
    )
    with pytest.raises(AuthorizationError, match="Owner"):
        agent_tools.blocker_resolve(
            cpo_context,
            project_id,
            opened["blocker_id"],
            checkpoint["checkpoint_id"],
            "CPO cannot close delivery blockers.",
        )

    replacement = workspace.runtime.register_session(
        f"{project_id}:owner",
        project_root,
        authority_kind=AuthorityKind.OWNER,
        identity_kind=IdentityKind.OWNER,
        replace=True,
    )
    assert replacement["session_id"] != owner_context.session_id
    with pytest.raises(StaleSessionError):
        agent_tools.blocker_resolve(
            owner_context,
            project_id,
            opened["blocker_id"],
            checkpoint["checkpoint_id"],
            "A replaced session cannot close it.",
        )


def test_blocker_resolution_retry_is_idempotent_and_other_open_blocker_keeps_owner_blocked(
    active_project: tuple[Workspace, str, Path],
) -> None:
    workspace, project_id, _ = active_project
    session, context = _context(
        workspace,
        f"{project_id}:owner",
        authority_kind=AuthorityKind.OWNER,
        identity_kind=IdentityKind.OWNER,
    )
    first = agent_tools.blocker_record(
        context, project_id, "First blocker", "First failure", "Correct first failure"
    )["blocker"]
    agent_tools.blocker_record(
        context, project_id, "Second blocker", "Second failure", "Correct second failure"
    )
    checkpoint = agent_tools.checkpoint_record(
        context,
        project_id,
        "SPEC-001",
        "TASK-001",
        "First failure corrected",
        ["targeted regression passed"],
        "TASK-002",
    )["checkpoint"]

    first_result = agent_tools.blocker_resolve(
        context,
        project_id,
        first["blocker_id"],
        checkpoint["checkpoint_id"],
        "The first regression now passes.",
    )
    retry_result = agent_tools.blocker_resolve(
        context,
        project_id,
        first["blocker_id"],
        checkpoint["checkpoint_id"],
        "The first regression now passes.",
    )

    assert retry_result == first_result
    current = workspace.runtime.assert_current(str(session["session_id"]), int(session["epoch"]))
    assert current["work_state"] == "blocked"
    with pytest.raises(ConflictError, match="different resolution"):
        agent_tools.blocker_resolve(
            context,
            project_id,
            first["blocker_id"],
            checkpoint["checkpoint_id"],
            "Changed retry evidence must conflict.",
        )


def test_progress_pulse_runs_before_scheduled_review(
    active_project: tuple[Workspace, str, Path],
) -> None:
    workspace, project_id, _ = active_project
    now = datetime.now(UTC)
    workspace.runtime.set_supervision_due(
        project_id, next_review_at=(now + timedelta(hours=3)).isoformat()
    )
    with workspace.runtime.connect() as connection:
        connection.execute(
            "UPDATE supervision SET progress_requested_at=? WHERE project_id=?",
            ((now - timedelta(minutes=16)).isoformat(), project_id),
        )

    supervision_tick(workspace)

    with workspace.runtime.connect() as connection:
        row = connection.execute(
            "SELECT progress_requested_at FROM supervision WHERE project_id=?", (project_id,)
        ).fetchone()
    assert datetime.fromisoformat(row["progress_requested_at"]) > now
    assert {item["kind"] for item in workspace.runtime.inbox(f"{project_id}:owner")} >= {
        "progress_pulse"
    }
    assert workspace.runtime.queued_role_work("CTO") == []


def test_rebuild_restores_pending_gate_review_notification(
    active_project: tuple[Workspace, str, Path],
) -> None:
    workspace, project_id, _ = active_project
    request = request_gate_review(
        workspace,
        project_id,
        requested_by=f"{project_id}:owner",
        reason="Unsafe boundary",
        evidence="Trace shows loss",
    )
    _remove_runtime(workspace)

    rebuild_runtime(workspace)

    assert {row["object_id"] for row in workspace.runtime.inbox("CTO")} >= {request.review_id}
    assert {row["object_id"] for row in workspace.runtime.queued_role_work("CTO")} >= {
        request.review_id
    }


def test_rebuild_restores_promotion_shaping_notification(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    _, bet, manifest = _submitted_bet(workspace)
    board_decide_bet(workspace, bet.bet_id, 1, manifest.composite_digest, "select", "Proceed.")
    promotion = promote_selected_bet(workspace, bet.bet_id, name="Promoted")
    _remove_runtime(workspace)

    rebuild_runtime(workspace)

    assert {row["kind"] for row in workspace.runtime.inbox("CTO")} >= {"technical_shaping_request"}
    assert {row["object_id"] for row in workspace.runtime.queued_role_work("CTO")} >= {
        promotion.project_id
    }


def test_rebuild_restores_release_candidate_notifications(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    project, _, product_commit = _active_release_project(workspace)
    release = record_release_candidate(
        workspace,
        project.id,
        product_commit=product_commit,
        acceptance_evidence=["evidence/acceptance.txt"],
    )
    _remove_runtime(workspace)

    rebuild_runtime(workspace)

    for role in ("CEO", "CTO"):
        assert {row["object_id"] for row in workspace.runtime.inbox(role)} >= {release.release_id}


def test_rebuild_does_not_requeue_completed_instruction(
    active_project: tuple[Workspace, str, Path],
) -> None:
    workspace, project_id, project_root = active_project
    instruction = Instruction(
        instruction_id="inst_completed",
        sender_authority="CTO",
        action="HARDEN",
        project_id=project_id,
        spec_id="SPEC-001",
        task_id="TASK-001",
        observed="Missing retry evidence",
        evidence="Review trace",
        required_action="Add retry evidence",
        do_not="Change product scope",
    )
    record_instruction(workspace, instruction)
    session = workspace.runtime.register_session(f"{project_id}:owner", project_root)
    record_checkpoint(
        workspace,
        project_id,
        "SPEC-001",
        "TASK-001",
        "Instruction completed",
        ["pytest passed"],
        "TASK-002",
        actor=f"{project_id}:owner",
        session_id=str(session["session_id"]),
        epoch=int(session["epoch"]),
        source_instruction_id=instruction.instruction_id,
    )
    _remove_runtime(workspace)

    rebuild_runtime(workspace)

    assert workspace.runtime.queued_work(f"{project_id}:owner") == []


def test_replaced_candidate_cannot_complete_succession(runtime: Runtime, tmp_path: Path) -> None:
    candidate = runtime.register_session(
        "CPO:candidate:recovery",
        tmp_path,
        authority_kind=AuthorityKind.CANDIDATE,
        identity_kind=IdentityKind.CANDIDATE,
    )
    phrase = runtime.request_succession("CPO", str(candidate["session_id"]))
    runtime.register_session(
        "CPO:candidate:recovery",
        tmp_path,
        authority_kind=AuthorityKind.CANDIDATE,
        identity_kind=IdentityKind.CANDIDATE,
        replace=True,
    )

    with pytest.raises(StaleSessionError):
        runtime.confirm_succession(str(candidate["session_id"]), "turn-1", phrase)
    assert runtime.current_session("CPO") is None


def test_release_endorsement_revalidates_acceptance_evidence(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    project, project_root, product_commit = _active_release_project(workspace)
    release = record_release_candidate(
        workspace,
        project.id,
        product_commit=product_commit,
        acceptance_evidence=["evidence/acceptance.txt"],
    )
    (project_root / "evidence" / "acceptance.txt").write_text(
        "changed after candidate\n", encoding="utf-8"
    )

    with pytest.raises(ConflictError, match="changed|digest|stale"):
        endorse_release(workspace, release.release_id, "CEO", "Ready")


def test_release_rejection_has_distinct_state(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    project, _, product_commit = _active_release_project(workspace)
    release = record_release_candidate(
        workspace,
        project.id,
        product_commit=product_commit,
        acceptance_evidence=["evidence/acceptance.txt"],
    )
    endorse_release(workspace, release.release_id, "CEO", "Product ready")
    endorse_release(workspace, release.release_id, "CTO", "Technical ready")

    board_decide_release(workspace, release.release_id, "reject", "Do not ship.")

    state = workspace.runtime.snapshot(extended=True)["releases"][0]["state"]
    assert state == "rejected"
    assert workspace.runtime.queued_work(f"{project.id}:owner") == []


def test_release_candidate_creation_is_retry_idempotent(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    project, _, product_commit = _active_release_project(workspace)

    first = record_release_candidate(
        workspace,
        project.id,
        product_commit=product_commit,
        acceptance_evidence=["evidence/acceptance.txt"],
    )
    second = record_release_candidate(
        workspace,
        project.id,
        product_commit=product_commit,
        acceptance_evidence=["evidence/acceptance.txt"],
    )

    assert second.release_id == first.release_id
    assert len(workspace.runtime.snapshot(extended=True)["releases"]) == 1


def test_release_result_requires_evidence(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    project, _, product_commit = _active_release_project(workspace)
    release = record_release_candidate(
        workspace,
        project.id,
        product_commit=product_commit,
        acceptance_evidence=["evidence/acceptance.txt"],
    )
    endorse_release(workspace, release.release_id, "CEO", "Product ready")
    endorse_release(workspace, release.release_id, "CTO", "Technical ready")
    board_decide_release(workspace, release.release_id, "approve", "Ship.")

    with pytest.raises(ConflictError, match="evidence"):
        record_release_result(
            workspace,
            release.release_id,
            actor=f"{project.id}:owner",
            succeeded=True,
            evidence=[],
            note="Claimed success without proof",
        )


def test_cockpit_snapshot_contains_complete_awareness_views(
    active_project: tuple[Workspace, str, Path],
) -> None:
    workspace, project_id, _ = active_project
    record_technical_review(
        workspace,
        project_id,
        verdict="needs_correction",
        evidence="Retry gap",
        required_action="Fix retry",
        affected_scope="retry path",
    )

    snapshot = BridgeAPI(workspace).snapshot()

    assert {"releases", "messages", "reviews", "blockers"} <= set(snapshot)
    assert snapshot["reviews"][0]["project_id"] == project_id
    assert snapshot["messages"]
    assert snapshot["unread"]


def test_json_schemas_match_domain_evidence_and_decision_rules() -> None:
    root = Path(__file__).resolve().parents[1]
    revision_schema = json.loads(
        (root / "schemas" / "bet-revision.schema.json").read_text(encoding="utf-8")
    )
    decision_schema = json.loads(
        (root / "schemas" / "board-decision.schema.json").read_text(encoding="utf-8")
    )
    invalid_revision = {
        "bet_id": "BET-0001",
        "revision": 1,
        "state": "ready_for_board",
        "validation_state": "not_run",
        "packet_path": "bets/BET-0001/revisions/R001/PRODUCT_BET_PACKET.md",
        "packet_sha256": "a" * 64,
        "evidence": [
            {"evidence_id": "E-1", "title": "No location", "observed_at": "2026-09-28T00:00:00Z"}
        ],
        "composite_digest": "b" * 64,
        "supersedes_revision": None,
        "submitted_by": "CPO",
        "submitted_at": "2026-09-28T00:00:00Z",
    }
    invalid_decision = {
        "decision_id": "dec_1",
        "kind": "more_evidence",
        "bet_id": None,
        "revision": None,
        "packet_digest": None,
        "project_id": None,
        "release_id": None,
        "release_digest": None,
        "reason": "Need more",
        "evidence_gap": None,
        "provider_thread_id": None,
        "provider_turn_id": None,
        "supersedes": None,
        "created_at": "2026-09-28T00:00:00Z",
    }

    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(revision_schema).validate(invalid_revision)
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(decision_schema).validate(invalid_decision)


def test_external_skill_health_checks_declared_identity(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: wrong-skill\n---\n", encoding="utf-8")

    result = health._skill_health(skill, "expected-skill")

    assert result["state"] == "incompatible"
    assert "expected-skill" in result["diagnostic"]


def test_bridge_diagnostic_uses_current_major_version(
    monkeypatch: pytest.MonkeyPatch, workspace: Workspace
) -> None:
    monkeypatch.setattr("oneco_os.bridge.os.uname", lambda: SimpleNamespace(sysname="Linux"))

    with pytest.raises(WorkspaceError, match="v1") as error:
        run_bridge(workspace)
    assert "v0.2" not in str(error.value)


def test_commit_exact_retries_after_head_compare_and_swap_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath", "/dev/null"], check=True
    )
    baseline = repo / "README.md"
    baseline.write_text("baseline\n", encoding="utf-8")
    commit_exact(repo, [baseline], "initial")
    target = repo / "durable.json"
    target.write_text("{}\n", encoding="utf-8")
    original_git = records._git
    moved_head: list[str] = []

    def race_once(repo_path: Path, *args: str, environment: dict[str, str] | None = None):
        if args and args[0] == "update-ref" and not moved_head:
            result = subprocess.run(
                ["git", "-C", str(repo_path), "commit", "--allow-empty", "-m", "concurrent"],
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "Concurrent",
                    "GIT_AUTHOR_EMAIL": "concurrent@local",
                    "GIT_COMMITTER_NAME": "Concurrent",
                    "GIT_COMMITTER_EMAIL": "concurrent@local",
                },
            )
            assert result.returncode == 0, result.stderr
            moved_head.append(
                subprocess.run(
                    ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
            )
        return original_git(repo_path, *args, environment=environment)

    monkeypatch.setattr(records, "_git", race_once)

    committed = records.commit_exact(repo, [target], "durable record")

    assert committed
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", moved_head[0], committed],
            check=False,
        ).returncode
        == 0
    )
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "show", f"{committed}:durable.json"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == "{}\n"
    )
