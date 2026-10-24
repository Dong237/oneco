from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from oneco_os.bridge import BridgeAPI
from oneco_os.cli import app
from oneco_os.company_services import (
    board_decide_release,
    endorse_release,
    record_release_candidate,
    record_release_result,
    record_technical_review,
)
from oneco_os.models import AuthorityKind, WorkState
from oneco_os.records import commit_exact
from oneco_os.services import rebuild_runtime
from oneco_os.supervisor import supervision_tick
from oneco_os.workspace import Workspace

runner = CliRunner()


def _remove_runtime(workspace: Workspace) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(workspace.runtime.database) + suffix).unlink(missing_ok=True)
    workspace.runtime.initialize()


def _launched_project(workspace: Workspace):
    project = workspace.register_project("Alpha", Path("alpha"), initialize_git=True)
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
    workspace.endorse_launch(project.id, "CTO", "Technically ready")
    workspace.activate_from_launch(project.id, actor="CEO")
    return workspace.find_project(project.id), root


def _approved_release(workspace: Workspace):
    project, root = _launched_project(workspace)
    evidence = root / "evidence" / "acceptance.txt"
    evidence.write_text("acceptance passed\n", encoding="utf-8")
    product_commit = commit_exact(
        root,
        [path for path in root.rglob("*") if path.is_file() and ".git" not in path.parts],
        "test: verified product increment",
    )
    assert product_commit
    release = record_release_candidate(
        workspace,
        project.id,
        product_commit=product_commit,
        acceptance_evidence=["evidence/acceptance.txt"],
    )
    endorse_release(workspace, release.release_id, "CEO", "Product accepted")
    endorse_release(workspace, release.release_id, "CTO", "Technical evidence accepted")
    board_decide_release(workspace, release.release_id, "approve", "Ship exact bytes")
    return project, root, release


def test_cockpit_open_is_read_only_and_does_not_start_supervisor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "QA", git_init=False)
    opened: list[tuple[Path, bool, str]] = []

    monkeypatch.setattr(
        "oneco_os.supervisor.ensure_supervisor",
        lambda *args, **kwargs: pytest.fail("Cockpit must not start supervision"),
    )
    monkeypatch.setattr(
        "oneco_os.bridge.run_bridge",
        lambda current, launch_executives=True, permission="": opened.append(
            (current.root, launch_executives, permission)
        ),
    )

    result = runner.invoke(app, ["cockpit", "--root", str(workspace.root), "--no-executives"])

    assert result.exit_code == 0, result.output
    assert opened == [(workspace.root, False, "bypass_permissions")]


def test_cockpit_uses_launch_decision_instead_of_endorsement_record(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "QA", git_init=True)
    project, _ = _launched_project(workspace)
    launch_digest = workspace.launch_digest(project.id)

    displayed = BridgeAPI(workspace).snapshot()["projects"][0]["launch"]

    assert displayed["record"]["launch_digest"] == launch_digest
    assert displayed["record"]["launched_by"] == "CEO"
    assert displayed["changed_since_launch"] is False


def test_release_result_retry_is_idempotent_after_terminal_runtime_state(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "QA", git_init=True)
    project, _, release = _approved_release(workspace)

    first = record_release_result(
        workspace,
        release.release_id,
        actor=f"{project.id}:owner",
        succeeded=True,
        evidence=["evidence/acceptance.txt"],
        note="Production verified",
    )
    second = record_release_result(
        workspace,
        release.release_id,
        actor=f"{project.id}:owner",
        succeeded=True,
        evidence=["evidence/acceptance.txt"],
        note="Production verified",
    )

    assert second == first


def test_release_result_retires_owner_work_and_is_informational(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "QA", git_init=True)
    project, _, release = _approved_release(workspace)
    owner = f"{project.id}:owner"
    production = next(
        row for row in workspace.runtime.inbox(owner) if row["kind"] == "production_release"
    )

    record_release_result(
        workspace,
        release.release_id,
        actor=owner,
        succeeded=True,
        evidence=["evidence/acceptance.txt"],
        note="Production verified",
    )

    with workspace.runtime.connect() as connection:
        work = connection.execute(
            "SELECT status FROM work_queue WHERE message_id=?", (production["id"],)
        ).fetchone()
    assert work["status"] == "done"
    assert production["id"] not in {
        row["id"] for row in workspace.runtime.inbox(owner, unread_only=True)
    }
    assert not any(
        row["kind"] == "release_result"
        for row in workspace.runtime.inbox("BOARD", unread_only=True)
    )


def test_cockpit_overlays_terminal_result_and_effective_delivery_state(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "QA", git_init=True)
    project, _, release = _approved_release(workspace)
    result_path = record_release_result(
        workspace,
        release.release_id,
        actor=f"{project.id}:owner",
        succeeded=True,
        evidence=["evidence/acceptance.txt"],
        note="Production verified",
    )
    owner = workspace.runtime.register_session(
        f"{project.id}:owner",
        workspace.project_path(project.id),
        authority_kind=AuthorityKind.OWNER,
    )
    workspace.runtime.touch(str(owner["session_id"]), int(owner["epoch"]), WorkState.BLOCKED)

    snapshot = BridgeAPI(workspace).snapshot()
    displayed_release = snapshot["releases"][0]
    displayed_project = snapshot["projects"][0]

    assert displayed_release["state"] == "released"
    assert displayed_release["result"] == json.loads(result_path.read_text(encoding="utf-8"))
    assert displayed_project["delivery_state"] == "released"
    assert displayed_project["current_task"] == "Production release completed"
    assert displayed_project["owner_effective_state"] == "complete"
    assert displayed_project["queue"] == []
    assert not any(item["kind"] == "technical_review" for item in snapshot["attention"])


def test_rebuild_terminal_release_has_no_obsolete_role_or_owner_work(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "QA", git_init=True)
    project, _, release = _approved_release(workspace)
    record_technical_review(
        workspace,
        project.id,
        verdict="needs_correction",
        evidence="Unsafe retry",
        required_action="Fix retry",
        affected_scope="retry",
        review_id="review_retry_bad",
    )
    record_technical_review(
        workspace,
        project.id,
        verdict="on_track",
        evidence="Retry is now safe",
        required_action="Continue",
        affected_scope="retry",
        review_id="review_retry_clear",
    )
    record_release_result(
        workspace,
        release.release_id,
        actor=f"{project.id}:owner",
        succeeded=True,
        evidence=["evidence/acceptance.txt"],
        note="Production verified",
    )
    _remove_runtime(workspace)

    rebuild_runtime(workspace)
    runtime = workspace.runtime.snapshot(extended=True)

    assert runtime["work_queue"] == []
    assert runtime["role_work_queue"] == []
    assert runtime["releases"][0]["state"] == "released"
    assert BridgeAPI(workspace).snapshot()["attention"] == []


def test_rebuild_queues_only_missing_release_endorsement(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "QA", git_init=True)
    project, root = _launched_project(workspace)
    evidence = root / "evidence" / "acceptance.txt"
    evidence.write_text("passed\n", encoding="utf-8")
    product_commit = commit_exact(
        root,
        [path for path in root.rglob("*") if path.is_file() and ".git" not in path.parts],
        "test: candidate",
    )
    assert product_commit
    release = record_release_candidate(
        workspace,
        project.id,
        product_commit=product_commit,
        acceptance_evidence=["evidence/acceptance.txt"],
    )
    endorse_release(workspace, release.release_id, "CEO", "Product accepted")
    _remove_runtime(workspace)

    rebuild_runtime(workspace)

    assert workspace.runtime.queued_role_work("CEO") == []
    assert {
        (row["kind"], row["object_id"]) for row in workspace.runtime.queued_role_work("CTO")
    } == {("release_candidate", release.release_id)}


def test_rebuild_restores_initial_launch_work(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "QA", git_init=True)
    project, root = _launched_project(workspace)
    launch_digest = workspace.launch_digest(project.id)
    brief = root / "BRIEF.md"
    brief.write_text(brief.read_text(encoding="utf-8") + "\nOwner implementation note.\n")
    commit_exact(root, [brief], "test: evolve project after launch")
    assert workspace.launch_digest(project.id) != launch_digest
    _remove_runtime(workspace)

    rebuild_runtime(workspace)

    queued = workspace.runtime.snapshot(extended=True)["work_queue"]
    assert [(row["recipient"], row["project_id"]) for row in queued] == [
        (f"{project.id}:owner", project.id)
    ]
    launch_message = workspace.runtime.inbox(f"{project.id}:owner", unread_only=True)[0]
    assert launch_message["kind"] == "launch"
    assert launch_message["artifact_digest"] == launch_digest


def test_rebuild_restores_latest_on_track_clearance_before_candidate(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "QA", git_init=True)
    project, _ = _launched_project(workspace)
    record_technical_review(
        workspace,
        project.id,
        verdict="on_track",
        evidence="Acceptance evidence is sound",
        required_action="Prepare release candidate",
        affected_scope="current increment",
        review_id="review_ready",
    )
    _remove_runtime(workspace)

    rebuild_runtime(workspace)

    owner_messages = workspace.runtime.inbox(f"{project.id}:owner", unread_only=True)
    assert [row["kind"] for row in owner_messages] == ["technical_clearance"]
    assert len(workspace.runtime.snapshot(extended=True)["work_queue"]) == 1


def test_supervisor_never_replays_owner_after_terminal_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "QA", git_init=True)
    project, _, release = _approved_release(workspace)
    record_release_result(
        workspace,
        release.release_id,
        actor=f"{project.id}:owner",
        succeeded=True,
        evidence=["evidence/acceptance.txt"],
        note="Production verified",
    )
    monkeypatch.setattr(
        "oneco_os.supervisor.wake_owner",
        lambda *args, **kwargs: pytest.fail("released work must never be replayed"),
    )

    supervision_tick(workspace)

    assert workspace.runtime.snapshot(extended=True)["releases"][0]["state"] == "released"


def test_spec_validate_accepts_standard_speckit_task_checkbox(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "QA", git_init=False)
    project = workspace.register_project("Alpha", Path("alpha"), initialize_git=False)
    root = workspace.project_path(project.id)
    feature = root / "specs" / "001-first-vertical"
    feature.mkdir(parents=True)
    (root / ".specify" / "feature.json").write_text(
        json.dumps({"feature_directory": "specs/001-first-vertical", "spec_id": "SPEC-001"}),
        encoding="utf-8",
    )
    for name in ("spec.md", "plan.md"):
        (feature / name).write_text(f"# {name}\n", encoding="utf-8")
    (feature / "tasks.md").write_text("- [ ] T001 Implement the vertical slice\n", encoding="utf-8")

    result = runner.invoke(
        app, ["spec", "validate", project.id, "--root", str(workspace.root), "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["valid"] is True


def test_human_can_requeue_failed_owner_work(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "QA", git_init=False)
    work_id = workspace.runtime.enqueue_work("PROJ-001:owner", "PROJ-001")
    claimed = workspace.runtime.claim_work("PROJ-001:owner")
    assert claimed
    workspace.runtime.finish_work(work_id, str(claimed["claim_token"]), error="provider failed")

    result = runner.invoke(app, ["work", "retry", work_id, "--root", str(workspace.root), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "queued"
    assert workspace.runtime.work_status(work_id) == "queued"


def test_session_reconcile_preserves_exact_thread_for_dead_ghostty_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "QA", git_init=False)
    session = workspace.runtime.register_session(
        "CEO",
        workspace.root,
        provider_session_id="thread-exact",
        authority_kind=AuthorityKind.CANONICAL,
        terminal_transport="ghostty",
        terminal_handle="dead-window",
    )
    monkeypatch.setattr("oneco_os.cli.ghostty.is_alive", lambda handle: False)

    result = runner.invoke(
        app,
        ["session", "reconcile", "--root", str(workspace.root), "--stale-after", "9999", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert str(session["session_id"]) in json.loads(result.output)["lost"]
    current = workspace.runtime.current_session("CEO")
    assert current["lifecycle"] == "lost"
    assert current["provider_session_id"] == "thread-exact"


@pytest.mark.parametrize("provider_session_id", [None, "thread-exact"])
def test_role_exec_commands_always_include_full_permission_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider_session_id: str | None,
) -> None:
    from oneco_os.adapters import trae

    monkeypatch.setattr(
        trae, "isolated_config_argv", lambda cwd: ["-c", "plugins.x.enabled=false"]
    )
    argv = trae.exec_argv(
        Path("/usr/bin/traecli"),
        cwd=tmp_path,
        model="GPT-5.6-Sol",
        permission=trae.DEFAULT_PERMISSION_MODE,
        prompt="continue",
        provider_session_id=provider_session_id,
    )

    assert argv[1:3] == ["-c", "plugins.x.enabled=false"]
    assert "--no-daemon" not in argv
    assert "--dangerously-bypass-hook-trust" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "--permission-mode" not in argv
