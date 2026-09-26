from __future__ import annotations

import json
import plistlib
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from typer.testing import CliRunner

from oneco_os import agent_tools, health
from oneco_os.adapters import ghostty, trae
from oneco_os.authority import AgentContext, resolve_agent_context
from oneco_os.bridge import BridgeAPI, run_bridge
from oneco_os.cli import _handle_hook, app
from oneco_os.errors import AuthorizationError
from oneco_os.launcher import create_launcher
from oneco_os.models import AuthorityKind, WorkState
from oneco_os.orchestrator import open_executive, run_executive, wake_owner
from oneco_os.workspace import Workspace

runner = CliRunner()


def test_create_is_one_scriptable_company_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "company"
    launcher = tmp_path / "OneCo.app"
    monkeypatch.setattr(trae, "require_model", lambda model: Path("/usr/bin/true"))
    monkeypatch.setattr("oneco_os.cli._install_trae_plugin", lambda: "installed")
    monkeypatch.setattr("oneco_os.cli.create_finder_launcher", lambda workspace: launcher)

    result = runner.invoke(
        app,
        [
            "create",
            str(root),
            "--name",
            "\u4e00\u4eba\u516c\u53f8",
            "--no-git-init",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["root"] == str(root.resolve())
    assert payload["launcher"] == str(launcher)
    assert Workspace.open(root).manifest().desktop.model_dump() == {
        "cockpit": "macos-webview",
        "executives": "ghostty",
        "owners": "tmux",
    }


def test_finder_launcher_quotes_interpreter_and_company_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = Workspace.initialize(tmp_path / "Company Folder", "Acme / Lab", git_init=False)
    monkeypatch.setattr("oneco_os.launcher.sys.executable", "/Applications/Python Custom/bin/python")

    app_path = create_launcher(workspace, tmp_path / "Applications")

    assert app_path.name == "OneCo \u2014 Acme - Lab.app"
    executable = app_path / "Contents" / "MacOS" / "oneco-launch"
    source = executable.read_text(encoding="utf-8")
    assert "'/Applications/Python Custom/bin/python'" in source
    assert f"'{workspace.root}'" in source
    with (app_path / "Contents" / "Info.plist").open("rb") as handle:
        plist = plistlib.load(handle)
    assert plist["CFBundleIdentifier"].startswith("local.oneco.")


def test_ghostty_window_uses_argumentized_applescript_and_safe_title(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: list[str] = []

    def fake_run(source: str, *arguments: str) -> str:
        captured.extend(arguments)
        return '{"window_id":"42","terminal_id":"9"}'

    monkeypatch.setattr(ghostty, "_run_applescript", fake_run)
    result = ghostty.open_window(
        tmp_path, ["/bin/echo", "hello; touch /tmp/no"], title="CEO'; nope"
    )

    assert result["window_id"] == "42"
    assert captured[0] == str(tmp_path.resolve())
    outer = shlex.split(captured[1])
    assert outer[:2] == ["/bin/sh", "-lc"]
    assert "exec /bin/echo 'hello; touch /tmp/no'" in outer[2]
    assert "CEO'" in outer[2]


def test_open_executive_focuses_one_canonical_window(
    monkeypatch: pytest.MonkeyPatch, workspace: Workspace
) -> None:
    opened: list[tuple[Path, list[str], str | None]] = []
    focused: list[str] = []
    monkeypatch.setattr(
        ghostty,
        "open_window",
        lambda cwd, command, title=None: (
            opened.append((cwd, command, title)) or {"window_id": "window-1"}
        ),
    )
    monkeypatch.setattr(ghostty, "is_alive", lambda handle: handle == "window-1")
    monkeypatch.setattr(ghostty, "focus", focused.append)

    first = open_executive(workspace, "CEO")
    second = open_executive(workspace, "CEO")

    assert first["status"] == "opened"
    assert second["status"] == "focused"
    assert len(opened) == 1
    assert opened[0][2] == "OneCo \u00b7 Test Company \u00b7 CEO"
    assert focused == ["window-1"]
    assert workspace.runtime.current_session("CEO")["epoch"] == 1


def test_open_executive_replaces_unbound_legacy_identity_without_guessing_thread(
    monkeypatch: pytest.MonkeyPatch, workspace: Workspace
) -> None:
    legacy = workspace.runtime.register_session(
        "CEO", workspace.root, tmux_target="oneco-legacy:ceo"
    )
    opened: list[list[str]] = []
    monkeypatch.setattr(ghostty, "is_alive", lambda handle: False)
    monkeypatch.setattr(
        ghostty,
        "open_window",
        lambda cwd, command, title=None: (opened.append(command) or {"window_id": "new-window"}),
    )

    result = open_executive(workspace, "CEO")

    assert result["status"] == "opened"
    assert "--resume" not in opened[0]
    current = workspace.runtime.current_session("CEO")
    assert current["epoch"] == 2
    assert current["provider_session_id"] is None
    with workspace.runtime.connect() as connection:
        old = connection.execute(
            "SELECT lifecycle FROM sessions WHERE id=?", (legacy["session_id"],)
        ).fetchone()
    assert old["lifecycle"] == "superseded"


def test_executive_resume_inherits_saved_approval_configuration(
    monkeypatch: pytest.MonkeyPatch, workspace: Workspace
) -> None:
    session = workspace.runtime.register_session("CEO", workspace.root)
    captured: dict[str, object] = {}
    monkeypatch.setattr(trae, "require_model", lambda model: Path("/usr/bin/traecli"))
    monkeypatch.setattr(
        "oneco_os.orchestrator.os.execvpe",
        lambda executable, argv, environment: captured.update(
            {"executable": executable, "argv": argv, "environment": environment}
        ),
    )

    run_executive(
        workspace,
        "CEO",
        str(session["session_id"]),
        int(session["epoch"]),
        model="GPT-5.6-Sol",
        permission="default",
        resume="provider-thread",
        fork_from=None,
        advisory_prompt=None,
    )

    assert captured["argv"][:2] == ["/usr/bin/traecli", "resume"]
    assert "--permission-mode" not in captured["argv"]
    environment = captured["environment"]
    assert environment["ONECO_SESSION_ID"] == session["session_id"]
    assert environment["ONECO_PYTHON"] == sys.executable


def test_owner_resume_inherits_saved_approval_configuration(tmp_path: Path) -> None:
    argv = trae.exec_argv(
        Path("/usr/bin/traecli"),
        cwd=tmp_path,
        model="GPT-5.6-Sol",
        permission="default",
        prompt="continue",
        provider_session_id="provider-thread",
    )

    assert argv[:3] == ["/usr/bin/traecli", "exec", "resume"]
    assert "--permission-mode" not in argv


def test_owner_instruction_only_queues_while_atomic_worker_is_alive(
    monkeypatch: pytest.MonkeyPatch, active_project: tuple[Workspace, str, Path]
) -> None:
    workspace, project_id, project_root = active_project
    session = workspace.runtime.register_session(
        f"{project_id}:owner", project_root, authority_kind=AuthorityKind.OWNER
    )
    workspace.runtime.touch(
        str(session["session_id"]), int(session["epoch"]), WorkState.WORKING
    )
    monkeypatch.setattr("oneco_os.orchestrator.tmux.target_alive", lambda target: True)
    monkeypatch.setattr(
        "oneco_os.orchestrator.tmux.ensure_owner_target",
        lambda *args, **kwargs: pytest.fail("must not spawn a concurrent Owner worker"),
    )

    result = wake_owner(workspace, project_id)

    assert result["status"] == "queued"
    assert workspace.runtime.current_session(f"{project_id}:owner")["epoch"] == 1


def test_interrupted_owner_work_is_requeued(tmp_path: Path) -> None:
    workspace = Workspace.initialize(tmp_path / "company", "Acme", git_init=False)
    work_id = workspace.runtime.enqueue_work("PROJ-001:owner", "PROJ-001")
    claimed = workspace.runtime.claim_work("PROJ-001:owner")
    assert claimed is not None
    workspace.runtime.start_work(work_id, str(claimed["claim_token"]), "turn-old")

    assert workspace.runtime.requeue_interrupted_work("PROJ-001:owner") == [work_id]
    row = workspace.runtime.queued_work("PROJ-001:owner")[0]
    assert row["status"] == "queued"
    assert row["claim_token"] is None


def test_hooks_bind_provider_identity_and_do_not_end_owner_wrapper(
    monkeypatch: pytest.MonkeyPatch, active_project: tuple[Workspace, str, Path]
) -> None:
    workspace, project_id, project_root = active_project
    session = workspace.runtime.register_session(
        f"{project_id}:owner", project_root, authority_kind=AuthorityKind.OWNER
    )
    monkeypatch.setenv("ONECO_ROOT", str(workspace.root))
    monkeypatch.setenv("ONECO_IDENTITY", "untrusted-prompt-role")
    monkeypatch.setenv("ONECO_SESSION_ID", str(session["session_id"]))
    monkeypatch.setenv("ONECO_EPOCH", str(session["epoch"]))

    _handle_hook("SessionStart", {"session_id": "provider-thread", "cwd": str(project_root)})
    _handle_hook("UserPromptSubmit", {"session_id": "provider-thread"})
    _handle_hook("SessionEnd", {"session_id": "provider-thread"})

    current = workspace.runtime.current_session(f"{project_id}:owner")
    assert current["provider_session_id"] == "provider-thread"
    assert current["lifecycle"] == "live"
    assert current["work_state"] == "idle"


def test_runtime_authority_blocks_secretary_mutation_and_cross_role_message(
    workspace: Workspace,
) -> None:
    session = workspace.runtime.register_session(
        "CEO:secretary:pricing", workspace.root, authority_kind=AuthorityKind.SECRETARY
    )
    context = resolve_agent_context(
        {"ONECO_ROOT": str(workspace.root), "ONECO_SESSION_ID": str(session["session_id"])}
    )

    with pytest.raises(AuthorizationError):
        agent_tools.project_create(context, "Unauthorized")
    with pytest.raises(PermissionError, match="only canonical CEO"):
        agent_tools.message_send(context, "CTO", "advice", "No")
    sent = agent_tools.message_send(context, "CEO", "advice", "Try the narrow path")
    assert sent["message_id"].startswith("msg_")


def test_bridge_snapshot_is_read_only_operational_view(
    active_project: tuple[Workspace, str, Path]
) -> None:
    workspace, project_id, _ = active_project
    snapshot = BridgeAPI(workspace).snapshot()

    assert snapshot["company"] == "Test Company"
    assert snapshot["projects"][0]["id"] == project_id
    assert "scores" not in snapshot
    assert "leaderboard" not in snapshot


def test_bridge_removes_socket_when_native_loop_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Event:
        def __iadd__(self, callback: object) -> Event:
            return self

    window = SimpleNamespace(
        events=SimpleNamespace(closed=Event()),
        show=lambda: None,
        restore=lambda: None,
    )
    fake_webview = SimpleNamespace(
        create_window=lambda *args, **kwargs: window,
        start=lambda **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setattr("oneco_os.bridge.collect_health", lambda workspace: [])

    with tempfile.TemporaryDirectory(prefix="oneco-", dir="/tmp") as root:
        workspace = Workspace.initialize(Path(root), "Bridge Test", git_init=False)
        with pytest.raises(KeyboardInterrupt):
            run_bridge(workspace, launch_executives=False)

        assert not (workspace.root / ".oneco" / "bridge.sock").exists()


def test_health_parses_real_text_plugin_list(
    monkeypatch: pytest.MonkeyPatch, workspace: Workspace
) -> None:
    monkeypatch.setattr(health.ghostty, "health", lambda: {"state": "ok", "diagnostic": "test"})
    monkeypatch.setattr(health.trae, "find_trae", lambda: Path("/usr/bin/traecli"))
    monkeypatch.setattr(health.shutil, "which", lambda command: f"/usr/bin/{command}")

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        if argv[1:3] == ["plugin", "list"]:
            return SimpleNamespace(returncode=0, stdout="Plugins:\n\n\u2713 oneco\n  id: oneco@local\n", stderr="")
        if argv[1:4] == ["mcp", "get", "oneco"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "transport": {
                            "env_vars": [
                                "ONECO_ROOT",
                                "ONECO_IDENTITY",
                                "ONECO_SESSION_ID",
                                "ONECO_EPOCH",
                                "ONECO_PYTHON",
                            ]
                        }
                    }
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    rows = health.collect(workspace, persist=False)

    plugin = next(row for row in rows if row["name"] == "oneco-plugin")
    session_env = next(row for row in rows if row["name"] == "oneco-mcp-session-env")
    assert plugin["state"] == "ok"
    assert session_env == {
        "name": "oneco-mcp-session-env",
        "source": "trae",
        "state": "ok",
        "diagnostic": "ONECO session environment passthrough configured",
    }


def test_health_rejects_plugin_without_mcp_session_environment(
    monkeypatch: pytest.MonkeyPatch, workspace: Workspace
) -> None:
    monkeypatch.setattr(health.ghostty, "health", lambda: {"state": "ok", "diagnostic": "test"})
    monkeypatch.setattr(health.trae, "find_trae", lambda: Path("/usr/bin/traecli"))
    monkeypatch.setattr(health.shutil, "which", lambda command: f"/usr/bin/{command}")

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        if argv[1:3] == ["plugin", "list"]:
            return SimpleNamespace(returncode=0, stdout="\u2713 oneco\n", stderr="")
        if argv[1:4] == ["mcp", "get", "oneco"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"transport": {"env_vars": ["ONECO_ROOT"]}}),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    rows = health.collect(workspace, persist=False)

    session_env = next(row for row in rows if row["name"] == "oneco-mcp-session-env")
    assert session_env["state"] == "degraded"
    assert session_env["diagnostic"] == (
        "missing MCP environment passthrough: "
        "ONECO_EPOCH, ONECO_IDENTITY, ONECO_PYTHON, ONECO_SESSION_ID"
    )


def test_oneco_plugin_declares_mcp_session_environment() -> None:
    plugin = Path(__file__).resolve().parents[1] / "adapters" / "trae-plugin" / "oneco"
    config = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))

    assert config["mcpServers"]["oneco"]["env_vars"] == [
        "ONECO_ROOT",
        "ONECO_IDENTITY",
        "ONECO_SESSION_ID",
        "ONECO_EPOCH",
        "ONECO_PYTHON",
    ]


def test_advisory_context_is_explicitly_noncanonical(workspace: Workspace) -> None:
    session = workspace.runtime.register_session(
        "CTO:secretary:alternate", workspace.root, authority_kind=AuthorityKind.SECRETARY
    )
    context = AgentContext(
        workspace,
        str(session["session_id"]),
        "CTO:secretary:alternate",
        int(session["epoch"]),
        "secretary",
        None,
    )
    assert context.canonical_role is None


@pytest.mark.anyio
async def test_mcp_stdio_lists_tools_and_enforces_runtime_context(workspace: Workspace) -> None:
    session = workspace.runtime.register_session("CEO", workspace.root)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "oneco_os.cli", "mcp", "serve"],
        cwd=workspace.root,
        env={
            "ONECO_ROOT": str(workspace.root),
            "ONECO_SESSION_ID": str(session["session_id"]),
        },
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert {"oneco_company_snapshot", "oneco_project_create", "oneco_owner_instruct"} <= names
            result = await client.call_tool("oneco_company_snapshot", {})
            assert not result.isError
            assert result.structuredContent["company"]["company_name"] == "Test Company"
