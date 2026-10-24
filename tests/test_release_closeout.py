from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_live_qa_regressions import _approved_release, _launched_project
from typer.testing import CliRunner

from oneco_os import cli, orchestrator, supervisor
from oneco_os.bridge import BridgeAPI
from oneco_os.company_services import (
    board_decide_release,
    endorse_release,
    record_release_candidate,
    record_release_result,
)
from oneco_os.errors import ProjectError
from oneco_os.models import Instruction
from oneco_os.records import commit_exact
from oneco_os.services import (
    create_action_request,
    decide_action,
    rebuild_runtime,
    reconcile_owner_work,
    record_checkpoint,
    record_instruction,
)
from oneco_os.util import load_json
from oneco_os.workspace import Workspace


@pytest.mark.parametrize("marker", ["ONECO_IDENTITY", "ONECO_SESSION_ID"])
@pytest.mark.parametrize("command", ["role-run", "internal owner-wake"])
def test_role_agents_cannot_mint_or_wake_canonical_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str, command: str
) -> None:
    monkeypatch.setenv(marker, "CPO")
    opened = Mock(side_effect=AssertionError("agent must be rejected before opening a company"))
    monkeypatch.setattr(cli, "_workspace", opened)
    identity = "CEO" if command == "role-run" else "PROJ-001"

    result = CliRunner().invoke(cli.app, [*command.split(), identity, "--root", str(tmp_path)])

    assert result.exit_code == 2
    assert "human CLI control plane" in result.output
    opened.assert_not_called()


def test_cli_reports_idle_owner_without_starting_a_worker(
    active_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, project_id, _ = active_project
    wake = Mock(return_value={"status": "idle", "session": None})
    monkeypatch.setattr(orchestrator, "wake_owner", wake)

    result = CliRunner().invoke(
        cli.app, ["launch", f"{project_id}:owner", "--root", str(workspace.root)]
    )

    assert result.exit_code == 0
    assert "Owner idle:" in result.output
    assert "Owner ready:" not in result.output


def test_concurrent_cross_domain_approvals_survive_runtime_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Approval race", git_init=True)
    project, root = _launched_project(workspace)
    action = create_action_request(
        workspace, project.id, f"{project.id}:owner", "cross_domain",
        "Publish verified work", "Public product and infrastructure", True,
    )
    original = workspace.runtime.action
    start = threading.Barrier(2)

    def delayed_snapshot(identifier):
        snapshot = original(identifier)
        time.sleep(0.05)
        return snapshot

    monkeypatch.setattr(workspace.runtime, "action", delayed_snapshot)

    def approve(role):
        start.wait(timeout=5)
        return decide_action(workspace, action.action_id, role, "approve")[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(approve, ["CEO", "CTO"]))
    assert sorted(statuses) == ["approved", "pending"]
    monkeypatch.setattr(workspace.runtime, "action", original)
    durable = load_json(root / "decisions" / f"{action.action_id}.json")
    rebuild_runtime(workspace)

    for record in [durable, workspace.runtime.action(action.action_id)]:
        assert record["status"] == "approved"
        assert {item["approver"] for item in record["approvals"]} == {"CEO", "CTO"}


def test_committed_approval_survives_a_failed_sqlite_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Approval recovery", git_init=True)
    project, root = _launched_project(workspace)
    action = create_action_request(
        workspace, project.id, f"{project.id}:owner", "cross_domain",
        "Publish verified work", "Public product and infrastructure", True,
    )
    original = workspace.runtime.decide_action
    monkeypatch.setattr(
        workspace.runtime, "decide_action",
        Mock(side_effect=sqlite3.OperationalError("database is locked")),
    )
    with pytest.raises(sqlite3.OperationalError):
        decide_action(workspace, action.action_id, "CEO", "approve")
    monkeypatch.setattr(workspace.runtime, "decide_action", original)

    status, path = decide_action(workspace, action.action_id, "CTO", "approve")

    assert status == "approved"
    for record in [load_json(path), workspace.runtime.action(action.action_id)]:
        assert {item["approver"] for item in record["approvals"]} == {"CEO", "CTO"}
    rebuild_runtime(workspace)
    assert workspace.runtime.action(action.action_id)["status"] == "approved"


@pytest.mark.parametrize("attributed", [True, False])
def test_checkpoint_does_not_consume_another_instruction(active_project, attributed: bool) -> None:
    workspace, project_id, _ = active_project
    for identifier, task in [("inst_A", "TASK-A"), ("inst_B", "TASK-B")]:
        record_instruction(
            workspace,
            Instruction(
                instruction_id=identifier, sender_authority="CTO", action="HARDEN",
                project_id=project_id, spec_id="SPEC-001", task_id=task,
                observed="Independent task", evidence="Two distinct acceptance criteria",
                required_action=f"Complete {task}", do_not="Change the other task",
            ),
        )
    owner = f"{project_id}:owner"
    messages = {row["object_id"]: row["id"] for row in workspace.runtime.inbox(owner)}
    work = {row["message_id"]: row["id"] for row in workspace.runtime.queued_work(owner)}
    record_checkpoint(
        workspace, project_id, "SPEC-001", "TASK-A", "A completed", ["A verified"], "TASK-B",
        actor=owner, source_instruction_id="inst_A" if attributed else None,
    )

    reconcile_owner_work(workspace, project_id)

    assert workspace.runtime.work_status(work[messages["inst_A"]]) == "done"
    assert workspace.runtime.work_status(work[messages["inst_B"]]) == "queued"


@pytest.mark.parametrize("reserved", ["signals", "BETS", "roles", "portfolio", "operations",
                                      "playbook", ".oneco", ".git"])
def test_project_cannot_occupy_company_control_directory(tmp_path: Path, reserved: str) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Protected company", git_init=True)
    before = workspace.portfolio().model_dump(mode="json")
    ignore = (workspace.root / ".gitignore").read_bytes()

    with pytest.raises(ProjectError, match="reserved"):
        workspace.register_project("Product", Path(reserved))

    assert workspace.portfolio().model_dump(mode="json") == before
    assert (workspace.root / ".gitignore").read_bytes() == ignore
    assert not (workspace.root / reserved / ".git").exists()


@pytest.mark.parametrize("outcome", [None, True, False])
def test_endorsement_retry_preserves_approved_and_terminal_release_state(
    tmp_path: Path, outcome: bool | None
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Release retry", git_init=True)
    project, _, release = _approved_release(workspace)
    if outcome is not None:
        record_release_result(
            workspace, release.release_id, actor=f"{project.id}:owner", succeeded=outcome,
            evidence=["evidence/acceptance.txt"], note="Recorded execution",
        )
    before = workspace.runtime.snapshot(extended=True)["releases"][0]["state"]

    endorse_release(workspace, release.release_id, "CEO", "Product accepted")
    endorse_release(workspace, release.release_id, "CTO", "Technical evidence accepted")

    assert workspace.runtime.snapshot(extended=True)["releases"][0]["state"] == before
    if outcome is None:
        record_release_result(
            workspace, release.release_id, actor=f"{project.id}:owner", succeeded=True,
            evidence=["evidence/acceptance.txt"], note="First execution",
        )


def test_provider_startup_failure_is_retryable_and_supervised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Spawn recovery", git_init=True)
    project, root = _launched_project(workspace)
    owner = f"{project.id}:owner"
    session = workspace.runtime.register_session(owner, root)
    work_id = workspace.runtime.enqueue_work(owner, project.id)
    monkeypatch.setattr(orchestrator.trae, "require_model", lambda _: Path("/bin/false"))
    monkeypatch.setattr(orchestrator.trae, "exec_argv", lambda *args, **kwargs: ["provider"])
    monkeypatch.setattr(
        orchestrator.subprocess, "Popen",
        Mock(side_effect=OSError("temporary provider startup failure")),
    )

    assert orchestrator.run_owner_turn(
        workspace, project.id, str(session["session_id"]), int(session["epoch"])
    ) == 1
    assert workspace.runtime.work_status(work_id) == "queued"
    monkeypatch.undo()
    wakes = Mock()
    monkeypatch.setattr(supervisor, "wake_owner", wakes)

    supervisor.supervision_tick(workspace)

    assert wakes.call_count == 1


def test_abandoned_running_work_resumes_the_exact_owner_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Abandoned recovery", git_init=True)
    project, root = _launched_project(workspace)
    owner = f"{project.id}:owner"
    session = workspace.runtime.register_session(owner, root, provider_session_id="owner-thread")
    work_id = workspace.runtime.enqueue_work(owner, project.id)
    claim = workspace.runtime.claim_work(owner)
    workspace.runtime.start_work(work_id, claim["claim_token"])
    workspace.runtime.mark_lost(session["session_id"], session["epoch"])
    targets = Mock()
    monkeypatch.setattr(orchestrator.tmux, "target_alive", lambda _: False)
    monkeypatch.setattr(orchestrator.tmux, "ensure_owner_target", targets)

    result = orchestrator.wake_owner(workspace, project.id)

    assert result["status"] == "started"
    assert workspace.runtime.work_status(work_id) == "queued"
    assert workspace.runtime.current_session(owner)["provider_session_id"] == "owner-thread"
    targets.assert_called_once()


def test_concurrent_owner_wakes_launch_one_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Single writer", git_init=True)
    project, _ = _launched_project(workspace)
    workspace.runtime.enqueue_work(f"{project.id}:owner", project.id)
    start = threading.Barrier(2)
    alive = threading.Event()
    launches = []

    def create_target(*args):
        time.sleep(0.05)
        launches.append(args)
        alive.set()

    monkeypatch.setattr(orchestrator.tmux, "target_alive", lambda _: alive.is_set())
    monkeypatch.setattr(orchestrator.tmux, "ensure_owner_target", create_target)

    def wake(_):
        start.wait(timeout=5)
        return orchestrator.wake_owner(workspace, project.id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(wake, range(2)))

    assert len(launches) == 1
    assert sum(result["status"] == "started" for result in results) == 1
    assert len(workspace.runtime.snapshot()["identities"]) == 1


def test_rejected_release_has_an_accurate_delivery_state(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Rejected release", git_init=True)
    project, root = _launched_project(workspace)
    evidence = root / "evidence" / "acceptance.txt"
    evidence.write_text("acceptance passed\n")
    product_commit = commit_exact(root, [evidence], "test: verified evidence")
    release = record_release_candidate(
        workspace, project.id, product_commit=product_commit,
        acceptance_evidence=["evidence/acceptance.txt"],
    )
    endorse_release(workspace, release.release_id, "CEO", "Accepted")
    endorse_release(workspace, release.release_id, "CTO", "Accepted")
    board_decide_release(workspace, release.release_id, "reject", "Do not ship this candidate")

    snapshot = BridgeAPI(workspace).snapshot()

    assert snapshot["projects"][0]["delivery_state"] == "release_rejected"
    assert "rejection" in snapshot["projects"][0]["current_task"]
    assert snapshot["releases"][0]["state"] == "rejected"
