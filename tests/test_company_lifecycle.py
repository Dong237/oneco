from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from oneco_os.company_services import (
    board_decide_bet,
    board_decide_release,
    create_bet_draft,
    endorse_release,
    latest_bet_decision,
    list_bets,
    promote_selected_bet,
    record_release_candidate,
    record_release_result,
    record_signal,
    revise_bet_draft,
    submit_bet,
)
from oneco_os.errors import AuthorizationError, ConflictError
from oneco_os.records import commit_exact
from oneco_os.services import rebuild_runtime
from oneco_os.workspace import Workspace

PACKET = """# Product Bet Packet

## User and Job
Independent developers need a reliable release gate.

## Alternatives
Manual checklists and hosted project trackers.

## Source provenance and dates
See the attached evidence manifest.

## Counterevidence and unknowns
Teams may prefer their existing tracker. Maximum unknown: repeat usage.

## Product opportunity
A local evidence-bound release workflow.

## Business-system hypothesis
Paid local tooling for small AI-native teams.

## Validation result
Not run.

## Recommendation and confidence boundary
Investigate further; confidence is medium until repeat use is observed.
"""


def _company(tmp_path: Path) -> Workspace:
    return Workspace.initialize(tmp_path / "company", "Test Company", git_init=True)


def _submitted_bet(workspace: Workspace, *, validation: str = "not_run"):
    signal = record_signal(
        workspace,
        "Board idea",
        "A traceable opportunity reported by the human Board.",
        [{"kind": "board_idea", "title": "Board note", "uri": "board://turn-1"}],
    )
    bet = create_bet_draft(workspace, "Release confidence", [signal.signal_id], PACKET)
    return bet, submit_bet(workspace, bet.bet_id, validation_state=validation)


def test_board_idea_and_teardown_artifact_are_both_plain_traceable_signals(
    tmp_path: Path,
) -> None:
    workspace = _company(tmp_path)
    board = record_signal(
        workspace,
        "Board idea",
        "Investigate a workflow problem.",
        [{"kind": "board_idea", "title": "Board prompt", "uri": "board://turn-1"}],
    )
    teardown = record_signal(
        workspace,
        "External teardown",
        "A reviewed teardown may contain a useful signal.",
        [
            {
                "kind": "external_artifact",
                "title": "Daily Product Teardown output",
                "uri": "file:///reviewed/teardown.md",
            }
        ],
    )

    assert board.signal_id != teardown.signal_id
    assert json.loads(
        (workspace.root / "signals" / teardown.signal_id / "sources.manifest.json").read_text()
    )[0]["kind"] == "external_artifact"
    assert not any(
        row["kind"] == "daily_product_teardown"
        for row in workspace.runtime.queued_role_work("CPO")
    )


def test_submitted_revision_is_immutable_until_board_names_more_evidence(
    tmp_path: Path,
) -> None:
    workspace = _company(tmp_path)
    bet, manifest = _submitted_bet(workspace)

    with pytest.raises(ConflictError, match="Board more-evidence or reopen"):
        revise_bet_draft(workspace, bet.bet_id, PACKET + "\nChanged without authority.\n")

    board_decide_bet(
        workspace,
        bet.bet_id,
        manifest.revision,
        manifest.composite_digest,
        "more_evidence",
        "Repeat use is still unknown.",
        evidence_gap="Evidence of repeat use after the first successful release.",
    )
    revised = revise_bet_draft(workspace, bet.bet_id, PACKET + "\n## New evidence\nObserved twice.\n")
    assert revised.current_revision == 2


def test_resubmitted_revision_waits_for_a_new_board_decision(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    bet, manifest = _submitted_bet(workspace)
    board_decide_bet(
        workspace,
        bet.bet_id,
        manifest.revision,
        manifest.composite_digest,
        "more_evidence",
        "Repeat use is still unknown.",
        evidence_gap="Evidence of repeat use after the first successful release.",
    )
    revise_bet_draft(workspace, bet.bet_id, PACKET + "\n## New evidence\nObserved twice.\n")
    revised_manifest = submit_bet(workspace, bet.bet_id, validation_state="mixed")

    current = list_bets(workspace)[0]

    assert current["current_manifest"]["composite_digest"] == revised_manifest.composite_digest
    assert current["latest_decision"] is None


def test_sqlite_loss_restores_current_resubmitted_bet_for_board(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    bet, manifest = _submitted_bet(workspace)
    board_decide_bet(
        workspace,
        bet.bet_id,
        manifest.revision,
        manifest.composite_digest,
        "more_evidence",
        "Need stronger evidence.",
        evidence_gap="Repeat usage evidence.",
    )
    revise_bet_draft(workspace, bet.bet_id, PACKET + "\n## New evidence\nObserved twice.\n")
    revised_manifest = submit_bet(workspace, bet.bet_id, validation_state="mixed")
    for suffix in ("", "-wal", "-shm"):
        Path(str(workspace.runtime.database) + suffix).unlink(missing_ok=True)
    workspace.runtime.initialize()

    rebuild_runtime(workspace)

    board_inbox = workspace.runtime.inbox("BOARD")
    assert any(
        row["kind"] == "bet_ready"
        and row["artifact_digest"] == revised_manifest.composite_digest
        for row in board_inbox
    )
    assert not workspace.runtime.queued_role_work("CPO")


def test_sqlite_loss_restores_unprocessed_signal_assignment(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    signal = record_signal(
        workspace,
        "Recover this signal",
        "The CPO must still see this assignment after runtime loss.",
        [{"kind": "board_idea", "title": "Board prompt", "uri": "board://recovery"}],
    )
    for suffix in ("", "-wal", "-shm"):
        Path(str(workspace.runtime.database) + suffix).unlink(missing_ok=True)
    workspace.runtime.initialize()

    rebuild_runtime(workspace)

    assert any(
        row["kind"] == "signal_assignment" and row["object_id"] == signal.signal_id
        for row in workspace.runtime.inbox("CPO")
    )
    assert any(
        row["kind"] == "signal_assignment" and row["object_id"] == signal.signal_id
        for row in workspace.runtime.queued_role_work("CPO")
    )


@pytest.mark.parametrize("validation", ["passed", "mixed", "failed", "invalid", "tbd"])
def test_validation_state_is_evidence_not_board_authority(
    tmp_path: Path, validation: str
) -> None:
    workspace = _company(tmp_path)
    bet, manifest = _submitted_bet(workspace, validation=validation)

    decision = board_decide_bet(
        workspace,
        bet.bet_id,
        manifest.revision,
        manifest.composite_digest,
        "select",
        f"The Board accepts the stated {validation} confidence boundary.",
    )

    assert decision.kind == "select"
    assert latest_bet_decision(workspace, bet.bet_id) == decision


def test_rejected_bet_persists_without_creating_project(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    bet, manifest = _submitted_bet(workspace)
    decision = board_decide_bet(
        workspace,
        bet.bet_id,
        manifest.revision,
        manifest.composite_digest,
        "reject",
        "The opportunity is not worth pursuing now.",
    )

    assert decision.kind == "reject"
    assert workspace.portfolio().projects == []
    assert list((workspace.root / "bets" / bet.bet_id / "decisions").glob("*.json"))


def test_ceo_promotes_exact_selected_digest_and_stale_selection_is_blocked(
    tmp_path: Path,
) -> None:
    workspace = _company(tmp_path)
    bet, manifest = _submitted_bet(workspace)
    board_decide_bet(
        workspace,
        bet.bet_id,
        manifest.revision,
        manifest.composite_digest,
        "select",
        "Proceed with the smallest vertical result.",
    )

    promotion = promote_selected_bet(workspace, bet.bet_id, name="Release Confidence")

    project = workspace.find_project(promotion.project_id)
    assert project.source_bet_digest == manifest.composite_digest
    assert (
        workspace.project_path(project.id) / "handoffs/product-bet/PRODUCT_BET_PACKET.md"
    ).read_text() == PACKET
    assert subprocess.run(
        ["git", "-C", str(workspace.project_path(project.id)), "fsck", "--no-dangling"],
        capture_output=True,
        check=False,
    ).returncode == 0


def test_promotion_requires_current_board_selection(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    bet, manifest = _submitted_bet(workspace)
    board_decide_bet(
        workspace,
        bet.bet_id,
        manifest.revision,
        manifest.composite_digest,
        "more_evidence",
        "Need proof.",
        evidence_gap="A named adoption signal.",
    )

    with pytest.raises(AuthorizationError, match="Board-selected"):
        promote_selected_bet(workspace, bet.bet_id, name="Must Not Exist")


def test_sqlite_loss_recovers_signal_bet_decision_and_promotion_truth(tmp_path: Path) -> None:
    workspace = _company(tmp_path)
    bet, manifest = _submitted_bet(workspace)
    board_decide_bet(
        workspace,
        bet.bet_id,
        manifest.revision,
        manifest.composite_digest,
        "select",
        "Proceed.",
    )
    promotion = promote_selected_bet(workspace, bet.bet_id, name="Recovered Product")
    for suffix in ("", "-wal", "-shm"):
        Path(str(workspace.runtime.database) + suffix).unlink(missing_ok=True)
    workspace.runtime.initialize()

    counts = rebuild_runtime(workspace)

    assert counts["signals"] == 1
    assert counts["bet_revisions"] == 1
    assert counts["board_decisions"] == 1
    assert counts["promotions"] == 1
    assert workspace.find_project(promotion.project_id).source_bet_digest == manifest.composite_digest
    record_types = {row["record_type"] for row in workspace.runtime.records()}
    assert {"signal", "bet_revision", "board_bet_decision", "promotion"} <= record_types


def test_verified_increment_still_requires_exact_board_release_decision(
    tmp_path: Path,
) -> None:
    workspace = _company(tmp_path)
    project = workspace.register_project("Alpha", Path("alpha"), initialize_git=True)
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
    evidence = project_root / "evidence" / "acceptance.txt"
    evidence.write_text("All acceptance checks passed.\n", encoding="utf-8")
    product_commit = commit_exact(
        project_root,
        [path for path in project_root.rglob("*") if path.is_file() and ".git" not in path.parts],
        "test: verified increment",
    )
    assert product_commit
    workspace.endorse_launch(project.id, "CEO", "Product ready")
    workspace.endorse_launch(project.id, "CTO", "Technically sound")
    workspace.activate_from_launch(project.id, actor="CEO")

    release = record_release_candidate(
        workspace,
        project.id,
        product_commit=product_commit,
        acceptance_evidence=["evidence/acceptance.txt"],
    )
    assert workspace.runtime.queued_work(f"{project.id}:owner") == []
    endorse_release(workspace, release.release_id, "CEO", "Product acceptance met")
    endorse_release(workspace, release.release_id, "CTO", "Technical checks met")
    assert workspace.runtime.queued_work(f"{project.id}:owner") == []

    with pytest.raises(
        ConflictError,
        match="exact Board-approved queued release",
    ):
        record_release_result(
            workspace,
            release.release_id,
            actor=f"{project.id}:owner",
            succeeded=True,
            evidence=["evidence/acceptance.txt"],
            note="This must remain blocked until the Board approves the exact digest.",
        )

    decision = board_decide_release(
        workspace, release.release_id, "approve", "Ship this exact verified increment."
    )
    queue = workspace.runtime.queued_work(f"{project.id}:owner")
    assert decision.kind == "approve_release"
    assert len(queue) == 1
    assert workspace.runtime.snapshot(extended=True)["releases"][0]["state"] == "queued"

    result = record_release_result(
        workspace,
        release.release_id,
        actor=f"{project.id}:owner",
        succeeded=True,
        evidence=["evidence/acceptance.txt"],
        note="Production deployment completed and verified.",
    )
    assert result.is_file()
    assert workspace.runtime.snapshot(extended=True)["releases"][0]["state"] == "released"
