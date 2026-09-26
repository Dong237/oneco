from __future__ import annotations

import json
from pathlib import Path

import pytest

from oneco_os.errors import AuthorizationError, StaleSessionError
from oneco_os.models import Instruction
from oneco_os.services import (
    create_action_request,
    rebuild_runtime,
    record_checkpoint,
    record_instruction,
)
from oneco_os.workspace import Workspace


def _instruction(project_id: str) -> Instruction:
    return Instruction(
        instruction_id="inst_001",
        sender_authority="CTO",
        action="HARDEN",
        project_id=project_id,
        spec_id="SPEC-001",
        task_id="TASK-002",
        observed="The error path is unverified",
        evidence="The checkpoint lists only the happy path",
        required_action="Add recovery coverage",
        do_not="Do not widen the feature scope",
    )


def test_record_checkpoint_is_durable_indexed_and_notifies_reviewers(
    active_project: tuple[Workspace, str, Path],
) -> None:
    workspace, project_id, project_root = active_project
    session = workspace.runtime.register_session(f"{project_id}:owner", project_root)

    checkpoint, path = record_checkpoint(
        workspace,
        project_id,
        "SPEC-001",
        "TASK-001",
        "A runnable slice is complete",
        ["pytest passed"],
        "TASK-002",
        actor=f"{project_id}:owner",
        session_id=str(session["session_id"]),
        epoch=int(session["epoch"]),
        key_files=["src/app.py"],
    )

    assert path.parent == project_root / "checkpoints"
    assert json.loads(path.read_text(encoding="utf-8"))["checkpoint_id"] == checkpoint.checkpoint_id
    project_data = json.loads((project_root / "project.json").read_text(encoding="utf-8"))
    assert project_data["current_spec"] == "SPEC-001"
    assert project_data["current_task"] == "TASK-002"
    assert project_data["latest_checkpoint"] == path.relative_to(project_root).as_posix()
    assert workspace.runtime.inbox("CEO")[0]["kind"] == "checkpoint"
    assert workspace.runtime.inbox("CTO")[0]["kind"] == "checkpoint"
    with workspace.runtime.connect() as connection:
        indexed = connection.execute(
            "SELECT checkpoint_id, session_id, epoch FROM checkpoint_index"
        ).fetchone()
    assert tuple(indexed) == (checkpoint.checkpoint_id, session["session_id"], session["epoch"])


def test_record_checkpoint_rejects_wrong_owner(
    active_project: tuple[Workspace, str, Path],
) -> None:
    workspace, project_id, _ = active_project

    with pytest.raises(AuthorizationError, match=f"only {project_id}:owner"):
        record_checkpoint(
            workspace,
            project_id,
            "SPEC-001",
            "TASK-001",
            "Done",
            ["verified"],
            "TASK-002",
            actor="CEO",
        )


def test_stale_checkpoint_is_rejected_without_writing_durable_record(
    active_project: tuple[Workspace, str, Path],
) -> None:
    workspace, project_id, project_root = active_project
    stale = workspace.runtime.register_session(f"{project_id}:owner", project_root)
    workspace.runtime.register_session(f"{project_id}:owner", project_root, replace=True)
    before = list((project_root / "checkpoints").iterdir())

    with pytest.raises(StaleSessionError):
        record_checkpoint(
            workspace,
            project_id,
            "SPEC-001",
            "TASK-001",
            "Must not persist",
            ["verified"],
            "TASK-002",
            actor=f"{project_id}:owner",
            session_id=str(stale["session_id"]),
            epoch=int(stale["epoch"]),
        )

    assert list((project_root / "checkpoints").iterdir()) == before


def test_rebuild_runtime_restores_derived_indexes_and_messages_from_durable_files(
    active_project: tuple[Workspace, str, Path],
) -> None:
    workspace, project_id, project_root = active_project
    session = workspace.runtime.register_session(f"{project_id}:owner", project_root)
    _, checkpoint_path = record_checkpoint(
        workspace,
        project_id,
        "SPEC-001",
        "TASK-001",
        "Complete",
        ["pytest passed"],
        "TASK-002",
        actor=f"{project_id}:owner",
        session_id=str(session["session_id"]),
        epoch=int(session["epoch"]),
    )
    instruction_path = record_instruction(workspace, _instruction(project_id))
    action = create_action_request(
        workspace,
        project_id,
        f"{project_id}:owner",
        "cross_domain",
        "Publish and deploy",
        "Creates public infrastructure",
        True,
    )
    durable_before = {
        checkpoint_path: checkpoint_path.read_bytes(),
        instruction_path: instruction_path.read_bytes(),
        project_root / "decisions" / f"{action.action_id}.json": (
            project_root / "decisions" / f"{action.action_id}.json"
        ).read_bytes(),
    }

    counts = rebuild_runtime(workspace)

    assert counts == {"checkpoints": 1, "instructions": 1, "actions": 1}
    assert all(path.read_bytes() == content for path, content in durable_before.items())
    with workspace.runtime.connect() as connection:
        assert connection.execute("SELECT count(*) FROM checkpoint_index").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM action_requests").fetchone()[0] == "pending"
    owner_inbox = workspace.runtime.inbox(f"{project_id}:owner", unread_only=True)
    assert [(row["kind"], row["requires_ack"]) for row in owner_inbox] == [("instruction", 1)]
    assert workspace.runtime.snapshot()["unread"] == {f"{project_id}:owner": 1}


def test_live_terminal_notifications_are_best_effort(
    active_project: tuple[Workspace, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, project_id, project_root = active_project
    session = workspace.runtime.register_session(
        "CEO", workspace.root, tmux_target="oneco-test:ceo"
    )
    calls: list[tuple[str | None, str]] = []

    def capture(target: str | None, text: str) -> bool:
        calls.append((target, text))
        return True

    monkeypatch.setattr("oneco_os.adapters.tmux.notify", capture)
    record_checkpoint(
        workspace,
        project_id,
        "SPEC-001",
        "T001",
        "done",
        ["passed"],
        "T002",
        actor=f"{project_id}:owner",
    )

    assert session["logical_id"] == "CEO"
    assert calls == [
        (
            "oneco-test:ceo",
            f"{project_id} checkpoint T001: done\nVerification: passed\nNext: T002",
        )
    ]
    assert len(workspace.runtime.inbox("CEO")) == 1
    assert list((project_root / "checkpoints").glob("*.json"))
