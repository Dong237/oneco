from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from oneco_os.errors import AuthorizationError, ConflictError, SessionError, StaleSessionError
from oneco_os.runtime import Runtime


@pytest.fixture
def runtime(tmp_path: Path) -> Runtime:
    value = Runtime(tmp_path / "runtime.sqlite")
    value.initialize()
    return value


def _action(action_id: str, domain: str) -> dict[str, object]:
    return {
        "action_id": action_id,
        "requester": "PROJ-001:owner",
        "project_id": "PROJ-001",
        "domain": domain,
        "summary": "Publish a launch page",
        "effect": "Creates a public artifact",
        "reversible": True,
        "created_at": datetime.now(UTC).isoformat(),
    }


def test_initialize_creates_private_wal_database_with_version(runtime: Runtime) -> None:
    assert runtime.database.stat().st_mode & 0o777 == 0o600
    with runtime.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "2"
        )


def test_session_takeover_increments_epoch_and_rejects_superseded_session(
    runtime: Runtime, tmp_path: Path
) -> None:
    first = runtime.register_session("PROJ-001:owner", tmp_path)
    with pytest.raises(ConflictError, match="already has an active session"):
        runtime.register_session("PROJ-001:owner", tmp_path)

    second = runtime.register_session("PROJ-001:owner", tmp_path, replace=True)

    assert first["epoch"] == 1
    assert second["epoch"] == 2
    assert second["session_id"] != first["session_id"]
    with pytest.raises(StaleSessionError, match="stale session"):
        runtime.assert_current(str(first["session_id"]), int(first["epoch"]))
    assert (
        runtime.assert_current(str(second["session_id"]), int(second["epoch"]))["logical_id"]
        == "PROJ-001:owner"
    )
    with runtime.connect() as connection:
        assert (
            connection.execute(
                "SELECT lifecycle FROM sessions WHERE id=?", (first["session_id"],)
            ).fetchone()[0]
            == "superseded"
        )


def test_close_releases_identity_and_preserves_monotonic_epoch(
    runtime: Runtime, tmp_path: Path
) -> None:
    first = runtime.register_session("CEO", tmp_path)
    runtime.close_session(str(first["session_id"]), int(first["epoch"]))

    with pytest.raises(StaleSessionError):
        runtime.touch(str(first["session_id"]), int(first["epoch"]))
    second = runtime.register_session("CEO", tmp_path)
    assert second["epoch"] == 2

    with pytest.raises(SessionError, match="unknown session"):
        runtime.assert_current("ses_missing", 1)


def test_messages_are_durable_idempotent_and_acknowledged_by_recipient(
    runtime: Runtime, tmp_path: Path
) -> None:
    message_id = runtime.send_message(
        "CEO",
        "PROJ-001:owner",
        "instruction",
        "Continue TASK-002",
        project_id="PROJ-001",
        requires_ack=True,
        idempotency_key="instruction-001",
    )
    duplicate_id = runtime.send_message(
        "CEO",
        "PROJ-001:owner",
        "instruction",
        "A retry must not create another message",
        project_id="PROJ-001",
        requires_ack=True,
        idempotency_key="instruction-001",
    )

    assert duplicate_id == message_id
    reopened = Runtime(runtime.database)
    rows = reopened.inbox("PROJ-001:owner", unread_only=True)
    assert [row["id"] for row in rows] == [message_id]
    assert rows[0]["body"] == "Continue TASK-002"
    assert rows[0]["requires_ack"] == 1

    with pytest.raises(AuthorizationError, match="cannot acknowledge"):
        reopened.acknowledge(message_id, "CTO")
    reopened.acknowledge(message_id, "PROJ-001:owner")
    assert reopened.inbox("PROJ-001:owner", unread_only=True) == []
    acknowledged = reopened.inbox("PROJ-001:owner")[0]
    assert acknowledged["acked_by"] == "PROJ-001:owner"
    assert acknowledged["acked_at"] is not None


def test_broadcast_messages_are_visible_to_each_role(runtime: Runtime) -> None:
    message_id = runtime.send_message("BOARD", "ALL", "decision", "Company policy")

    assert [row["id"] for row in runtime.inbox("CEO")] == [message_id]
    assert [row["id"] for row in runtime.inbox("CTO")] == [message_id]


def test_acknowledging_missing_message_is_rejected(runtime: Runtime) -> None:
    with pytest.raises(ConflictError, match="message not found"):
        runtime.acknowledge("msg_missing", "CEO")


def test_writer_lease_is_owner_only_idempotent_and_released_on_takeover(
    runtime: Runtime, tmp_path: Path
) -> None:
    owner = runtime.register_session("PROJ-001:owner", tmp_path)
    ceo = runtime.register_session("CEO", tmp_path)

    runtime.acquire_writer("PROJ-001", str(owner["session_id"]), int(owner["epoch"]))
    runtime.acquire_writer("PROJ-001", str(owner["session_id"]), int(owner["epoch"]))
    with pytest.raises(AuthorizationError, match="only PROJ-001:owner"):
        runtime.acquire_writer("PROJ-001", str(ceo["session_id"]), int(ceo["epoch"]))

    replacement = runtime.register_session("PROJ-001:owner", tmp_path, replace=True)
    with runtime.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM writer_leases WHERE project_id='PROJ-001'"
            ).fetchone()[0]
            == 0
        )
    runtime.acquire_writer("PROJ-001", str(replacement["session_id"]), int(replacement["epoch"]))
    with pytest.raises(StaleSessionError):
        runtime.release_writer("PROJ-001", str(owner["session_id"]), int(owner["epoch"]))


def test_checkpoint_index_rejects_stale_epoch(runtime: Runtime, tmp_path: Path) -> None:
    first = runtime.register_session("PROJ-001:owner", tmp_path)
    runtime.register_session("PROJ-001:owner", tmp_path, replace=True)

    with pytest.raises(StaleSessionError):
        runtime.index_checkpoint(
            "cp_stale",
            "PROJ-001",
            tmp_path / "checkpoint.json",
            "SPEC-001",
            "TASK-001",
            session_id=str(first["session_id"]),
            epoch=int(first["epoch"]),
        )
    with runtime.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM checkpoint_index WHERE checkpoint_id='cp_stale'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("session_id,epoch", [("ses_only", None), (None, 1)])
def test_checkpoint_index_requires_complete_session_fence(
    runtime: Runtime, tmp_path: Path, session_id: str | None, epoch: int | None
) -> None:
    with pytest.raises((SessionError, ConflictError, ValueError)):
        runtime.index_checkpoint(
            "cp_partial",
            "PROJ-001",
            tmp_path / "checkpoint.json",
            "SPEC-001",
            "TASK-001",
            session_id=session_id,
            epoch=epoch,
        )


def test_action_approval_roles_and_cross_domain_consensus(runtime: Runtime) -> None:
    runtime.create_action(_action("act_product", "product"))
    with pytest.raises(AuthorizationError, match="CTO cannot decide"):
        runtime.decide_action("act_product", "CTO", "approve")
    assert runtime.decide_action("act_product", "CEO", "approve") == "approved"

    runtime.create_action(_action("act_technical", "technical"))
    with pytest.raises(AuthorizationError, match="CEO cannot decide"):
        runtime.decide_action("act_technical", "CEO", "approve")
    assert runtime.decide_action("act_technical", "CTO", "approve") == "approved"

    runtime.create_action(_action("act_cross", "cross_domain"))
    assert runtime.decide_action("act_cross", "CEO", "approve") == "pending"
    assert runtime.decide_action("act_cross", "CTO", "approve") == "approved"

    runtime.create_action(_action("act_rejected", "cross_domain"))
    assert runtime.decide_action("act_rejected", "CTO", "reject") == "rejected"


def test_action_decision_rejects_unknown_role_decision_and_action(runtime: Runtime) -> None:
    runtime.create_action(_action("act_product", "product"))
    with pytest.raises(AuthorizationError, match="only CEO or CTO"):
        runtime.decide_action("act_product", "BOARD", "approve")
    with pytest.raises(ConflictError, match="approve or reject"):
        runtime.decide_action("act_product", "CEO", "abstain")
    with pytest.raises(ConflictError, match="action not found"):
        runtime.decide_action("act_missing", "CEO", "approve")


def test_snapshot_exposes_coordination_state_without_leaderboard_metrics(
    runtime: Runtime, tmp_path: Path
) -> None:
    runtime.register_session("CEO", tmp_path)
    runtime.send_message("BOARD", "CEO", "decision", "Continue")
    runtime.create_action(_action("act_product", "product"))

    snapshot = runtime.snapshot()

    assert set(snapshot) == {"identities", "unread", "pending_actions"}
    assert snapshot["unread"] == {"CEO": 1}
    serialized = repr(snapshot).lower()
    for metric in ("leaderboard", "ranking", "token_count", "code_volume"):
        assert metric not in serialized


def test_snapshot_lists_only_identities_with_current_sessions(
    runtime: Runtime, tmp_path: Path
) -> None:
    session = runtime.register_session("CEO", tmp_path)
    runtime.close_session(str(session["session_id"]), int(session["epoch"]))

    assert runtime.snapshot()["identities"] == []


def test_runtime_schema_contains_no_leaderboard_or_product_scoring_tables(runtime: Runtime) -> None:
    with runtime.connect() as connection:
        names = {
            row[0].lower()
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
    assert not names & {"leaderboard", "leaderboards", "scores", "rankings", "metrics"}
