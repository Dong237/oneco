from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from typer.testing import CliRunner

from oneco_os import health, orchestrator
from oneco_os.authority import require_capability, resolve_agent_context
from oneco_os.bridge import BridgeAPI
from oneco_os.cli import app
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
    submit_bet,
)
from oneco_os.errors import ConflictError, StaleSessionError, WorkspaceError
from oneco_os.mcp_server import mcp
from oneco_os.models import (
    AuthorityKind,
    BlockerRecord,
    BlockerResolutionRecord,
    Checkpoint,
    IdentityKind,
    ReleaseRecord,
)
from oneco_os.records import commit_exact
from oneco_os.runtime import Runtime
from oneco_os.services import rebuild_runtime
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
Mixed.

## Recommendation and confidence boundary
Select a narrow trial; confidence remains medium.
"""


def _company(tmp_path: Path, name: str = "Hardening Company") -> Workspace:
    return Workspace.initialize(tmp_path / "company", name, git_init=True)


def _selected_bet(workspace: Workspace):
    signal = record_signal(
        workspace,
        "Board signal",
        "A traceable workflow problem.",
        [{"kind": "board_idea", "title": "Board note", "uri": "board://hardening"}],
    )
    bet = create_bet_draft(workspace, "Release confidence", [signal.signal_id], PACKET)
    manifest = submit_bet(workspace, bet.bet_id, validation_state="mixed")
    decision = board_decide_bet(
        workspace,
        bet.bet_id,
        manifest.revision,
        manifest.composite_digest,
        "select",
        "Proceed with the exact selected evidence.",
    )
    return bet, manifest, decision


def _active_project(workspace: Workspace):
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
    return project, root


def _release_candidate(workspace: Workspace):
    project, root = _active_project(workspace)
    evidence = root / "evidence" / "acceptance.txt"
    evidence.write_text("acceptance passed\n", encoding="utf-8")
    commit = commit_exact(
        root,
        [path for path in root.rglob("*") if path.is_file() and ".git" not in path.parts],
        "test: verified increment",
    )
    assert commit
    release = record_release_candidate(
        workspace,
        project.id,
        product_commit=commit,
        acceptance_evidence=["evidence/acceptance.txt"],
    )
    return project, root, release


def test_succession_promotes_identity_as_persistent_role(tmp_path: Path) -> None:
    root = tmp_path / "company"
    workspace = Workspace.initialize(root, "Succession", git_init=False)
    runtime = workspace.runtime
    runtime.register_session("CEO", root)
    candidate = runtime.register_session(
        "CEO:candidate:repair",
        root,
        authority_kind=AuthorityKind.CANDIDATE,
        identity_kind=IdentityKind.CANDIDATE,
    )
    phrase = runtime.request_succession("CEO", str(candidate["session_id"]))

    promoted = runtime.confirm_succession(str(candidate["session_id"]), "turn-1", phrase)
    assert promoted
    row = runtime.assert_active_session(str(candidate["session_id"]))
    assert row["identity_kind"] == IdentityKind.PERSISTENT_ROLE
    context = resolve_agent_context(
        {
            "ONECO_ROOT": str(root),
            "ONECO_SESSION_ID": str(candidate["session_id"]),
        }
    )
    assert require_capability(context, "project.promote") == "CEO"


def test_broadcast_acknowledgement_is_per_recipient(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path / "runtime.sqlite")
    runtime.initialize()
    message_id = runtime.send_message(
        "SYSTEM", "ALL", "notice", "Read this", requires_ack=True
    )

    runtime.acknowledge(message_id, "CEO")
    assert [row["id"] for row in runtime.inbox("CEO", unread_only=True)] == []
    assert [row["id"] for row in runtime.inbox("CTO", unread_only=True)] == [message_id]
    runtime.acknowledge(message_id, "CTO")
    assert runtime.inbox("CTO", unread_only=True) == []


def test_takeover_reclaims_stale_role_turn_lease(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path / "runtime.sqlite")
    runtime.initialize()
    first = runtime.register_session("CEO", tmp_path)
    runtime.acquire_role_turn("CEO", str(first["session_id"]), int(first["epoch"]))

    second = runtime.register_session("CEO", tmp_path, replace=True)

    assert runtime.acquire_role_turn(
        "CEO", str(second["session_id"]), int(second["epoch"])
    )


def test_message_write_rejects_stale_sender_epoch(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path / "runtime.sqlite")
    runtime.initialize()
    first = runtime.register_session("CEO", tmp_path)
    runtime.register_session("CEO", tmp_path, replace=True)

    with pytest.raises(StaleSessionError):
        runtime.send_message(
            "CEO",
            "CTO",
            "shaping",
            "Stale instruction",
            sender_session_id=str(first["session_id"]),
            sender_epoch=int(first["epoch"]),
        )


def test_inbox_preserves_sequence_with_equal_timestamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(tmp_path / "runtime.sqlite")
    runtime.initialize()
    values = iter((SimpleNamespace(hex="f" * 32), SimpleNamespace(hex="0" * 32)))
    monkeypatch.setattr("oneco_os.runtime.uuid.uuid4", lambda: next(values))
    monkeypatch.setattr("oneco_os.runtime.now_text", lambda: "2026-09-28T00:00:00+00:00")
    runtime.send_message("CEO", "CTO", "shaping", "First", thread_key="PROJ-001")
    runtime.send_message("CEO", "CTO", "shaping", "Second", thread_key="PROJ-001")

    assert [row["sequence"] for row in runtime.inbox("CTO")] == [1, 2]


def test_concurrent_executive_open_launches_one_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Concurrent", git_init=False)
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    monkeypatch.setattr(orchestrator.trae, "executive_command", lambda *a, **k: ["trae"])
    monkeypatch.setattr(orchestrator.ghostty, "is_alive", lambda handle: handle == "window-1")
    monkeypatch.setattr(orchestrator.ghostty, "focus", lambda handle: None)

    def bind_fake_process(workspace, session_id, epoch, launch_nonce, **kwargs):
        workspace.runtime.bind_process(
            session_id,
            epoch,
            os.getpid(),
            launch_nonce=launch_nonce,
        )
        return True

    monkeypatch.setattr(orchestrator, "_wait_for_executive_process", bind_fake_process)

    def open_window(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return {"window_id": "window-1"}

    monkeypatch.setattr(orchestrator.ghostty, "open_window", open_window)
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            results.append(orchestrator.open_executive(workspace, "CEO"))
        except BaseException as exc:  # pragma: no cover - assertion reports the exception
            errors.append(exc)

    first = threading.Thread(target=invoke)
    second = threading.Thread(target=invoke)
    first.start()
    assert entered.wait(2)
    second.start()
    time.sleep(0.15)
    try:
        assert len(calls) == 1
    finally:
        release.set()
        first.join(3)
        second.join(3)
    assert not errors
    assert sorted(str(item["status"]) for item in results) == ["focused", "opened"]


def test_owner_outcome_must_come_from_owner_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _company(tmp_path)
    project, root = _active_project(workspace)
    owner = workspace.runtime.register_session(
        f"{project.id}:owner", root, authority_kind=AuthorityKind.OWNER
    )
    work_id = workspace.runtime.enqueue_work(f"{project.id}:owner", project.id)

    monkeypatch.setattr(orchestrator.trae, "require_model", lambda model: Path("/bin/true"))
    monkeypatch.setattr(orchestrator.trae, "exec_argv", lambda *a, **k: ["true"])

    class Process:
        stdout: list[str] = []

        def wait(self) -> int:
            review_root = workspace.root / "operations" / project.id / "reviews"
            review_root.mkdir(parents=True, exist_ok=True)
            (review_root / "unrelated.json").write_text("{}", encoding="utf-8")
            return 0

    monkeypatch.setattr(orchestrator.subprocess, "Popen", lambda *a, **k: Process())

    assert orchestrator.run_owner_turn(
        workspace, project.id, str(owner["session_id"]), int(owner["epoch"])
    ) == 1
    row = next(item for item in workspace.runtime.queued_work() if item["id"] == work_id)
    assert row["status"] != "done"


def test_owner_provider_crash_requeues_exact_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _company(tmp_path)
    project, root = _active_project(workspace)
    owner = workspace.runtime.register_session(
        f"{project.id}:owner", root, authority_kind=AuthorityKind.OWNER
    )
    work_id = workspace.runtime.enqueue_work(f"{project.id}:owner", project.id)
    monkeypatch.setattr(orchestrator.trae, "require_model", lambda model: Path("/bin/false"))
    monkeypatch.setattr(orchestrator.trae, "exec_argv", lambda *a, **k: ["false"])

    class Process:
        stdout: list[str] = []

        def wait(self) -> int:
            return 1

    monkeypatch.setattr(orchestrator.subprocess, "Popen", lambda *a, **k: Process())

    assert orchestrator.run_owner_turn(
        workspace, project.id, str(owner["session_id"]), int(owner["epoch"])
    ) == 1
    row = next(item for item in workspace.runtime.queued_work() if item["id"] == work_id)
    assert row["status"] == "queued"
    assert "Trae exited" in row["error"]


def test_owner_tmux_yard_is_scoped_to_company_root(tmp_path: Path) -> None:
    first = Workspace.initialize(tmp_path / "one" / "company", "Same Name", git_init=False)
    second = Workspace.initialize(tmp_path / "two" / "company", "Same Name", git_init=False)

    assert orchestrator.owner_yard_name(first) != orchestrator.owner_yard_name(second)


def test_bet_signal_ids_cannot_escape_signal_root(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    outside = tmp_path / "outside-signal"
    outside.mkdir()
    (outside / "SIGNAL.md").write_text("# Outside\n\nStatus: `assigned`\n", encoding="utf-8")
    original = {
        "signal_id": "SIG-9999",
        "title": "Outside",
        "summary": "Must not be touched",
        "status": "assigned",
        "assigned_by": "BOARD",
        "source_ids": ["SRC-001"],
        "linked_bet_ids": [],
        "created_at": "2026-09-28T00:00:00Z",
    }
    (outside / "signal.json").write_text(json.dumps(original), encoding="utf-8")

    with pytest.raises(ConflictError):
        create_bet_draft(workspace, "Escape", [str(outside)], PACKET)

    assert json.loads((outside / "signal.json").read_text(encoding="utf-8")) == original


def test_release_requires_evidence_present_in_product_commit(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    project, root = _active_project(workspace)
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    evidence = root / "evidence" / "untracked.txt"
    evidence.write_text("not in commit\n", encoding="utf-8")

    with pytest.raises(ConflictError, match="commit"):
        record_release_candidate(
            workspace,
            project.id,
            product_commit=commit,
            acceptance_evidence=["evidence/untracked.txt"],
        )


def test_release_result_revalidates_approved_digest(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    project, root, release = _release_candidate(workspace)
    endorse_release(workspace, release.release_id, "CEO", "Product accepted")
    endorse_release(workspace, release.release_id, "CTO", "Technical evidence accepted")
    board_decide_release(workspace, release.release_id, "approve", "Ship exact bytes")
    (root / "evidence" / "acceptance.txt").write_text("changed\n", encoding="utf-8")

    with pytest.raises(ConflictError, match="stale|changed"):
        record_release_result(
            workspace,
            release.release_id,
            actor=f"{project.id}:owner",
            succeeded=True,
            evidence=["evidence/acceptance.txt"],
            note="Must not release stale evidence",
        )


def test_partial_promotion_is_resumable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _company(tmp_path)
    bet, _, _ = _selected_bet(workspace)
    original = __import__("shutil").copy2
    calls = 0

    def fail_once(source, destination, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("interrupted promotion")
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr("oneco_os.company_services.shutil.copy2", fail_once)
    with pytest.raises(OSError, match="interrupted"):
        promote_selected_bet(workspace, bet.bet_id, name="Recoverable Product")
    monkeypatch.setattr("oneco_os.company_services.shutil.copy2", original)

    promotion = promote_selected_bet(workspace, bet.bet_id, name="Recoverable Product")
    assert promotion.project_id == workspace.portfolio().projects[0].id
    assert len(workspace.portfolio().projects) == 1


def test_rebuild_ignores_uncommitted_board_release_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _company(tmp_path)
    project, _, release = _release_candidate(workspace)
    endorse_release(workspace, release.release_id, "CEO", "Product accepted")
    endorse_release(workspace, release.release_id, "CTO", "Technical evidence accepted")

    def fail_commit(*args, **kwargs):
        raise WorkspaceError("simulated Git failure")

    monkeypatch.setattr("oneco_os.company_services.commit_exact", fail_commit)
    with pytest.raises(WorkspaceError, match="simulated"):
        board_decide_release(workspace, release.release_id, "approve", "Ship")
    for suffix in ("", "-wal", "-shm"):
        Path(str(workspace.runtime.database) + suffix).unlink(missing_ok=True)
    workspace.runtime.initialize()

    rebuild_runtime(workspace)

    indexed = workspace.runtime.snapshot(extended=True)["releases"]
    assert indexed[0]["state"] == "ready"
    assert not workspace.runtime.queued_work(f"{project.id}:owner")


def test_agent_environment_cannot_use_human_control_plane_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "company"
    Workspace.initialize(root, "CLI Guard", git_init=False)
    monkeypatch.setenv("ONECO_SESSION_ID", "ses_agent")
    monkeypatch.setenv("ONECO_IDENTITY", "CPO")
    result = CliRunner().invoke(
        app,
        ["session", "register", "CEO", "--root", str(root), "--json"],
    )

    assert result.exit_code == 2
    assert Workspace.open(root).runtime.current_session("CEO") is None


def test_mcp_evidence_and_enum_schema_matches_runtime_contract() -> None:
    tools = asyncio.run(mcp.list_tools())
    schema = next(tool.inputSchema for tool in tools if tool.name == "oneco_bet_submit")
    evidence = schema["properties"]["evidence"]["anyOf"][0]["items"]
    if "$ref" in evidence:
        evidence = schema["$defs"][evidence["$ref"].rsplit("/", 1)[-1]]
    validation = schema["properties"]["validation_state"]

    assert set(evidence["required"]) >= {"evidence_id", "title", "observed_at"}
    assert evidence["additionalProperties"] is False
    assert set(validation["enum"]) == {"passed", "mixed", "failed", "invalid", "tbd", "not_run"}


def test_health_reports_missing_trae_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Health", git_init=False)
    monkeypatch.setattr(
        health.trae,
        "find_trae",
        lambda: (_ for _ in ()).throw(WorkspaceError("Trae executable not found")),
    )

    rows = {row["name"]: row for row in health.collect(workspace, persist=False)}

    for name in ("trae", "oneco-plugin", "oneco-mcp-session-env", "user-mcp-profile"):
        assert rows[name]["state"] in {"missing", "degraded"}


def test_external_skill_contract_requires_usable_body(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: agent-reach\n---\n", encoding="utf-8")

    assert health._skill_health(skill, "agent-reach")["state"] == "incompatible"

    skill.write_text(
        "---\nname: agent-reach\ndescription: Research access\n---\n# Stub\nToo short.\n",
        encoding="utf-8",
    )
    assert health._skill_health(skill, "agent-reach")["state"] == "incompatible"


def test_plugin_cross_skill_references_are_namespaced() -> None:
    root = Path("adapters/trae-plugin/oneco/skills")
    files = [*root.glob("*/SKILL.md"), *root.glob("*/agents/openai.yaml")]

    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "$oneco-" not in text, path


def test_release_id_rejects_scriptable_values() -> None:
    with pytest.raises(ValidationError):
        ReleaseRecord(
            release_id="rel_x');alert(1);('y",
            project_id="PROJ-001",
            product_commit="abc",
            release_digest="a" * 64,
            acceptance_evidence=["evidence/result.txt"],
        )


def test_hardened_durable_models_match_public_schemas() -> None:
    root = Path("schemas")
    digest = "a" * 64
    release = ReleaseRecord(
        release_id="rel_" + "b" * 32,
        project_id="PROJ-001",
        product_commit="abc123",
        release_digest=digest,
        acceptance_evidence=["evidence/result.txt"],
        acceptance_evidence_hashes={"evidence/result.txt": digest},
    )
    blocker = BlockerRecord(
        blocker_id="block_test",
        project_id="PROJ-001",
        reported_by="PROJ-001:owner",
        recorded_session_id="ses_owner",
        recorded_epoch=2,
        summary="Unsafe migration",
        evidence="Rollback is unavailable",
        requested_decision="Choose a safe migration",
    )
    blocker_resolution = BlockerResolutionRecord(
        resolution_id="resolve_test",
        blocker_id=blocker.blocker_id,
        project_id=blocker.project_id,
        resolved_by=blocker.reported_by,
        recorded_session_id="ses_owner",
        recorded_epoch=2,
        checkpoint_id="cp_test",
        evidence="Rollback verification passed",
    )
    checkpoint = Checkpoint(
        checkpoint_id="cp_test",
        project_id="PROJ-001",
        spec_id="SPEC-001",
        task_id="TASK-001",
        result="Complete",
        verification=["pytest passed"],
        next_task="TASK-002",
        source_instruction_id="inst_test",
    )

    for filename, record in (
        ("release.schema.json", release),
        ("blocker.schema.json", blocker),
        ("blocker-resolution.schema.json", blocker_resolution),
        ("checkpoint.schema.json", checkpoint),
    ):
        schema = json.loads((root / filename).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(record.model_dump(mode="json"))


def test_cockpit_uses_data_actions_instead_of_inline_javascript() -> None:
    page = Path("src/oneco_os/resources/bridge/index.html").read_text(encoding="utf-8")
    assert "onclick=" not in page


def test_cockpit_revalidates_release_before_showing_board_action(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    _, root, release = _release_candidate(workspace)
    endorse_release(workspace, release.release_id, "CEO", "Product accepted")
    endorse_release(workspace, release.release_id, "CTO", "Technical evidence accepted")
    (root / "evidence" / "acceptance.txt").write_text("changed\n", encoding="utf-8")

    snapshot = BridgeAPI(workspace).snapshot()
    displayed = next(item for item in snapshot["releases"] if item["release_id"] == release.release_id)

    assert displayed["state"] == "stale"
    assert not any(item["kind"] == "release_decision" for item in snapshot["attention"])


def test_later_on_track_review_resolves_same_scope_attention(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    project, _ = _active_project(workspace)
    record_technical_review(
        workspace,
        project.id,
        verdict="needs_correction",
        evidence="A failing invariant",
        required_action="Correct it",
        affected_scope="payments",
        review_id="review_payments_1",
    )
    record_technical_review(
        workspace,
        project.id,
        verdict="on_track",
        evidence="The invariant now passes",
        required_action="Continue",
        affected_scope="payments",
        review_id="review_payments_2",
    )

    snapshot = BridgeAPI(workspace).snapshot()

    assert not any(
        item["kind"] == "technical_review" and item.get("project_id") == project.id
        for item in snapshot["attention"]
    )


def test_stale_notices_do_not_count_as_current_unread(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    stale = workspace.runtime.send_message(
        "CPO",
        "BOARD",
        "bet_ready",
        "Old revision",
        object_id="BET-0001",
        artifact_digest="a" * 64,
        requires_ack=True,
    )
    workspace.runtime.mark_object_messages_stale("BET-0001", "b" * 64)

    snapshot = BridgeAPI(workspace).snapshot()

    assert stale not in {item["id"] for item in snapshot["messages"]}
    assert snapshot["unread"].get("BOARD", 0) == 0


def test_cockpit_signal_has_board_provenance_without_external_uri(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    signal = BridgeAPI(workspace).add_signal(
        "Board idea", "Investigate this user pain", "board_idea", "Board direction", ""
    )
    sources = json.loads(
        (
            workspace.root
            / "signals"
            / signal["signal_id"]
            / "sources.manifest.json"
        ).read_text(encoding="utf-8")
    )

    assert sources[0]["uri"].startswith("board://")

