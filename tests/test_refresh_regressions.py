from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

import oneco_os.company_services as company_services
import oneco_os.orchestrator as orchestrator
import oneco_os.services as services
from oneco_os import bridge, health
from oneco_os.adapters import ghostty
from oneco_os.bridge import BridgeAPI
from oneco_os.cli import app
from oneco_os.company_services import (
    board_decide_bet,
    board_decide_release,
    create_bet_draft,
    endorse_release,
    latest_bet_decision,
    list_bets,
    list_signals,
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
from oneco_os.models import Instruction, ProjectState, ReleaseRecord, TechnicalReview
from oneco_os.records import commit_exact, committed_file_matches_head
from oneco_os.services import (
    create_action_request,
    rebuild_runtime,
    reconcile_owner_work,
    record_blocker,
    record_checkpoint,
    record_instruction,
    resolve_blocker,
    terminal_release_result,
)
from oneco_os.supervisor import supervision_tick
from oneco_os.util import atomic_json
from oneco_os.workspace import Workspace

runner = CliRunner()

PACKET = """# Product Bet Packet

## User and Job
Independent developers need a reliable release gate.

## Alternatives
Manual checklists and hosted project trackers.

## Source provenance and dates
Board interview, observed 2026-09-29.

## Counterevidence and unknowns
Teams may prefer an existing tracker.

## Product opportunity
A local evidence-bound release workflow.

## Business-system hypothesis
Paid local tooling for small AI-native teams.

## Maximum unknown
Whether teams repeat the workflow after one release.

## Validation result
Mixed.

## Recommendation and confidence boundary
Select a narrow trial; confidence remains medium.
"""


def _project(workspace: Workspace, *, active: bool = False):
    project = workspace.register_project("Alpha", Path("alpha"), initialize_git=False)
    if active:
        portfolio = workspace.portfolio()
        portfolio.projects[0] = project.model_copy(update={"state": ProjectState.ACTIVE})
        workspace.save_portfolio(portfolio)
        project = workspace.find_project(project.id)
    return project


def _write_candidate(
    workspace: Workspace,
    project_id: str,
    release_id: str,
    created_at: datetime,
    *,
    result_at: datetime | None = None,
) -> Path:
    release_root = workspace.root / "operations" / project_id / "releases" / release_id
    candidate = ReleaseRecord(
        release_id=release_id,
        project_id=project_id,
        product_commit="test-commit",
        release_digest=("a" if release_id.endswith("0") else "b") * 64,
        acceptance_evidence=["evidence/acceptance.txt"],
        acceptance_evidence_hashes={"evidence/acceptance.txt": "c" * 64},
        created_at=created_at,
        updated_at=created_at,
    )
    candidate_path = release_root / "candidate.json"
    atomic_json(candidate_path, candidate.model_dump(mode="json"))
    if result_at is not None:
        atomic_json(
            release_root / "result.json",
            {
                "release_id": release_id,
                "project_id": project_id,
                "release_digest": candidate.release_digest,
                "succeeded": True,
                "evidence": [{"path": "evidence/acceptance.txt", "sha256": "c" * 64}],
                "note": "Released.",
                "recorded_by": f"{project_id}:owner",
                "recorded_session_id": None,
                "recorded_epoch": None,
                "created_at": result_at.isoformat(),
            },
        )
    return candidate_path


def _selected_bet(workspace: Workspace):
    signal = record_signal(
        workspace,
        "Board idea",
        "A traceable opportunity reported by the Board.",
        [{"kind": "board_idea", "title": "Board note", "uri": "board://refresh"}],
    )
    bet = create_bet_draft(workspace, "Release confidence", [signal.signal_id], PACKET)
    manifest = submit_bet(workspace, bet.bet_id, validation_state="mixed")
    board_decide_bet(
        workspace,
        bet.bet_id,
        manifest.revision,
        manifest.composite_digest,
        "select",
        "Proceed with the narrow trial.",
    )
    return bet, manifest


def _release_candidate_for_retry(workspace: Workspace):
    project = workspace.register_project("Release Retry", Path("release-retry"), initialize_git=True)
    root = workspace.project_path(project.id)
    for relative in (
        "BRIEF.md",
        ".specify/memory/constitution.md",
        "spec-seeds/001-first-vertical.md",
    ):
        path = root / relative
        path.write_text(
            path.read_text(encoding="utf-8")
            .replace("TBD", "Defined")
            .replace("DRAFT —", "READY —"),
            encoding="utf-8",
        )
    commit_exact(
        root,
        [path for path in root.rglob("*") if path.is_file() and ".git" not in path.parts],
        "test: commit launch packet",
    )
    workspace.endorse_launch(project.id, "CEO", "Product ready")
    workspace.endorse_launch(project.id, "CTO", "Technical path ready")
    workspace.activate_from_launch(project.id, actor="CEO")
    evidence = root / "evidence" / "acceptance.txt"
    evidence.write_text("acceptance passed\n", encoding="utf-8")
    product_commit = commit_exact(
        root,
        [path for path in root.rglob("*") if path.is_file() and ".git" not in path.parts],
        "test: verified increment",
    )
    assert product_commit
    release = record_release_candidate(
        workspace,
        project.id,
        product_commit=product_commit,
        acceptance_evidence=["evidence/acceptance.txt"],
    )
    return project, root, release


def test_health_uses_current_product_bet_proposal_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Health", git_init=False)
    checked: list[str] = []

    def skill_health(path: Path, expected_name: str) -> dict[str, str]:
        checked.append(expected_name)
        return {"state": "ok", "diagnostic": str(path)}

    monkeypatch.setattr(health, "_skill_health", skill_health)
    monkeypatch.setattr(health.ghostty, "health", lambda: {"state": "ok", "diagnostic": "ok"})
    monkeypatch.setattr(
        health, "_command_health", lambda command: {"state": "ok", "diagnostic": command}
    )
    monkeypatch.setattr(
        health.trae,
        "find_trae",
        lambda: (_ for _ in ()).throw(WorkspaceError("not needed for this test")),
    )

    rows = {row["name"]: row for row in health.collect(workspace, persist=False)}

    assert "proposing-a-product-bet" in checked
    assert "proposing-a-product-bet" in rows
    assert "validating-a-product-bet" not in checked
    assert "validating-a-product-bet" not in rows


def test_prior_release_result_does_not_terminate_a_newer_candidate(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Release Cycles", git_init=False)
    project = _project(workspace)
    baseline = datetime(2026, 1, 1, tzinfo=UTC)
    _write_candidate(
        workspace,
        project.id,
        "rel_00000000000000000000000000000000",
        baseline,
        result_at=baseline + timedelta(minutes=1),
    )
    _write_candidate(
        workspace,
        project.id,
        "rel_ffffffffffffffffffffffffffffffff",
        baseline + timedelta(days=1),
    )

    assert terminal_release_result(workspace, project.id) is None


def test_new_durable_instruction_reopens_work_after_a_release(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Next Increment", git_init=False)
    project = _project(workspace)
    baseline = datetime(2026, 1, 1, tzinfo=UTC)
    _write_candidate(
        workspace,
        project.id,
        "rel_00000000000000000000000000000000",
        baseline,
        result_at=baseline + timedelta(minutes=1),
    )
    instruction = Instruction(
        instruction_id="inst_next_increment",
        sender_authority="CEO",
        action="CONTINUE",
        project_id=project.id,
        spec_id="SPEC-002",
        task_id="TASK-001",
        observed="The first increment shipped.",
        evidence="The Board requested the accepted follow-up scope.",
        required_action="Build the next accepted increment.",
        do_not="Change the selected user or product intent.",
        created_at=baseline + timedelta(days=1),
    )
    atomic_json(
        workspace.project_path(project.id) / "instructions" / "next-increment.json",
        instruction.model_dump(mode="json"),
    )

    assert terminal_release_result(workspace, project.id) is None


def test_new_owner_queue_after_release_wakes_and_remains_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Continuous Delivery", git_init=False)
    project = _project(workspace, active=True)
    baseline = datetime(2026, 1, 1, tzinfo=UTC)
    candidate_path = _write_candidate(
        workspace,
        project.id,
        "rel_00000000000000000000000000000000",
        baseline,
        result_at=baseline + timedelta(minutes=1),
    )
    candidate = ReleaseRecord.model_validate(json.loads(candidate_path.read_text(encoding="utf-8")))
    workspace.runtime.index_release(
        candidate.release_id,
        project.id,
        candidate.release_digest,
        "released",
        candidate_path,
    )
    owner = f"{project.id}:owner"
    message_id = workspace.runtime.send_message(
        "CEO",
        owner,
        "instruction",
        "Build the next accepted increment.",
        project_id=project.id,
        object_id="inst_next",
        thread_key=project.id,
    )
    workspace.runtime.enqueue_work(owner, project.id, message_id=message_id)
    wakes: list[str] = []
    monkeypatch.setattr(
        "oneco_os.supervisor.wake_owner",
        lambda workspace, project_id, permission="default": wakes.append(project_id),
    )

    supervision_tick(workspace)
    snapshot = BridgeAPI(workspace).snapshot()

    assert wakes == [project.id]
    assert snapshot["projects"][0]["queue"]
    assert snapshot["projects"][0]["owner_effective_state"] != "complete"


def test_technical_clearance_requires_a_candidate_after_its_review(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Clearance Order", git_init=False)
    project = _project(workspace)
    baseline = datetime(2026, 1, 1, tzinfo=UTC)
    _write_candidate(
        workspace,
        project.id,
        "rel_00000000000000000000000000000000",
        baseline,
    )
    review = TechnicalReview(
        review_id="review_next",
        project_id=project.id,
        verdict="on_track",
        evidence="The current increment is technically sound.",
        required_action="Prepare the next release candidate.",
        affected_scope="current increment",
        created_at=baseline + timedelta(days=1),
        next_review_at=baseline + timedelta(days=2),
    )
    review_path = (
        workspace.root
        / "operations"
        / project.id
        / "reviews"
        / "20260102T000000000000Z-review_next-cto.json"
    )
    atomic_json(review_path, review.model_dump(mode="json"))
    owner = f"{project.id}:owner"
    message_id = workspace.runtime.send_message(
        "CTO",
        owner,
        "technical_clearance",
        "Prepare the next release candidate.",
        project_id=project.id,
        object_id=review.review_id,
        thread_key=project.id,
    )
    work_id = workspace.runtime.enqueue_work(owner, project.id, message_id=message_id)

    assert reconcile_owner_work(workspace, project.id) == []
    assert workspace.runtime.work_status(work_id) == "queued"

    _write_candidate(
        workspace,
        project.id,
        "rel_ffffffffffffffffffffffffffffffff",
        baseline + timedelta(days=2),
    )
    assert reconcile_owner_work(workspace, project.id) == [work_id]
    assert workspace.runtime.work_status(work_id) == "done"


def test_unknown_owner_work_is_not_completed_by_old_release_history(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Unknown Work", git_init=False)
    project = _project(workspace)
    baseline = datetime(2026, 1, 1, tzinfo=UTC)
    _write_candidate(
        workspace,
        project.id,
        "rel_00000000000000000000000000000000",
        baseline,
        result_at=baseline + timedelta(minutes=1),
    )
    owner = f"{project.id}:owner"
    message_id = workspace.runtime.send_message(
        "CEO",
        owner,
        "future_work_kind",
        "A future queue contract.",
        project_id=project.id,
        object_id="future-1",
        thread_key=project.id,
    )
    work_id = workspace.runtime.enqueue_work(owner, project.id, message_id=message_id)

    assert reconcile_owner_work(workspace, project.id) == []
    assert workspace.runtime.work_status(work_id) == "queued"


def test_concurrent_promotion_of_one_bet_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Promotion Race", git_init=False)
    bet, _ = _selected_bet(workspace)
    original_register = workspace.register_project

    def delayed_register(*args: object, **kwargs: object):
        time.sleep(0.1)
        return original_register(*args, **kwargs)

    monkeypatch.setattr(workspace, "register_project", delayed_register)
    start = threading.Barrier(2)

    def promote(index: int):
        start.wait(timeout=10)
        return promote_selected_bet(
            workspace,
            bet.bet_id,
            name=f"Concurrent Product {index}",
            slug=f"concurrent-product-{index}",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        promotions = list(pool.map(promote, (1, 2)))

    assert len({promotion.project_id for promotion in promotions}) == 1
    assert len(workspace.portfolio().projects) == 1


def test_sequential_promotion_retry_is_side_effect_free(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Promotion Retry", git_init=True)
    bet, _ = _selected_bet(workspace)

    first = promote_selected_bet(
        workspace,
        bet.bet_id,
        name="Stable Product",
        slug="stable-product",
    )
    project_root = workspace.project_path(first.project_id)
    head_after_first = subprocess.check_output(
        ["git", "-C", str(project_root), "rev-parse", "HEAD"], text=True
    ).strip()
    promotion_path = project_root / "handoffs" / "product-bet" / "promotion.json"
    promotion_bytes = promotion_path.read_bytes()

    second = promote_selected_bet(
        workspace,
        bet.bet_id,
        name="Stable Product",
        slug="stable-product",
    )
    head_after_retry = subprocess.check_output(
        ["git", "-C", str(project_root), "rev-parse", "HEAD"], text=True
    ).strip()

    assert second == first
    assert head_after_retry == head_after_first
    assert promotion_path.read_bytes() == promotion_bytes


def test_active_owner_remains_launchable_after_spec_kit_materializes_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Owner Continuity", git_init=True)
    bet, _ = _selected_bet(workspace)
    promotion = promote_selected_bet(workspace, bet.bet_id, name="Stable Owner")
    project = workspace.find_project(promotion.project_id)
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
        [
            path
            for path in project_root.rglob("*")
            if path.is_file() and ".git" not in path.parts
        ],
        "test: commit launch packet",
    )
    workspace.endorse_launch(project.id, "CEO", "Product ready")
    workspace.endorse_launch(project.id, "CTO", "Technical path ready")
    workspace.activate_from_launch(project.id, actor="CEO")

    spec = project_root / "specs" / "001-first-vertical" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# Active Spec\n\nAccepted Owner work.\n", encoding="utf-8")
    assert workspace.readiness(project.id)[0] is False

    registered = runner.invoke(
        app,
        [
            "session",
            "register",
            f"{project.id}:owner",
            "--root",
            str(workspace.root),
            "--json",
        ],
    )
    wakes: list[str] = []
    watches: list[str] = []
    monkeypatch.setattr(
        "oneco_os.orchestrator.wake_owner",
        lambda current, project_id, permission="default": wakes.append(project_id) or {},
    )
    monkeypatch.setattr(
        "oneco_os.orchestrator.watch_owner",
        lambda current, project_id: watches.append(project_id) or {},
    )
    opened = runner.invoke(
        app,
        ["open", project.id, "--root", str(workspace.root)],
    )

    assert registered.exit_code == 0, registered.output
    assert opened.exit_code == 0, opened.output
    assert wakes == [project.id]
    assert watches == [project.id]


def test_concurrent_same_board_turn_records_one_bet_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Decision Race", git_init=False)
    signal = record_signal(
        workspace,
        "Board idea",
        "A traceable opportunity reported by the Board.",
        [{"kind": "board_idea", "title": "Board note", "uri": "board://decision-race"}],
    )
    bet = create_bet_draft(workspace, "Decision race", [signal.signal_id], PACKET)
    manifest = submit_bet(workspace, bet.bet_id, validation_state="mixed")
    original_latest = company_services.latest_bet_decision

    def delayed_latest(workspace: Workspace, bet_id: str):
        result = original_latest(workspace, bet_id)
        time.sleep(0.1)
        return result

    monkeypatch.setattr(company_services, "latest_bet_decision", delayed_latest)
    start = threading.Barrier(2)

    def decide(_: int):
        start.wait(timeout=10)
        return board_decide_bet(
            workspace,
            bet.bet_id,
            manifest.revision,
            manifest.composite_digest,
            "select",
            "Proceed with the narrow trial.",
            provider_thread_id="board-thread",
            provider_turn_id="board-turn",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(decide, (1, 2)))

    decision_files = list((workspace.root / "bets" / bet.bet_id / "decisions").glob("*.json"))
    assert len({decision.decision_id for decision in decisions}) == 1
    assert len(decision_files) == 1


def test_cockpit_fails_before_native_side_effects_without_an_active_display(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Display Guard", git_init=False)
    native_calls: list[str] = []

    fake_uname = os.uname_result(("Darwin", "qa-host", "25.0.0", "QA", "arm64"))
    monkeypatch.setattr(bridge.os, "uname", lambda: fake_uname)
    monkeypatch.setattr(bridge, "_active_display_count", lambda: 0, raising=False)
    monkeypatch.setattr(
        bridge,
        "collect_health",
        lambda current: native_calls.append("health"),
    )

    with pytest.raises(WorkspaceError, match="No active macOS display"):
        bridge.run_bridge(workspace, launch_executives=False)

    assert native_calls == []


def test_executive_window_without_process_handshake_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Launch Handshake", git_init=False)
    closed: list[str] = []
    commands: list[list[str]] = []

    monkeypatch.setattr(
        ghostty,
        "open_window",
        lambda cwd, command, title=None: (
            commands.append(command)
            or {"window_id": "phantom-window", "terminal_id": "phantom-terminal"}
        ),
    )
    monkeypatch.setattr(ghostty, "close_window", closed.append, raising=False)
    monkeypatch.setattr(
        orchestrator,
        "_wait_for_executive_process",
        lambda *args, **kwargs: False,
        raising=False,
    )

    with pytest.raises(WorkspaceError, match="did not start"):
        orchestrator.open_executive(workspace, "CPO")

    current = workspace.runtime.current_session("CPO")
    assert current is not None
    assert current["lifecycle"] == "lost"
    assert current["launch_nonce"]
    assert "--launch-nonce" in commands[0]
    assert closed == ["phantom-window"]


def test_reconcile_does_not_treat_retained_ghostty_window_as_live_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Dead Process", git_init=False)
    session = workspace.runtime.register_session(
        "CTO",
        workspace.root,
        provider_session_id="thread-exact",
        terminal_transport="ghostty",
        terminal_handle="retained-window",
        pid=999_999_999,
        launch_nonce="verified-launch",
    )
    monkeypatch.setattr(ghostty, "is_alive", lambda handle: handle == "retained-window")

    result = runner.invoke(
        app,
        ["session", "reconcile", "--root", str(workspace.root), "--stale-after", "9999", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert str(session["session_id"]) in json.loads(result.output)["lost"]
    current = workspace.runtime.current_session("CTO")
    assert current is not None
    assert current["lifecycle"] == "lost"
    assert current["provider_session_id"] == "thread-exact"


def test_replaced_launch_attempt_cannot_bind_as_current_process(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Launch Fence", git_init=False)
    session = workspace.runtime.register_session(
        "CPO",
        workspace.root,
        terminal_transport="ghostty",
    )
    session_id = str(session["session_id"])
    epoch = int(session["epoch"])
    workspace.runtime.prepare_process_launch(session_id, epoch, "first-attempt")
    workspace.runtime.prepare_process_launch(session_id, epoch, "replacement-attempt")

    with pytest.raises(StaleSessionError, match="stale launch attempt"):
        workspace.runtime.bind_process(
            session_id,
            epoch,
            12345,
            launch_nonce="first-attempt",
        )

    workspace.runtime.bind_process(
        session_id,
        epoch,
        54321,
        launch_nonce="replacement-attempt",
    )
    assert workspace.runtime.current_session("CPO")["pid"] == 54321


def test_release_candidate_retry_commits_and_restores_notifications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Candidate Retry", git_init=True)
    project, root, baseline = _release_candidate_for_retry(workspace)
    baseline_root = workspace.root / "operations" / project.id / "releases" / baseline.release_id
    for path in baseline_root.rglob("*.json"):
        path.unlink()
    commit_exact(workspace.root, [baseline_root], "test: remove baseline candidate")
    workspace.runtime.reset_indexes()

    original_commit = company_services.commit_exact
    calls = 0

    def fail_once(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkspaceError("simulated Git failure")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(company_services, "commit_exact", fail_once)
    product_commit = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    with pytest.raises(WorkspaceError, match="simulated Git failure"):
        record_release_candidate(
            workspace,
            project.id,
            product_commit=product_commit,
            acceptance_evidence=["evidence/acceptance.txt"],
        )
    candidate_path = next(
        (workspace.root / "operations" / project.id / "releases").glob("rel_*/candidate.json")
    )
    assert not committed_file_matches_head(workspace.root, candidate_path)

    retried = record_release_candidate(
        workspace,
        project.id,
        product_commit=product_commit,
        acceptance_evidence=["evidence/acceptance.txt"],
    )

    assert retried.release_id == candidate_path.parent.name
    assert committed_file_matches_head(workspace.root, candidate_path)
    assert {row["object_id"] for row in workspace.runtime.inbox("CEO")} == {retried.release_id}
    assert {row["object_id"] for row in workspace.runtime.inbox("CTO")} == {retried.release_id}


def test_bet_submission_retry_commits_and_restores_board_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Bet Retry", git_init=True)
    signal = record_signal(
        workspace,
        "Board idea",
        "A traceable retry opportunity.",
        [{"kind": "board_idea", "title": "Board", "uri": "board://bet-retry"}],
    )
    bet = create_bet_draft(workspace, "Retry-safe Bet", [signal.signal_id], PACKET)
    original_commit = company_services.commit_exact
    calls = 0

    def fail_once(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkspaceError("simulated Git failure")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(company_services, "commit_exact", fail_once)
    with pytest.raises(WorkspaceError, match="simulated Git failure"):
        submit_bet(workspace, bet.bet_id, validation_state="mixed")
    manifest_path = (
        workspace.root / "bets" / bet.bet_id / "revisions" / "R001" / "packet.manifest.json"
    )
    assert not committed_file_matches_head(workspace.root, manifest_path)

    manifest = submit_bet(workspace, bet.bet_id, validation_state="mixed")

    assert committed_file_matches_head(workspace.root, manifest_path)
    assert {row["object_id"] for row in workspace.runtime.inbox("BOARD")} == {bet.bet_id}
    with workspace.runtime.connect() as connection:
        indexed = connection.execute(
            "SELECT record_id FROM durable_records WHERE object_id=?", (bet.bet_id,)
        ).fetchall()
    assert {row["record_id"] for row in indexed} >= {f"{bet.bet_id}:R001"}
    assert manifest.composite_digest


def test_board_bet_decision_retry_commits_and_restores_ceo_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Bet Decision Retry", git_init=True)
    signal = record_signal(
        workspace,
        "Board idea",
        "A traceable decision opportunity.",
        [{"kind": "board_idea", "title": "Board", "uri": "board://decision-retry"}],
    )
    bet = create_bet_draft(workspace, "Decision-safe Bet", [signal.signal_id], PACKET)
    manifest = submit_bet(workspace, bet.bet_id, validation_state="mixed")
    original_commit = company_services.commit_exact
    calls = 0

    def fail_once(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkspaceError("simulated Git failure")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(company_services, "commit_exact", fail_once)
    with pytest.raises(WorkspaceError, match="simulated Git failure"):
        board_decide_bet(
            workspace,
            bet.bet_id,
            manifest.revision,
            manifest.composite_digest,
            "select",
            "Proceed with the narrow trial.",
            provider_thread_id="board-thread",
            provider_turn_id="board-turn",
        )
    decision_path = next((workspace.root / "bets" / bet.bet_id / "decisions").glob("*.json"))
    assert not committed_file_matches_head(workspace.root, decision_path)

    decision = board_decide_bet(
        workspace,
        bet.bet_id,
        manifest.revision,
        manifest.composite_digest,
        "select",
        "Proceed with the narrow trial.",
        provider_thread_id="board-thread",
        provider_turn_id="board-turn",
    )

    assert decision.decision_id == json.loads(decision_path.read_text())["decision_id"]
    assert committed_file_matches_head(workspace.root, decision_path)
    assert {row["object_id"] for row in workspace.runtime.inbox("CEO")} == {bet.bet_id}
    assert {row["object_id"] for row in workspace.runtime.queued_role_work("CEO")} == {
        bet.bet_id
    }


def test_gate_review_retry_commits_and_restores_cto_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Gate Review Retry", git_init=True)
    project = _project(workspace, active=True)
    original_commit = company_services.commit_exact
    calls = 0

    def fail_once(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkspaceError("simulated Git failure")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(company_services, "commit_exact", fail_once)
    with pytest.raises(WorkspaceError, match="simulated Git failure"):
        request_gate_review(
            workspace,
            project.id,
            requested_by=f"{project.id}:owner",
            reason="Verify the retry boundary.",
            evidence="The checkpoint is ready for review.",
            review_id="review_retry_gate",
        )
    request_path = (
        workspace.root / "operations" / project.id / "reviews" / "review_retry_gate.json"
    )
    assert not committed_file_matches_head(workspace.root, request_path)

    review = request_gate_review(
        workspace,
        project.id,
        requested_by=f"{project.id}:owner",
        reason="Verify the retry boundary.",
        evidence="The checkpoint is ready for review.",
        review_id="review_retry_gate",
    )

    assert review.review_id == "review_retry_gate"
    assert committed_file_matches_head(workspace.root, request_path)
    assert {row["object_id"] for row in workspace.runtime.inbox("CTO")} == {
        "review_retry_gate"
    }
    assert {row["object_id"] for row in workspace.runtime.queued_role_work("CTO")} == {
        "review_retry_gate"
    }


def test_technical_review_retry_commits_and_restores_messages_and_supervision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Technical Retry", git_init=True)
    project = _project(workspace, active=True)
    original_commit = company_services.commit_exact
    calls = 0

    def fail_once(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkspaceError("simulated Git failure")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(company_services, "commit_exact", fail_once)
    with pytest.raises(WorkspaceError, match="simulated Git failure"):
        record_technical_review(
            workspace,
            project.id,
            verdict="needs_correction",
            evidence="The retry path loses its notification.",
            required_action="Restore durable and runtime state.",
            affected_scope="retry path",
            review_id="review_retry_technical",
        )
    review_path = next(
        (workspace.root / "operations" / project.id / "reviews").glob(
            "*-review_retry_technical-cto.json"
        )
    )
    assert not committed_file_matches_head(workspace.root, review_path)

    review = record_technical_review(
        workspace,
        project.id,
        verdict="needs_correction",
        evidence="The retry path loses its notification.",
        required_action="Restore durable and runtime state.",
        affected_scope="retry path",
        review_id="review_retry_technical",
    )

    assert review.review_id == "review_retry_technical"
    assert committed_file_matches_head(workspace.root, review_path)
    assert {row["object_id"] for row in workspace.runtime.inbox("CEO")} == {
        "review_retry_technical"
    }
    owner = f"{project.id}:owner"
    assert {row["object_id"] for row in workspace.runtime.inbox(owner)} == {
        "review_retry_technical"
    }
    assert workspace.runtime.snapshot(extended=True)["supervision"][0]["last_verdict"] == (
        "needs_correction"
    )


def test_uncommitted_bet_authority_and_reviews_are_invisible_to_reads_and_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Fail Closed", git_init=True)
    signal = record_signal(
        workspace,
        "Board idea",
        "A traceable fail-closed opportunity.",
        [{"kind": "board_idea", "title": "Board", "uri": "board://fail-closed"}],
    )
    bet = create_bet_draft(workspace, "Fail-closed Bet", [signal.signal_id], PACKET)
    original_commit = company_services.commit_exact

    monkeypatch.setattr(
        company_services,
        "commit_exact",
        lambda *args, **kwargs: (_ for _ in ()).throw(WorkspaceError("simulated Git failure")),
    )
    with pytest.raises(WorkspaceError, match="simulated Git failure"):
        submit_bet(workspace, bet.bet_id, validation_state="mixed")
    assert list_bets(workspace)[0]["current_manifest"] is None
    workspace.runtime.reset_indexes()
    counts = rebuild_runtime(workspace)
    assert counts["bet_revisions"] == 0
    assert workspace.runtime.inbox("BOARD") == []

    monkeypatch.setattr(company_services, "commit_exact", original_commit)
    manifest = submit_bet(workspace, bet.bet_id, validation_state="mixed")
    monkeypatch.setattr(
        company_services,
        "commit_exact",
        lambda *args, **kwargs: (_ for _ in ()).throw(WorkspaceError("simulated Git failure")),
    )
    with pytest.raises(WorkspaceError, match="simulated Git failure"):
        board_decide_bet(
            workspace,
            bet.bet_id,
            manifest.revision,
            manifest.composite_digest,
            "select",
            "Proceed.",
        )
    assert latest_bet_decision(workspace, bet.bet_id) is None
    with pytest.raises(AuthorizationError, match="Board-selected"):
        promote_selected_bet(workspace, bet.bet_id, name="Must Not Exist")

    monkeypatch.setattr(company_services, "commit_exact", original_commit)
    project = _project(workspace, active=True)
    monkeypatch.setattr(
        company_services,
        "commit_exact",
        lambda *args, **kwargs: (_ for _ in ()).throw(WorkspaceError("simulated Git failure")),
    )
    with pytest.raises(WorkspaceError, match="simulated Git failure"):
        request_gate_review(
            workspace,
            project.id,
            requested_by=f"{project.id}:owner",
            reason="Invisible until committed.",
            evidence="Interrupted write.",
            review_id="review_uncommitted",
        )
    workspace.runtime.reset_indexes()
    counts = rebuild_runtime(workspace)
    assert counts["reviews"] == 0
    assert BridgeAPI(workspace).snapshot()["reviews"] == []
    assert workspace.runtime.inbox("CTO") == []


def test_uncommitted_owner_and_action_records_are_invisible_to_rebuild_and_cockpit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Owner Fail Closed", git_init=True)
    project, _, _ = _release_candidate_for_retry(workspace)
    monkeypatch.setattr(
        services,
        "commit_exact",
        lambda *args, **kwargs: (_ for _ in ()).throw(WorkspaceError("simulated Git failure")),
    )

    with pytest.raises(WorkspaceError, match="simulated Git failure"):
        record_checkpoint(
            workspace,
            project.id,
            "SPEC-002",
            "TASK-001",
            "Implemented but not durable.",
            ["pytest"],
            "Retry the checkpoint.",
            actor=f"{project.id}:owner",
        )
    with pytest.raises(WorkspaceError, match="simulated Git failure"):
        record_blocker(
            workspace,
            project.id,
            actor=f"{project.id}:owner",
            summary="Uncommitted blocker",
            evidence="Interrupted write.",
            requested_decision="Choose a safe path.",
        )
    instruction = Instruction(
        instruction_id="inst_uncommitted",
        sender_authority="CEO",
        action="CONTINUE",
        project_id=project.id,
        spec_id="SPEC-002",
        task_id="TASK-002",
        observed="A retry is required.",
        evidence="Interrupted write.",
        required_action="Resume only after persistence.",
        do_not="Treat the working tree as durable truth.",
    )
    with pytest.raises(WorkspaceError, match="simulated Git failure"):
        record_instruction(workspace, instruction)
    with pytest.raises(WorkspaceError, match="simulated Git failure"):
        create_action_request(
            workspace,
            project.id,
            f"{project.id}:owner",
            "technical",
            "Uncommitted external action",
            "Would change an external system.",
            False,
        )

    workspace.runtime.reset_indexes()
    counts = rebuild_runtime(workspace)
    snapshot = BridgeAPI(workspace).snapshot()

    assert counts["checkpoints"] == 0
    assert counts["instructions"] == 0
    assert counts["actions"] == 0
    assert snapshot["projects"][0]["latest_checkpoint"] is None
    assert snapshot["blockers"] == []
    assert workspace.runtime.snapshot(extended=True)["pending_actions"] == []
    with workspace.runtime.connect() as connection:
        forbidden = connection.execute(
            "SELECT kind FROM messages WHERE kind IN ('checkpoint','blocker','instruction','action_request')"
        ).fetchall()
    assert forbidden == []


def test_blocker_resolution_retry_commits_and_restores_notifications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Resolution Retry", git_init=True)
    project, project_root, _ = _release_candidate_for_retry(workspace)
    blocker, _ = record_blocker(
        workspace,
        project.id,
        actor=f"{project.id}:owner",
        summary="Retry-safe blocker",
        evidence="A dependency is unavailable.",
        requested_decision="Provide an alternative.",
    )
    checkpoint, _ = record_checkpoint(
        workspace,
        project.id,
        "SPEC-002",
        "TASK-001",
        "Alternative verified.",
        ["pytest: passed"],
        "Resume delivery.",
        actor=f"{project.id}:owner",
    )
    original_commit = services.commit_exact
    calls = 0

    def fail_once(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkspaceError("simulated Git failure")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(services, "commit_exact", fail_once)
    with pytest.raises(WorkspaceError, match="simulated Git failure"):
        resolve_blocker(
            workspace,
            project.id,
            blocker.blocker_id,
            checkpoint.checkpoint_id,
            "The verified checkpoint resolves the blocker.",
            actor=f"{project.id}:owner",
        )
    resolution_path = project_root / "blockers" / "resolutions" / f"{blocker.blocker_id}.json"
    assert not committed_file_matches_head(project_root, resolution_path)

    resolution, _ = resolve_blocker(
        workspace,
        project.id,
        blocker.blocker_id,
        checkpoint.checkpoint_id,
        "The verified checkpoint resolves the blocker.",
        actor=f"{project.id}:owner",
    )

    assert committed_file_matches_head(project_root, resolution_path)
    assert {row["object_id"] for row in workspace.runtime.inbox("CEO")} >= {
        blocker.blocker_id
    }
    assert {row["object_id"] for row in workspace.runtime.inbox("CTO")} >= {
        blocker.blocker_id
    }
    assert resolution.blocker_id == blocker.blocker_id


def test_signal_and_bet_retries_do_not_publish_interrupted_orphans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Create Retry", git_init=True)
    original_commit = company_services.commit_exact
    calls = 0

    def fail_once(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkspaceError("simulated Git failure")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(company_services, "commit_exact", fail_once)
    with pytest.raises(WorkspaceError, match="simulated Git failure"):
        record_signal(
            workspace,
            "Retry signal",
            "Only committed Signals are visible.",
            [{"kind": "board_idea", "title": "Board", "uri": "board://signal-retry"}],
        )
    signal = record_signal(
        workspace,
        "Retry signal",
        "Only committed Signals are visible.",
        [{"kind": "board_idea", "title": "Board", "uri": "board://signal-retry"}],
    )
    assert [item["signal_id"] for item in list_signals(workspace)] == [signal.signal_id]

    calls = 0
    with pytest.raises(WorkspaceError, match="simulated Git failure"):
        create_bet_draft(workspace, "Retry Bet", [signal.signal_id], PACKET)
    bet = create_bet_draft(workspace, "Retry Bet", [signal.signal_id], PACKET)

    assert [item["bet_id"] for item in list_bets(workspace)] == [bet.bet_id]
    assert list_signals(workspace)[0]["linked_bet_ids"] == [bet.bet_id]
    workspace.runtime.reset_indexes()
    counts = rebuild_runtime(workspace)
    assert counts["signals"] == 1
    assert counts["bet_revisions"] == 0


def test_bet_revision_retry_commits_the_same_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Revision Retry", git_init=True)
    signal = record_signal(
        workspace,
        "Board idea",
        "A traceable revision opportunity.",
        [{"kind": "board_idea", "title": "Board", "uri": "board://revision-retry"}],
    )
    bet = create_bet_draft(workspace, "Revision-safe Bet", [signal.signal_id], PACKET)
    manifest = submit_bet(workspace, bet.bet_id, validation_state="mixed")
    board_decide_bet(
        workspace,
        bet.bet_id,
        manifest.revision,
        manifest.composite_digest,
        "more_evidence",
        "Clarify repeat use.",
        evidence_gap="Repeat-use evidence",
    )
    revised_packet = PACKET.replace(
        "Whether teams repeat the workflow after one release.",
        "Whether teams repeat the workflow after two observed releases.",
    )
    original_commit = company_services.commit_exact
    calls = 0

    def fail_once(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkspaceError("simulated Git failure")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(company_services, "commit_exact", fail_once)
    with pytest.raises(WorkspaceError, match="simulated Git failure"):
        revise_bet_draft(workspace, bet.bet_id, revised_packet)
    revision_path = (
        workspace.root / "bets" / bet.bet_id / "revisions" / "R002" / "PRODUCT_BET_PACKET.md"
    )
    assert not committed_file_matches_head(workspace.root, revision_path)

    revised = revise_bet_draft(workspace, bet.bet_id, revised_packet)

    assert revised.current_revision == 2
    assert committed_file_matches_head(workspace.root, revision_path)
    assert committed_file_matches_head(workspace.root, workspace.root / "bets" / bet.bet_id / "bet.json")


def test_uncommitted_release_endorsement_is_not_authoritative_and_retry_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Endorsement Retry", git_init=True)
    project, _, release = _release_candidate_for_retry(workspace)
    original_commit = company_services.commit_exact
    calls = 0

    def fail_once(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkspaceError("simulated Git failure")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(company_services, "commit_exact", fail_once)
    with pytest.raises(WorkspaceError, match="simulated Git failure"):
        endorse_release(workspace, release.release_id, "CEO", "Product ready")
    endorsement = (
        workspace.root
        / "operations"
        / project.id
        / "releases"
        / release.release_id
        / "ceo-endorsement.json"
    )
    assert endorsement.is_file()
    assert not committed_file_matches_head(workspace.root, endorsement)
    with pytest.raises(ConflictError, match="endorsements"):
        board_decide_release(workspace, release.release_id, "approve", "Ship")

    endorse_release(workspace, release.release_id, "CEO", "Product ready")
    endorse_release(workspace, release.release_id, "CTO", "Technical ready")

    assert committed_file_matches_head(workspace.root, endorsement)
    release_state = workspace.runtime.snapshot(extended=True)["releases"][0]["state"]
    assert release_state == "ready"


def test_board_release_decision_retry_commits_and_restores_owner_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Decision Retry", git_init=True)
    project, _, release = _release_candidate_for_retry(workspace)
    endorse_release(workspace, release.release_id, "CEO", "Product ready")
    endorse_release(workspace, release.release_id, "CTO", "Technical ready")
    original_commit = company_services.commit_exact
    calls = 0

    def fail_once(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkspaceError("simulated Git failure")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(company_services, "commit_exact", fail_once)
    with pytest.raises(WorkspaceError, match="simulated Git failure"):
        board_decide_release(workspace, release.release_id, "approve", "Ship")
    decision_path = (
        workspace.root
        / "operations"
        / project.id
        / "releases"
        / release.release_id
        / "board-decision.json"
    )
    assert not committed_file_matches_head(workspace.root, decision_path)

    decision = board_decide_release(workspace, release.release_id, "approve", "Ship")

    assert decision.release_id == release.release_id
    assert committed_file_matches_head(workspace.root, decision_path)
    indexed = workspace.runtime.snapshot(extended=True)["releases"][0]
    assert indexed["state"] == "queued"
    assert len(workspace.runtime.queued_work(f"{project.id}:owner")) == 1


def test_release_result_retry_commits_and_restores_released_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Result Retry", git_init=True)
    project, _, release = _release_candidate_for_retry(workspace)
    endorse_release(workspace, release.release_id, "CEO", "Product ready")
    endorse_release(workspace, release.release_id, "CTO", "Technical ready")
    board_decide_release(workspace, release.release_id, "approve", "Ship")
    original_commit = company_services.commit_exact
    calls = 0

    def fail_once(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkspaceError("simulated Git failure")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(company_services, "commit_exact", fail_once)
    with pytest.raises(WorkspaceError, match="simulated Git failure"):
        record_release_result(
            workspace,
            release.release_id,
            actor=f"{project.id}:owner",
            succeeded=True,
            evidence=["evidence/acceptance.txt"],
            note="Released exactly once.",
        )
    result_path = (
        workspace.root
        / "operations"
        / project.id
        / "releases"
        / release.release_id
        / "result.json"
    )
    assert not committed_file_matches_head(workspace.root, result_path)

    record_release_result(
        workspace,
        release.release_id,
        actor=f"{project.id}:owner",
        succeeded=True,
        evidence=["evidence/acceptance.txt"],
        note="Released exactly once.",
    )

    assert committed_file_matches_head(workspace.root, result_path)
    indexed = workspace.runtime.snapshot(extended=True)["releases"][0]
    assert indexed["state"] == "released"
    assert workspace.runtime.queued_work(f"{project.id}:owner")[0]["status"] == "done"


def test_spec_init_continues_when_oneco_preset_is_already_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Spec Retry", git_init=False)
    project = workspace.register_project("Spec Product", Path("spec-product"), initialize_git=False)
    repository = tmp_path / "installed-oneco"
    stage = repository / "integrations" / "speckit" / "bin" / "stage-project"
    calls: list[list[str]] = []

    monkeypatch.setattr("oneco_os.cli._repository_root", lambda: repository)
    monkeypatch.setattr("oneco_os.cli.shutil.which", lambda command: f"/tools/{command}")

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:3] == ["preset", "add"]:
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr=(
                    "Preset 'oneco-lean' is already installed. "
                    "Use 'specify preset remove oneco-lean' first."
                ),
            )
        assert command[0] == str(stage)
        return subprocess.CompletedProcess(command, 0, stdout='{"ok":true}\n', stderr="")

    monkeypatch.setattr("oneco_os.cli.subprocess.run", run)

    result = runner.invoke(
        app,
        ["spec", "init", project.id, "--root", str(workspace.root)],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    assert calls[1][0] == str(stage)


def test_managed_executive_prefers_launching_oneco_distribution(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = workspace.runtime.register_session("CEO", workspace.root)
    captured: dict[str, object] = {}
    executable = "/private/tmp/oneco-package/venv/bin/python"
    monkeypatch.setenv("PATH", "/Users/example/.local/bin:/usr/bin")
    monkeypatch.setattr(orchestrator.sys, "executable", executable)
    monkeypatch.setattr(orchestrator.trae, "require_model", lambda model: Path("/usr/bin/true"))
    monkeypatch.setattr(
        orchestrator.os,
        "execvpe",
        lambda binary, argv, environment: captured.update(
            {"binary": binary, "argv": argv, "environment": environment}
        ),
    )

    orchestrator.run_executive(
        workspace,
        "CEO",
        str(session["session_id"]),
        int(session["epoch"]),
        model="GPT-5.6-Sol",
        permission="bypass_permissions",
        resume=None,
        fork_from=None,
        advisory_prompt="Stay within the test boundary.",
    )

    environment = captured["environment"]
    assert environment["ONECO_PYTHON"] == executable
    assert environment["PATH"].split(os.pathsep)[0] == str(Path(executable).parent)


def test_legacy_managed_role_prefers_launching_oneco_distribution(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    executable = "/private/tmp/oneco-package/venv/bin/python"
    monkeypatch.setenv("PATH", "/Users/example/.local/bin:/usr/bin")
    monkeypatch.setattr("oneco_os.cli.sys.executable", executable)
    monkeypatch.setattr("oneco_os.cli.trae.require_model", lambda model: Path("/usr/bin/true"))
    monkeypatch.setattr(
        "oneco_os.cli.os.execvpe",
        lambda binary, argv, environment: captured.update(
            {"binary": binary, "argv": argv, "environment": environment}
        ),
    )

    result = runner.invoke(app, ["role-run", "CEO", "--root", str(workspace.root)])

    assert result.exit_code == 0, result.output
    environment = captured["environment"]
    assert environment["ONECO_PYTHON"] == executable
    assert environment["PATH"].split(os.pathsep)[0] == str(Path(executable).parent)


def test_managed_owner_prefers_launching_oneco_distribution(
    active_project: tuple[Workspace, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, project_id, project_root = active_project
    session = workspace.runtime.register_session(f"{project_id}:owner", project_root)
    workspace.runtime.enqueue_work(f"{project_id}:owner", project_id)
    captured: dict[str, object] = {}
    executable = "/private/tmp/oneco-package/venv/bin/python"
    monkeypatch.setenv("PATH", "/Users/example/.local/bin:/usr/bin")
    monkeypatch.setattr(orchestrator.sys, "executable", executable)
    monkeypatch.setattr(orchestrator.trae, "require_model", lambda model: Path("/usr/bin/true"))
    monkeypatch.setattr(orchestrator.trae, "exec_argv", lambda *args, **kwargs: ["true"])

    class Process:
        stdout: list[str] = []

        def wait(self) -> int:
            return 1

    def popen(*args: object, **kwargs: object) -> Process:
        captured["environment"] = kwargs["env"]
        return Process()

    monkeypatch.setattr(orchestrator.subprocess, "Popen", popen)

    assert (
        orchestrator.run_owner_turn(
            workspace,
            project_id,
            str(session["session_id"]),
            int(session["epoch"]),
        )
        == 1
    )

    environment = captured["environment"]
    assert environment["ONECO_PYTHON"] == executable
    assert environment["PATH"].split(os.pathsep)[0] == str(Path(executable).parent)


def test_launch_packet_must_be_committed_and_feature_state_is_digest_bound(
    tmp_path: Path,
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Committed Launch", git_init=True)
    project = workspace.register_project("Committed Product", Path("product"), initialize_git=True)
    project_root = workspace.project_path(project.id)
    commit_exact(
        project_root,
        [path for path in project_root.rglob("*") if path.is_file() and ".git" not in path.parts],
        "test: commit initial scaffold",
    )
    for relative in ("BRIEF.md", ".specify/memory/constitution.md"):
        path = project_root / relative
        path.write_text(
            path.read_text(encoding="utf-8")
            .replace("TBD", "Defined")
            .replace("DRAFT —", "READY —"),
            encoding="utf-8",
        )
    feature = project_root / "specs" / "001-first-vertical" / "spec.md"
    feature.parent.mkdir(parents=True)
    feature.write_text("# Accepted Spec\n\nA committed vertical result.\n", encoding="utf-8")
    feature_state = project_root / ".specify" / "feature.json"
    feature_state.write_text(
        json.dumps(
            {
                "feature_directory": "specs/001-first-vertical",
                "feature_number": "001",
                "feature_slug": "first-vertical",
                "spec_id": "SPEC-001",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    workspace.endorse_launch(project.id, "CEO", "Product ready")
    workspace.endorse_launch(project.id, "CTO", "Technical path ready")

    assert workspace.find_project(project.id).state is ProjectState.SHAPING
    ready, reasons = workspace.readiness(project.id)
    assert ready is False
    assert any("not committed" in reason for reason in reasons)
    with pytest.raises(ProjectError, match="not committed"):
        workspace.activate_from_launch(project.id, actor="CEO")

    commit_exact(
        project_root,
        [
            project_root / "BRIEF.md",
            project_root / ".specify" / "memory" / "constitution.md",
            feature_state,
            feature,
        ],
        "test: commit launch packet",
    )
    assert workspace.readiness(project.id) == (True, [])
    digest = workspace.launch_digest(project.id)

    state = json.loads(feature_state.read_text(encoding="utf-8"))
    state["task_id_pattern"] = "^T[0-9]{3}$"
    feature_state.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    commit_exact(project_root, [feature_state], "test: change active feature state")

    assert workspace.launch_digest(project.id) != digest
    assert workspace.readiness(project.id)[0] is False


def test_rebuild_does_not_restore_stale_digest_bound_launch_endorsements(
    tmp_path: Path,
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Stale Launch", git_init=True)
    project = workspace.register_project("Alpha", Path("alpha"), initialize_git=True)
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
    workspace.endorse_launch(project.id, "CTO", "Technical path ready")
    endorsed_digest = workspace.launch_digest(project.id)

    brief = project_root / "BRIEF.md"
    brief.write_text(brief.read_text(encoding="utf-8") + "\nChanged after endorsement.\n")
    commit_exact(project_root, [brief], "test: change launch packet")
    assert workspace.launch_digest(project.id) != endorsed_digest

    workspace.runtime.reset_indexes()
    rebuild_runtime(workspace)

    ready, reasons = workspace.readiness(project.id)
    assert ready is False
    assert "missing CEO endorsement for current launch packet" in reasons
    assert "missing CTO endorsement for current launch packet" in reasons


def test_signal_markdown_contains_one_observation_section(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Signal Format", git_init=True)

    signal = record_signal(
        workspace,
        "Single observation",
        "One concise traceable observation.",
        [{"kind": "board_idea", "title": "Board", "uri": "board://single-observation"}],
    )

    markdown = (
        workspace.root / "signals" / signal.signal_id / "SIGNAL.md"
    ).read_text(encoding="utf-8")
    assert markdown.count("## Observation") == 1
    assert markdown.count("One concise traceable observation.") == 1
