from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from oneco_os.bridge import BridgeAPI
from oneco_os.company_services import (
    board_decide_bet,
    board_decide_release,
    create_bet_draft,
    endorse_release,
    promote_selected_bet,
    record_release_candidate,
    record_release_result,
    record_signal,
    submit_bet,
)
from oneco_os.errors import ConflictError
from oneco_os.models import AuthorityKind
from oneco_os.records import commit_exact
from oneco_os.runtime import Runtime
from oneco_os.services import rebuild_runtime, record_checkpoint
from oneco_os.supervisor import socket_path as supervisor_socket_path
from oneco_os.util import local_socket_path
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
Mixed: one successful run, repeat use not yet proven.

## Recommendation and confidence boundary
Select a narrow trial; confidence remains medium until repeat use is observed.
"""


@pytest.fixture
def runtime(tmp_path: Path) -> Runtime:
    value = Runtime(tmp_path / "runtime.sqlite")
    value.initialize()
    return value


def test_concurrent_messages_have_one_monotonic_sequence_per_thread(runtime: Runtime) -> None:
    def send(index: int) -> str:
        return runtime.send_message(
            "CEO",
            "CTO",
            "shaping",
            f"Message {index}",
            thread_key="PROJ-001",
            idempotency_key=f"concurrent-message-{index}",
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        message_ids = list(pool.map(send, range(36)))

    assert len(set(message_ids)) == 36
    rows = runtime.inbox("CTO")
    assert [row["sequence"] for row in rows] == list(range(1, 37))


def test_concurrent_identical_message_retry_creates_exactly_one_message(runtime: Runtime) -> None:
    def send(_: int) -> str:
        return runtime.send_message(
            "CEO",
            "CTO",
            "shaping",
            "Same durable request",
            thread_key="PROJ-001",
            idempotency_key="same-request",
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        message_ids = list(pool.map(send, range(24)))

    assert len(set(message_ids)) == 1
    assert len(runtime.inbox("CTO")) == 1


def test_out_of_order_acknowledgements_are_independent_and_idempotent(runtime: Runtime) -> None:
    message_ids = [
        runtime.send_message(
            "CEO",
            "CTO",
            "shaping",
            f"Step {index}",
            thread_key="PROJ-001",
            requires_ack=True,
        )
        for index in range(3)
    ]

    runtime.acknowledge(message_ids[2], "CTO")
    runtime.acknowledge(message_ids[0], "CTO")
    runtime.acknowledge(message_ids[2], "CTO")

    unread = runtime.inbox("CTO", unread_only=True)
    assert [row["id"] for row in unread] == [message_ids[1]]
    assert unread[0]["sequence"] == 2


def test_role_work_retry_rejects_changed_payload(runtime: Runtime) -> None:
    first = runtime.enqueue_role_work(
        "CTO",
        "gate_review",
        {"review_id": "review-1", "project_id": "PROJ-001"},
        object_id="review-1",
        idempotency_key="review-1:cto:work",
    )
    assert runtime.enqueue_role_work(
        "CTO",
        "gate_review",
        {"review_id": "review-1", "project_id": "PROJ-001"},
        object_id="review-1",
        idempotency_key="review-1:cto:work",
    ) == first

    with pytest.raises(ConflictError, match="different role work"):
        runtime.enqueue_role_work(
            "CTO",
            "gate_review",
            {"review_id": "review-1", "project_id": "PROJ-999"},
            object_id="review-1",
            idempotency_key="review-1:cto:work",
        )


def test_service_socket_paths_remain_bounded_for_deep_company_roots(tmp_path: Path) -> None:
    deep_root = tmp_path.joinpath(*(f"very-long-company-segment-{index}" for index in range(12)))
    workspace = Workspace.initialize(deep_root, "Deep Company", git_init=False)

    for channel in ("bridge", "supervisor"):
        path = local_socket_path(workspace.root, channel)
        assert len(os.fsencode(path)) < 100
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert supervisor_socket_path(workspace) == local_socket_path(workspace.root, "supervisor")


def test_complete_signal_to_release_survives_repeated_runtime_rebuilds(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Pressure Test Company", git_init=True)
    signal = record_signal(
        workspace,
        "Board idea",
        "A traceable opportunity reported by the human Board.",
        [{"kind": "board_idea", "title": "Board note", "uri": "board://pressure-test"}],
    )
    bet = create_bet_draft(workspace, "Release confidence", [signal.signal_id], PACKET)
    manifest = submit_bet(workspace, bet.bet_id, validation_state="mixed")
    board_decide_bet(
        workspace,
        bet.bet_id,
        manifest.revision,
        manifest.composite_digest,
        "select",
        "Run the smallest evidence-bound release workflow.",
    )
    promotion = promote_selected_bet(workspace, bet.bet_id, name="Pressure Product")
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
        [path for path in project_root.rglob("*") if path.is_file() and ".git" not in path.parts],
        "test: commit launch packet",
    )
    workspace.endorse_launch(project.id, "CEO", "Product intent and acceptance are ready.")
    workspace.endorse_launch(project.id, "CTO", "Architecture and safety floor are ready.")
    workspace.activate_from_launch(project.id, actor="CEO")

    owner = workspace.runtime.register_session(
        f"{project.id}:owner",
        project_root,
        authority_kind=AuthorityKind.OWNER,
    )
    checkpoint, _ = record_checkpoint(
        workspace,
        project.id,
        "SPEC-001",
        "TASK-001",
        "Acceptance criteria verified",
        ["pytest: passed", "manual smoke: passed"],
        "Prepare release candidate",
        actor=f"{project.id}:owner",
        session_id=str(owner["session_id"]),
        epoch=int(owner["epoch"]),
    )
    evidence = project_root / "evidence" / "acceptance.txt"
    evidence.write_text(
        f"{checkpoint.checkpoint_id}: acceptance passed\n",
        encoding="utf-8",
    )
    product_commit = commit_exact(
        project_root,
        [path for path in project_root.rglob("*") if path.is_file() and ".git" not in path.parts],
        "test: complete verified increment",
    )
    assert product_commit
    release = record_release_candidate(
        workspace,
        project.id,
        product_commit=product_commit,
        acceptance_evidence=["evidence/acceptance.txt"],
    )
    endorse_release(workspace, release.release_id, "CEO", "Product acceptance met.")
    endorse_release(workspace, release.release_id, "CTO", "Technical evidence met.")
    board_decide_release(
        workspace,
        release.release_id,
        "approve",
        "Approve this exact digest for production.",
    )
    record_release_result(
        workspace,
        release.release_id,
        actor=f"{project.id}:owner",
        succeeded=True,
        evidence=["evidence/acceptance.txt"],
        note="Production execution and verification completed.",
    )

    before = BridgeAPI(workspace).snapshot()
    assert before["releases"][0]["state"] == "released"
    assert before["projects"][0]["latest_checkpoint"]["checkpoint_id"] == checkpoint.checkpoint_id
    assert {row["kind"] for row in before["messages"]} >= {
        "signal_assignment",
        "bet_ready",
        "bet_decision",
        "technical_shaping_request",
        "checkpoint",
        "release_candidate",
        "release_ready",
        "production_release",
        "release_result",
    }

    first_counts = rebuild_runtime(workspace)
    first_snapshot = BridgeAPI(workspace).snapshot()
    second_counts = rebuild_runtime(workspace)
    second_snapshot = BridgeAPI(workspace).snapshot()

    assert first_counts == second_counts
    assert [
        (row["release_id"], row["release_digest"], row["state"])
        for row in first_snapshot["releases"]
    ] == [
        (row["release_id"], row["release_digest"], row["state"])
        for row in second_snapshot["releases"]
    ]
    assert first_snapshot["unread"] == second_snapshot["unread"]
    assert len(first_snapshot["messages"]) == len(second_snapshot["messages"])
    assert first_snapshot["releases"][0]["state"] == "released"
    assert workspace.runtime.queued_work(f"{project.id}:owner") == []


def test_cockpit_release_chronology_comes_from_durable_candidates_after_rebuild(
    tmp_path: Path,
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Release Order", git_init=True)
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
    workspace.endorse_launch(project.id, "CTO", "Technical ready")
    workspace.activate_from_launch(project.id, actor="CEO")

    evidence = project_root / "evidence" / "acceptance.txt"
    evidence.write_text("acceptance v1\n", encoding="utf-8")
    first_commit = commit_exact(project_root, [evidence], "test: acceptance v1")
    assert first_commit
    first = record_release_candidate(
        workspace,
        project.id,
        product_commit=first_commit,
        acceptance_evidence=["evidence/acceptance.txt"],
    )

    evidence.write_text("acceptance v2\n", encoding="utf-8")
    second_commit = commit_exact(project_root, [evidence], "test: acceptance v2")
    assert second_commit
    second = record_release_candidate(
        workspace,
        project.id,
        product_commit=second_commit,
        acceptance_evidence=["evidence/acceptance.txt"],
    )
    endorse_release(workspace, second.release_id, "CEO", "Product acceptance met.")
    endorse_release(workspace, second.release_id, "CTO", "Technical evidence met.")
    board_decide_release(workspace, second.release_id, "approve", "Ship the current evidence.")
    record_release_result(
        workspace,
        second.release_id,
        actor=f"{project.id}:owner",
        succeeded=True,
        evidence=["evidence/acceptance.txt"],
        note="Production verification passed.",
    )

    for suffix in ("", "-wal", "-shm"):
        Path(str(workspace.runtime.database) + suffix).unlink(missing_ok=True)
    workspace.runtime.initialize()
    rebuild_runtime(workspace)
    # SQLite is a rebuildable read model. Its insertion/update timestamps may be in
    # any order and must never redefine the durable candidate chronology.
    with workspace.runtime.connect() as connection:
        connection.execute(
            "UPDATE release_index SET updated_at='9999-12-31T23:59:59Z' WHERE release_id=?",
            (first.release_id,),
        )
        connection.execute(
            "UPDATE release_index SET updated_at='0001-01-01T00:00:00Z' WHERE release_id=?",
            (second.release_id,),
        )

    snapshot = BridgeAPI(workspace).snapshot()

    assert [release["release_id"] for release in snapshot["releases"]] == [
        first.release_id,
        second.release_id,
    ]
    assert snapshot["projects"][0]["delivery_state"] == "released"
    assert snapshot["projects"][0]["owner_effective_state"] == "complete"
    assert not any(
        item["kind"] == "release_stale" and item.get("object_id") == first.release_id
        for item in snapshot["attention"]
    )
