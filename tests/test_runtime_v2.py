from __future__ import annotations

import json
from pathlib import Path

import pytest

from oneco_os.errors import AuthorizationError, ConflictError, StaleSessionError
from oneco_os.models import AuthorityKind
from oneco_os.runtime import Runtime
from oneco_os.workspace import Workspace


def test_runtime_v1_database_migrates_without_losing_rows(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite"
    runtime = Runtime(database)
    runtime.initialize()
    session = runtime.register_session("CEO", tmp_path, provider_session_id="thread-old")
    message = runtime.send_message("CTO", "CEO", "context", "Preserve me")

    # Simulate a v1 database by removing the new version marker only; initialize must be idempotent.
    with runtime.connect() as connection:
        connection.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
    runtime.initialize()

    assert runtime.current_session("CEO")["provider_session_id"] == "thread-old"
    assert runtime.inbox("CEO")[0]["id"] == message
    assert runtime.assert_current(str(session["session_id"]), int(session["epoch"]))
    with runtime.connect() as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0] == "2"


def test_work_queue_serializes_owner_turns_and_preserves_new_work(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path / "runtime.sqlite")
    runtime.initialize()
    first = runtime.enqueue_work("PROJ-001:owner", "PROJ-001")
    second = runtime.enqueue_work("PROJ-001:owner", "PROJ-001", priority=50)

    claimed = runtime.claim_work("PROJ-001:owner")
    assert claimed and claimed["id"] == second
    assert runtime.claim_work("PROJ-001:owner") is None
    runtime.start_work(second, str(claimed["claim_token"]), "turn-1")
    runtime.finish_work(second, str(claimed["claim_token"]))

    next_claim = runtime.claim_work("PROJ-001:owner")
    assert next_claim and next_claim["id"] == first
    with pytest.raises(ConflictError, match="no longer current"):
        runtime.finish_work(second, str(claimed["claim_token"]))


def test_session_authority_and_provider_binding_are_runtime_derived(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path / "runtime.sqlite")
    runtime.initialize()
    secretary = runtime.register_session("CEO:secretary:research", tmp_path)
    row = runtime.assert_active_session(str(secretary["session_id"]))
    assert row["authority_kind"] == AuthorityKind.SECRETARY
    runtime.bind_provider_session(
        str(secretary["session_id"]), int(secretary["epoch"]), "provider-1"
    )
    with pytest.raises(ConflictError, match="already bound"):
        runtime.bind_provider_session(
            str(secretary["session_id"]), int(secretary["epoch"]), "provider-2"
        )


def test_succession_requires_exact_candidate_confirmation_and_fences_old_role(
    tmp_path: Path,
) -> None:
    runtime = Runtime(tmp_path / "runtime.sqlite")
    runtime.initialize()
    old = runtime.register_session("CEO", tmp_path)
    candidate = runtime.register_session(
        "CEO:candidate:repair", tmp_path, authority_kind=AuthorityKind.CANDIDATE
    )
    phrase = runtime.request_succession("CEO", str(candidate["session_id"]))

    assert runtime.confirm_succession(str(candidate["session_id"]), "turn-wrong", "PROMOTE CEO NO") is None
    promoted = runtime.confirm_succession(
        str(candidate["session_id"]), "turn-confirmed", phrase
    )
    assert promoted and promoted["role"] == "CEO"
    assert runtime.current_session("CEO")["id"] == candidate["session_id"]
    with pytest.raises(StaleSessionError):
        runtime.assert_current(str(old["session_id"]), int(old["epoch"]))


def test_manifest_v1_migrates_to_desktop_v2_with_backup(tmp_path: Path) -> None:
    root = tmp_path / "company"
    workspace = Workspace.initialize(root, "Acme", git_init=False)
    data = workspace.manifest().model_dump(mode="json", exclude_none=True)
    data["schema_version"] = 1
    data["terminal"] = "tmux"
    data.pop("desktop")
    workspace.manifest_path.write_text(json.dumps(data), encoding="utf-8")

    migrated = Workspace.open(root)

    assert migrated.manifest().schema_version == 2
    assert migrated.manifest().desktop.executives == "ghostty"
    assert migrated.manifest().desktop.owners == "tmux"
    assert (root / ".oneco" / "backups" / "manifest-v1.json").is_file()


def test_launch_endorsements_are_invalidated_by_artifact_change(workspace: Workspace) -> None:
    project = workspace.register_project("Alpha", Path("alpha"), initialize_git=False)
    project_root = workspace.project_path(project.id)
    for relative in (
        "BRIEF.md",
        ".specify/memory/constitution.md",
        "spec-seeds/001-first-vertical.md",
    ):
        path = project_root / relative
        path.write_text(
            path.read_text(encoding="utf-8").replace("TBD", "Defined").replace("DRAFT —", "READY —"),
            encoding="utf-8",
        )
    (project_root / ".git").mkdir()
    workspace.endorse_launch(project.id, "CEO", "Product ready")
    workspace.endorse_launch(project.id, "CTO", "Technical path ready")
    assert workspace.readiness(project.id)[0] is True

    with (project_root / "BRIEF.md").open("a", encoding="utf-8") as handle:
        handle.write("\nChanged after endorsement.\n")

    ready, reasons = workspace.readiness(project.id)
    assert ready is False
    assert "missing CEO endorsement for current launch packet" in reasons
    assert "missing CTO endorsement for current launch packet" in reasons


def test_board_override_requires_human_turn_and_cannot_bypass_artifacts(
    workspace: Workspace,
) -> None:
    project = workspace.register_project("Alpha", Path("alpha"), initialize_git=False)
    executive = workspace.runtime.register_session(
        "CEO", workspace.root, provider_session_id="provider-ceo"
    )
    workspace.runtime.record_human_turn(
        "provider-ceo", "turn-1", f"Just launch {project.id}."
    )
    override = workspace.runtime.consume_launch_override(project.id, "provider-ceo")
    assert override == "turn-1"
    assert workspace.runtime.consume_launch_override(project.id, "provider-ceo") is None
    with pytest.raises(Exception, match="cannot bypass artifacts"):
        workspace.activate_from_launch(project.id, actor="CEO", board_override_turn_id=override)
    assert executive["logical_id"] == "CEO"


def test_secretary_cannot_be_treated_as_canonical_executive(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path / "runtime.sqlite")
    runtime.initialize()
    secretary = runtime.register_session("CTO:secretary:alt", tmp_path)
    row = runtime.assert_active_session(str(secretary["session_id"]))
    assert row["logical_id"] != "CTO"
    assert row["authority_kind"] == AuthorityKind.SECRETARY
    with pytest.raises(AuthorizationError):
        runtime.request_succession("CTO", str(secretary["session_id"]))
