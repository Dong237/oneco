from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from oneco_os import __version__
from oneco_os.cli import app
from oneco_os.services import create_action_request
from oneco_os.workspace import Workspace

runner = CliRunner()


def invoke_ok(*arguments: object):
    result = runner.invoke(app, [str(argument) for argument in arguments])
    assert result.exit_code == 0, result.output
    return result


def test_root_help_and_version_expose_stable_cli_surface() -> None:
    help_result = invoke_ok("--help")

    for command in (
        "company",
        "project",
        "status",
        "message",
        "inbox",
        "session",
        "checkpoint",
        "action",
        "runtime",
        "doctor",
    ):
        assert command in help_result.output
    assert invoke_ok("--version").output.strip() == __version__


def test_company_init_project_register_and_status_json_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "company"

    initialized = invoke_ok(
        "company", "init", root, "--name", "CLI Company", "--no-git-init", "--json"
    )
    assert json.loads(initialized.output)["company"]["company_name"] == "CLI Company"

    registered = invoke_ok(
        "project",
        "register",
        "--root",
        root,
        "--name",
        "First Product",
        "--path",
        "first-product",
        "--no-git-init",
        "--json",
    )
    assert json.loads(registered.output)["id"] == "PROJ-001"

    status = json.loads(invoke_ok("status", "--root", root, "--json").output)
    assert status["company"] == "CLI Company"
    assert [(project["id"], project["path"]) for project in status["projects"]] == [
        ("PROJ-001", "first-product")
    ]
    assert "leaderboard" not in status
    assert "scores" not in status


def test_cli_rejects_duplicate_init_and_path_escape_without_traceback(tmp_path: Path) -> None:
    root = tmp_path / "company"
    invoke_ok("company", "init", root, "--name", "CLI Company", "--no-git-init")

    duplicate = runner.invoke(
        app, ["company", "init", str(root), "--name", "Replacement", "--no-git-init"]
    )
    assert duplicate.exit_code == 2
    assert "already a OneCo company" in duplicate.output
    assert "Traceback" not in duplicate.output

    escaped = runner.invoke(
        app,
        [
            "project",
            "register",
            "--root",
            str(root),
            "--name",
            "Escape",
            "--path",
            "../escape",
            "--no-git-init",
        ],
    )
    assert escaped.exit_code == 2
    assert "direct children" in escaped.output
    assert not (tmp_path / "escape").exists()


def test_cli_session_takeover_message_ack_and_unread_filter(
    workspace: Workspace, tmp_path: Path
) -> None:
    first = json.loads(
        invoke_ok(
            "session", "register", "CEO", "--cwd", tmp_path, "--root", workspace.root, "--json"
        ).output
    )
    second = json.loads(
        invoke_ok(
            "session", "takeover", "CEO", "--cwd", tmp_path, "--root", workspace.root, "--json"
        ).output
    )
    assert (first["epoch"], second["epoch"]) == (1, 2)

    sent = invoke_ok(
        "message",
        "send",
        "--root",
        workspace.root,
        "--sender",
        "CTO",
        "--to",
        "CEO",
        "--kind",
        "context",
        "--body",
        "Review the boundary",
        "--requires-ack",
    )
    message_id = re.search(r"msg_[0-9a-f]+", sent.output)
    assert message_id is not None

    unread = json.loads(
        invoke_ok(
            "inbox", "list", "CEO", "--root", workspace.root, "--unread-only", "--json"
        ).output
    )
    assert [message["id"] for message in unread] == [message_id.group()]
    invoke_ok("inbox", "ack", message_id.group(), "--actor", "CEO", "--root", workspace.root)
    assert (
        json.loads(
            invoke_ok("inbox", "CEO", "--root", workspace.root, "--unread-only", "--json").output
        )
        == []
    )


def test_cli_checkpoint_enforces_session_epoch_and_writes_durable_record(
    active_project: tuple[Workspace, str, Path],
) -> None:
    workspace, project_id, project_root = active_project
    stale = json.loads(
        invoke_ok(
            "session", "register", f"{project_id}:owner", "--root", workspace.root, "--json"
        ).output
    )
    current = json.loads(
        invoke_ok(
            "session", "takeover", f"{project_id}:owner", "--root", workspace.root, "--json"
        ).output
    )

    common = [
        "checkpoint",
        "create",
        "--root",
        str(workspace.root),
        "--project",
        project_id,
        "--spec",
        "SPEC-001",
        "--task",
        "TASK-001",
        "--result",
        "Slice complete",
        "--verification",
        "pytest passed",
        "--next-task",
        "TASK-002",
        "--key-file",
        "src/app.py",
        "--json",
    ]
    rejected = runner.invoke(
        app,
        common
        + [
            "--session-id",
            stale["session_id"],
            "--epoch",
            str(stale["epoch"]),
        ],
    )
    assert rejected.exit_code == 2
    assert "stale session" in rejected.output
    assert list((project_root / "checkpoints").iterdir()) == []

    created = json.loads(
        runner.invoke(
            app,
            common
            + [
                "--session-id",
                current["session_id"],
                "--epoch",
                str(current["epoch"]),
            ],
        ).output
    )
    assert created["checkpoint"]["task_id"] == "TASK-001"
    assert (project_root / created["path"]).exists() or Path(created["path"]).exists()


def test_cli_rejects_owner_session_before_project_activation(workspace: Workspace) -> None:
    project = workspace.register_project(
        "Unshaped Product", workspace.root / "unshaped", initialize_git=True
    )

    result = runner.invoke(
        app,
        [
            "session",
            "register",
            f"{project.id}:owner",
            "--root",
            str(workspace.root),
        ],
    )

    assert result.exit_code == 2
    assert "Owner session requires ready+active" in result.output
    assert workspace.runtime.snapshot()["identities"] == []


def test_cli_action_roles_and_runtime_rebuild(active_project: tuple[Workspace, str, Path]) -> None:
    workspace, project_id, _ = active_project
    action = create_action_request(
        workspace,
        project_id,
        f"{project_id}:owner",
        "cross_domain",
        "Publish launch",
        "Public and infrastructure effect",
        True,
    )

    first = invoke_ok(
        "action", "approve", action.action_id, "--approver", "CEO", "--root", workspace.root
    )
    assert "pending" in first.output
    second = invoke_ok(
        "action", "approve", action.action_id, "--approver", "CTO", "--root", workspace.root
    )
    assert "approved" in second.output

    rebuilt = json.loads(invoke_ok("runtime", "rebuild", "--root", workspace.root, "--json").output)
    assert rebuilt == {"checkpoints": 0, "instructions": 0, "actions": 1}
    with workspace.runtime.connect() as connection:
        row = connection.execute(
            "SELECT status FROM action_requests WHERE action_id=?", (action.action_id,)
        ).fetchone()
    assert row[0] == "approved"
