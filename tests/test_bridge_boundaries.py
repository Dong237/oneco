from __future__ import annotations

import json
import os
import plistlib
import shlex
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from typer.testing import CliRunner

from oneco_os import agent_tools, bridge, health
from oneco_os.adapters import ghostty, tmux, trae
from oneco_os.authority import AgentContext, resolve_agent_context
from oneco_os.bridge import BridgeAPI, run_bridge
from oneco_os.bridge import socket_path as bridge_socket_path
from oneco_os.cli import _handle_hook, app
from oneco_os.company_services import record_signal
from oneco_os.errors import AuthorizationError, StaleSessionError, WorkspaceError
from oneco_os.launcher import create_launcher
from oneco_os.models import AuthorityKind, WorkState
from oneco_os.orchestrator import branch_executive, open_executive, run_executive, wake_owner
from oneco_os.supervisor import supervision_tick
from oneco_os.workspace import Workspace

runner = CliRunner()


def _bind_fake_executive(
    workspace: Workspace,
    session_id: str,
    epoch: int,
    launch_nonce: str,
    **_: object,
) -> bool:
    workspace.runtime.bind_process(
        session_id,
        epoch,
        os.getpid(),
        launch_nonce=launch_nonce,
    )
    return True


def test_create_is_one_scriptable_company_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
    monkeypatch.setattr(
        "oneco_os.launcher.sys.executable", "/Applications/Python Custom/bin/python"
    )

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
        if source == ghostty.OPEN_WINDOW_SCRIPT:
            return '{"window_id":"42","terminal_id":"9"}'
        assert source == ghostty.FOCUS_SCRIPT
        return "focused"

    monkeypatch.setattr(ghostty, "_run_applescript", fake_run)
    monkeypatch.setattr(ghostty, "_active_space_id", lambda: 647)
    monkeypatch.setattr(ghostty, "_space_type", lambda space_id: 0)
    snapshots = iter(({41}, {41, 42}))
    monkeypatch.setattr(ghostty, "_ghostty_window_ids", lambda: next(snapshots))
    monkeypatch.setattr(ghostty, "_move_window_to_space", lambda window_id, space_id: None)
    result = ghostty.open_window(
        tmp_path, ["/bin/echo", "hello; touch /tmp/no"], title="CEO'; nope"
    )

    assert result["window_id"] == "42"
    assert captured[0] == str(tmp_path.resolve())
    outer = shlex.split(captured[1])
    assert outer[:2] == ["/bin/sh", "-lc"]
    assert "exec /bin/echo 'hello; touch /tmp/no'" in outer[2]
    assert "CEO'" in outer[2]


def test_ghostty_window_is_moved_to_invocation_space_before_focus(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    timeline: list[object] = []
    snapshots = iter(({41}, {41, 42}))

    def fake_run(source: str, *arguments: str) -> str:
        if source == ghostty.OPEN_WINDOW_SCRIPT:
            timeline.append("open")
            return '{"window_id":"oneco-window","terminal_id":"terminal-1"}'
        assert source == ghostty.FOCUS_SCRIPT
        timeline.append(("focus", arguments[0]))
        return "focused"

    monkeypatch.setattr(ghostty, "_run_applescript", fake_run)
    monkeypatch.setattr(ghostty, "_active_space_id", lambda: 647, raising=False)
    monkeypatch.setattr(ghostty, "_space_type", lambda space_id: 0)
    monkeypatch.setattr(
        ghostty,
        "_ghostty_window_ids",
        lambda: next(snapshots),
        raising=False,
    )
    monkeypatch.setattr(
        ghostty,
        "_move_window_to_space",
        lambda window_id, space_id: timeline.append(("move", window_id, space_id)),
        raising=False,
    )

    result = ghostty.open_window(tmp_path, ["/bin/echo", "hello"], title="OneCo QA CEO")

    assert result == {"window_id": "oneco-window", "terminal_id": "terminal-1"}
    assert timeline == ["open", ("move", 42, 647), ("focus", "oneco-window")]
    assert "activate window newWindow" not in ghostty.OPEN_WINDOW_SCRIPT


def test_ghostty_window_closes_exact_new_window_when_placement_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_run(source: str, *arguments: str) -> str:
        calls.append((source, arguments))
        if source == ghostty.OPEN_WINDOW_SCRIPT:
            return '{"window_id":"oneco-window","terminal_id":"terminal-1"}'
        assert source == ghostty.CLOSE_WINDOW_SCRIPT
        return "closed"

    monkeypatch.setattr(ghostty, "_run_applescript", fake_run)
    monkeypatch.setattr(ghostty, "_active_space_id", lambda: 647, raising=False)
    monkeypatch.setattr(ghostty, "_space_type", lambda space_id: 0)
    monkeypatch.setattr(ghostty, "_ghostty_window_ids", lambda: {41}, raising=False)

    with pytest.raises(WorkspaceError, match="current macOS Space"):
        ghostty.open_window(tmp_path, ["/bin/echo", "hello"], title="OneCo QA CEO")

    assert calls[-1] == (ghostty.CLOSE_WINDOW_SCRIPT, ("oneco-window",))


def test_ghostty_window_rejects_fullscreen_space_before_creating_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ghostty, "_active_space_id", lambda: 638)
    monkeypatch.setattr(ghostty, "_space_type", lambda space_id: 4)
    monkeypatch.setattr(
        ghostty,
        "_run_applescript",
        lambda *args: pytest.fail("fullscreen rejection must happen before AppleScript"),
    )

    with pytest.raises(WorkspaceError, match="Exit full screen"):
        ghostty.open_window(tmp_path, ["/bin/echo", "hello"], title="OneCo QA CEO")


def test_ghostty_space_move_uses_bridged_operation_and_verifies_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class Operation:
        @classmethod
        def alloc(cls) -> Operation:
            calls.append("alloc")
            return cls()

        def initWithWindows_spaceID_(
            self, windows: list[int], space_id: int
        ) -> Operation:
            calls.append(("init", windows, space_id))
            return self

        def performWithWMBridgeDelegate(self) -> None:
            calls.append("perform")

    monkeypatch.setitem(
        sys.modules,
        "Quartz",
        SimpleNamespace(SLSBridgedMoveWindowsToManagedSpaceOperation=Operation),
    )
    monkeypatch.setattr(ghostty, "_window_space_ids", lambda window_id: {647})

    ghostty._move_window_to_space(42, 647)

    assert calls == ["alloc", ("init", [42], 647), "perform"]


def test_ghostty_space_move_fails_when_membership_does_not_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Operation:
        @classmethod
        def alloc(cls) -> Operation:
            return cls()

        def initWithWindows_spaceID_(
            self, windows: list[int], space_id: int
        ) -> Operation:
            return self

        def performWithWMBridgeDelegate(self) -> None:
            return None

    monkeypatch.setitem(
        sys.modules,
        "Quartz",
        SimpleNamespace(SLSBridgedMoveWindowsToManagedSpaceOperation=Operation),
    )
    monkeypatch.setattr(ghostty, "_window_space_ids", lambda window_id: {142})
    monkeypatch.setattr(ghostty, "SPACE_MOVE_TIMEOUT_SECONDS", 0)

    with pytest.raises(WorkspaceError, match="did not place"):
        ghostty._move_window_to_space(42, 647)


def test_ghostty_applescript_timeout_fails_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="osascript", timeout=10)

    monkeypatch.setattr(ghostty.subprocess, "run", timeout)

    with pytest.raises(WorkspaceError, match="timed out after 10s"):
        ghostty._run_applescript("return true")


def test_trae_isolation_replaces_unrelated_mcp_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / ".trae"
    home.mkdir()
    (home / "traecli.toml").write_text(
        '[mcp_servers.broken]\ntransport = "invalid"\n'
        '[plugins."oneco@local"]\nenabled = true\n'
        '[plugins."oneco@local".mcp_servers.oneco.tools.oneco_message_send]\n'
        'approval_mode = "approve"\n'
        '[plugins."other@local"]\nenabled = true\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("TRAE_HOME", str(home))

    workspace = tmp_path / "fresh.company"
    argv = trae.isolated_config_argv(workspace)

    assert "mcp_servers.broken.enabled=false" in argv
    assert "plugins.other@local.enabled=false" in argv
    assert "plugins.oneco@local.enabled=false" not in argv
    assert (
        'plugins.oneco@local.mcp_servers.oneco.default_tools_approval_mode="auto"' in argv
    )
    assert (
        'plugins.oneco@local.mcp_servers.oneco.tools.oneco_message_send.approval_mode="auto"'
        in argv
    )
    assert f'projects={{"{workspace.resolve()}"={{trust_level="trusted"}}}}' in argv


def test_trae_workspace_trust_does_not_depend_on_existing_user_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "missing-trae-home"
    workspace = tmp_path / "fresh.company"
    monkeypatch.setenv("TRAE_HOME", str(home))

    argv = trae.isolated_config_argv(workspace)

    assert f'projects={{"{workspace.resolve()}"={{trust_level="trusted"}}}}' in argv
    assert (
        'plugins.oneco@local.mcp_servers.oneco.default_tools_approval_mode="auto"' in argv
    )


def test_cockpit_space_behavior_is_dispatched_to_cocoa_main_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class Native:
        def windowNumber(self) -> int:
            return 42

        def makeKeyAndOrderFront_(self, sender: object | None) -> None:
            calls.append(("front", sender))

        def orderFrontRegardless(self) -> None:
            calls.append("front-regardless")

    application = SimpleNamespace(
        setActivationPolicy_=lambda value: calls.append(("policy", value)),
        unhide_=lambda value: calls.append(("unhide", value)),
        activateIgnoringOtherApps_=lambda value: calls.append(("activate", value)),
    )
    application_class = SimpleNamespace(sharedApplication=lambda: application)

    fake_helper = SimpleNamespace(callAfter=lambda callback: (calls.append("scheduled"), callback()))
    monkeypatch.setitem(sys.modules, "PyObjCTools", SimpleNamespace(AppHelper=fake_helper))
    monkeypatch.setitem(
        sys.modules,
        "AppKit",
        SimpleNamespace(
            NSApplication=application_class,
        ),
    )
    monkeypatch.setattr(bridge.threading, "current_thread", lambda: object())
    monkeypatch.setattr(bridge.threading, "main_thread", lambda: object())
    monkeypatch.setattr(
        bridge.ghostty,
        "_active_space_id",
        lambda: calls.append("active-space") or 692,
    )
    monkeypatch.setattr(
        bridge.ghostty,
        "_move_window_to_space",
        lambda window_id, space_id: calls.append(("move", window_id, space_id)),
    )

    native = Native()
    bridge._move_native_window_to_active_space(
        SimpleNamespace(native=native, show=lambda: calls.append("show"), restore=lambda: calls.append("restore"))
    )

    assert calls == [
        "active-space",
        "scheduled",
        ("move", 42, 692),
        ("unhide", None),
        "show",
        "restore",
        ("front", None),
        "front-regardless",
        ("activate", True),
    ]


def test_cockpit_uses_normal_window_stacking_and_managed_spaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class Native:
        def styleMask(self) -> int:
            return 136

        def collectionBehavior(self) -> int:
            return 262545

        def setStyleMask_(self, value: int) -> None:
            calls.append(("style", value))

        def setFloatingPanel_(self, value: bool) -> None:
            calls.append(("floating", value))

        def setHidesOnDeactivate_(self, value: bool) -> None:
            calls.append(("hides", value))

        def setCollectionBehavior_(self, value: int) -> None:
            calls.append(("behavior", value))

        def setLevel_(self, value: int) -> None:
            calls.append(("level", value))

    monkeypatch.setitem(
        sys.modules,
        "AppKit",
        SimpleNamespace(
            NSNormalWindowLevel=0,
            NSWindowCollectionBehaviorCanJoinAllApplications=262144,
            NSWindowCollectionBehaviorCanJoinAllSpaces=1,
            NSWindowCollectionBehaviorFullScreenAuxiliary=256,
            NSWindowCollectionBehaviorStationary=16,
            NSWindowCollectionBehaviorManaged=4,
            NSWindowStyleMaskNonactivatingPanel=128,
        ),
    )

    bridge._prepare_native_window(SimpleNamespace(native=Native()))

    assert calls == [
        ("level", 0),
        ("style", 8),
        ("floating", False),
        ("hides", False),
        ("behavior", 132),
    ]


def test_cockpit_installs_regular_nswindow_before_webview_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class Panel:
        pass

    application = SimpleNamespace(
        setActivationPolicy_=lambda value: calls.append(("policy", value))
    )
    browser_view = SimpleNamespace(WindowHost=object)
    monkeypatch.setitem(
        sys.modules,
        "AppKit",
        SimpleNamespace(
            NSApplication=SimpleNamespace(sharedApplication=lambda: application),
            NSApplicationActivationPolicyRegular=0,
            NSWindow=Panel,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "webview.platforms.cocoa",
        SimpleNamespace(BrowserView=browser_view),
    )
    monkeypatch.setattr(bridge, "_COCOA_WINDOW_HOST", None)

    bridge._configure_cocoa_window_host()

    assert calls == [("policy", 0)]
    assert issubclass(browser_view.WindowHost, Panel)
    panel = browser_view.WindowHost()
    panel.focus = True
    assert panel.canBecomeKeyWindow() is True


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
    monkeypatch.setattr(
        "oneco_os.orchestrator._wait_for_executive_process", _bind_fake_executive
    )

    first = open_executive(workspace, "CEO")
    second = open_executive(workspace, "CEO")

    assert first["status"] == "opened"
    assert second["status"] == "focused"
    assert len(opened) == 1
    assert opened[0][2] == "OneCo \u00b7 Test Company \u00b7 CEO"
    assert focused == ["window-1"]
    assert workspace.runtime.current_session("CEO")["epoch"] == 1


def test_cpo_is_persistent_and_resumes_exact_thread_without_executive_authority(
    monkeypatch: pytest.MonkeyPatch, workspace: Workspace
) -> None:
    session = workspace.runtime.register_session(
        "CPO",
        workspace.root,
        provider_session_id="cpo-thread",
        authority_kind=AuthorityKind.ADVISORY,
        terminal_transport="ghostty",
        terminal_handle="old-window",
    )
    workspace.runtime.mark_lost(str(session["session_id"]), int(session["epoch"]))
    opened: list[list[str]] = []
    monkeypatch.setattr(ghostty, "is_alive", lambda handle: False)
    monkeypatch.setattr(
        ghostty,
        "open_window",
        lambda cwd, command, title=None: opened.append(command) or {"window_id": "cpo-window"},
    )
    monkeypatch.setattr(
        "oneco_os.orchestrator._wait_for_executive_process", _bind_fake_executive
    )

    result = open_executive(workspace, "CPO")

    assert result["session"]["session_id"] == session["session_id"]
    assert "--resume" in opened[0]
    assert "cpo-thread" in opened[0]
    current = workspace.runtime.current_session("CPO")
    assert current["authority_kind"] == "advisory"
    assert current["identity_kind"] == "persistent_role"


def test_executive_discussion_branch_forks_exact_thread_as_fenced_advisor(
    monkeypatch: pytest.MonkeyPatch, workspace: Workspace
) -> None:
    parent = workspace.runtime.register_session(
        "CEO",
        workspace.root,
        provider_session_id="ceo-thread-exact",
        authority_kind=AuthorityKind.CANONICAL,
    )
    opened: list[list[str]] = []
    monkeypatch.setattr(
        ghostty,
        "open_window",
        lambda cwd, command, title=None: (
            opened.append(command) or {"window_id": "discussion-window"}
        ),
    )
    monkeypatch.setattr(
        "oneco_os.orchestrator._wait_for_executive_process", _bind_fake_executive
    )

    result = branch_executive(workspace, "CEO", "pricing review")

    assert "--fork-from" in opened[0]
    assert opened[0][opened[0].index("--fork-from") + 1] == "ceo-thread-exact"
    row = workspace.runtime.current_session(result["logical_id"])
    assert row["authority_kind"] == AuthorityKind.SECRETARY
    assert row["identity_kind"] == "delegate"
    assert row["parent_session_id"] == parent["session_id"]
    context = resolve_agent_context(
        {"ONECO_ROOT": str(workspace.root), "ONECO_SESSION_ID": str(row["id"])}
    )
    assert agent_tools.message_send(context, "CEO", "advice", "Keep the pricing test narrow")
    with pytest.raises(PermissionError, match="only canonical CEO"):
        agent_tools.message_send(context, "CTO", "decision", "Bypass the CEO")


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
        lambda cwd, command, title=None: opened.append(command) or {"window_id": "new-window"},
    )
    monkeypatch.setattr(
        "oneco_os.orchestrator._wait_for_executive_process", _bind_fake_executive
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

    assert captured["argv"][0] == "/usr/bin/traecli"
    assert "--no-daemon" in captured["argv"]
    assert captured["argv"][captured["argv"].index("resume") + 1] == "provider-thread"
    assert "--dangerously-bypass-approvals-and-sandbox" in captured["argv"]
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

    assert argv[0] == "/usr/bin/traecli"
    assert argv[argv.index("exec") : argv.index("exec") + 2] == ["exec", "resume"]
    assert "--no-daemon" not in argv
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "--permission-mode" not in argv


def test_owner_tmux_uses_a_company_scoped_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        if "has-session" in argv:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tmux, "require_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(tmux.subprocess, "run", run)

    target = tmux.ensure_owner_target(
        "oneco-isolated-company-owners",
        "proj-001",
        tmp_path,
        ["oneco", "internal", "owner-turn"],
    )

    assert target == "oneco-isolated-company-owners:proj-001"
    assert calls
    assert all(
        argv[1:3] == ["-L", "oneco-isolated-company-owners"] for argv in calls
    )


def test_session_reconcile_checks_the_company_scoped_tmux_server(
    monkeypatch: pytest.MonkeyPatch, workspace: Workspace
) -> None:
    target = "oneco-isolated-company-owners:proj-001"
    session = workspace.runtime.register_session(
        "PROJ-001:owner",
        workspace.root,
        authority_kind=AuthorityKind.OWNER,
        terminal_transport="tmux",
        terminal_handle=target,
        tmux_target=target,
    )
    calls: list[list[str]] = []
    monkeypatch.setattr("oneco_os.cli.shutil.which", lambda command: "/usr/bin/tmux")
    monkeypatch.setattr(
        "oneco_os.cli.subprocess.run",
        lambda argv, **kwargs: (
            calls.append(argv) or SimpleNamespace(returncode=0, stdout="%1\n", stderr="")
        ),
    )

    result = runner.invoke(
        app,
        ["session", "reconcile", "--root", str(workspace.root), "--stale-after", "0", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert str(session["session_id"]) not in json.loads(result.output)["lost"]
    assert calls[0][1:3] == ["-L", "oneco-isolated-company-owners"]


def test_owner_instruction_only_queues_while_atomic_worker_is_alive(
    monkeypatch: pytest.MonkeyPatch, active_project: tuple[Workspace, str, Path]
) -> None:
    workspace, project_id, project_root = active_project
    session = workspace.runtime.register_session(
        f"{project_id}:owner", project_root, authority_kind=AuthorityKind.OWNER
    )
    workspace.runtime.touch(str(session["session_id"]), int(session["epoch"]), WorkState.WORKING)
    workspace.runtime.enqueue_work(f"{project_id}:owner", project_id)
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


def test_due_supervision_notifies_cto_without_interrupting_owner(
    monkeypatch: pytest.MonkeyPatch, active_project: tuple[Workspace, str, Path]
) -> None:
    workspace, project_id, _ = active_project
    workspace.runtime.set_supervision_due(
        project_id, next_review_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    )
    wakes: list[str] = []
    monkeypatch.setattr(
        "oneco_os.supervisor.wake_owner",
        lambda workspace, project_id, permission="default": wakes.append(project_id),
    )

    supervision_tick(workspace)

    assert wakes == []
    assert workspace.runtime.inbox("CTO")[0]["kind"] == "supervision_due"
    work = workspace.runtime.queued_role_work("CTO")
    assert len(work) == 1
    assert work[0]["kind"] == "scheduled_review"
    with workspace.runtime.connect() as connection:
        row = connection.execute(
            "SELECT progress_requested_at, next_review_at FROM supervision WHERE project_id=?",
            (project_id,),
        ).fetchone()
    assert row["progress_requested_at"]
    assert datetime.fromisoformat(row["next_review_at"]) > datetime.now(UTC)


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


def test_cpo_and_research_delegate_fail_closed_outside_advisory_boundary(
    workspace: Workspace,
) -> None:
    cpo_session = workspace.runtime.register_session(
        "CPO", workspace.root, authority_kind=AuthorityKind.ADVISORY
    )
    cpo = resolve_agent_context(
        {"ONECO_ROOT": str(workspace.root), "ONECO_SESSION_ID": str(cpo_session["session_id"])}
    )

    with pytest.raises(AuthorizationError):
        agent_tools.project_create(cpo, "Unauthorized")
    with pytest.raises(AuthorizationError):
        agent_tools.project_endorse(cpo, "PROJ-001", "No")
    with pytest.raises(AuthorizationError, match="message route"):
        agent_tools.message_send(cpo, "PROJ-001:owner", "instruction", "No")

    delegate_session = workspace.runtime.register_session(
        "CPO:secretary:research",
        workspace.root,
        authority_kind=AuthorityKind.SECRETARY,
    )
    delegate = resolve_agent_context(
        {
            "ONECO_ROOT": str(workspace.root),
            "ONECO_SESSION_ID": str(delegate_session["session_id"]),
        }
    )
    with pytest.raises(PermissionError, match="only canonical CPO"):
        agent_tools.message_send(delegate, "CEO", "research", "No")
    assert agent_tools.message_send(delegate, "CPO", "research", "Evidence")[
        "message_id"
    ].startswith("msg_")


def test_replaced_cpo_session_cannot_resolve_authority(workspace: Workspace) -> None:
    stale = workspace.runtime.register_session(
        "CPO", workspace.root, authority_kind=AuthorityKind.ADVISORY
    )
    workspace.runtime.register_session(
        "CPO", workspace.root, authority_kind=AuthorityKind.ADVISORY, replace=True
    )

    with pytest.raises(Exception, match="stale session"):
        resolve_agent_context(
            {"ONECO_ROOT": str(workspace.root), "ONECO_SESSION_ID": str(stale["session_id"])}
        )


def test_replaced_cpo_context_cannot_write_a_bet(workspace: Workspace) -> None:
    signal = record_signal(
        workspace,
        "Assigned signal",
        "A Board-assigned signal for stale-session fencing.",
        [{"kind": "board_idea", "title": "Board prompt", "uri": "board://stale-cpo"}],
    )
    stale_session = workspace.runtime.register_session(
        "CPO", workspace.root, authority_kind=AuthorityKind.ADVISORY
    )
    stale_context = resolve_agent_context(
        {
            "ONECO_ROOT": str(workspace.root),
            "ONECO_SESSION_ID": str(stale_session["session_id"]),
        }
    )
    workspace.runtime.register_session(
        "CPO", workspace.root, authority_kind=AuthorityKind.ADVISORY, replace=True
    )

    with pytest.raises(StaleSessionError, match="stale session"):
        agent_tools.bet_create(
            stale_context,
            "Must not be written",
            [signal.signal_id],
            "# Product Bet Packet\n",
        )


def test_replaced_executive_context_cannot_acknowledge_messages(workspace: Workspace) -> None:
    stale_session = workspace.runtime.register_session("CEO", workspace.root)
    stale_context = resolve_agent_context(
        {
            "ONECO_ROOT": str(workspace.root),
            "ONECO_SESSION_ID": str(stale_session["session_id"]),
        }
    )
    message_id = workspace.runtime.send_message("CTO", "CEO", "context", "Review this")
    workspace.runtime.register_session("CEO", workspace.root, replace=True)

    with pytest.raises(StaleSessionError, match="stale session"):
        agent_tools.message_ack(stale_context, message_id)


def test_replaced_executive_context_cannot_request_an_action(workspace: Workspace) -> None:
    project = workspace.register_project("Stale Action", Path("stale-action"), initialize_git=False)
    stale_session = workspace.runtime.register_session("CEO", workspace.root)
    stale_context = resolve_agent_context(
        {
            "ONECO_ROOT": str(workspace.root),
            "ONECO_SESSION_ID": str(stale_session["session_id"]),
        }
    )
    workspace.runtime.register_session("CEO", workspace.root, replace=True)

    with pytest.raises(StaleSessionError, match="stale session"):
        agent_tools.action_request(
            stale_context,
            project.id,
            "product",
            "Must not be requested",
            "Would mutate external state",
            True,
        )


def test_owner_cannot_request_action_for_another_project(workspace: Workspace) -> None:
    first = workspace.register_project("First", Path("first"), initialize_git=False)
    second = workspace.register_project("Second", Path("second"), initialize_git=False)
    session = workspace.runtime.register_session(
        f"{first.id}:owner",
        workspace.project_path(first.id),
        authority_kind=AuthorityKind.OWNER,
    )
    context = resolve_agent_context(
        {"ONECO_ROOT": str(workspace.root), "ONECO_SESSION_ID": str(session["session_id"])}
    )

    with pytest.raises(AuthorizationError, match="cannot operate"):
        agent_tools.action_request(
            context,
            second.id,
            "technical",
            "Change infrastructure",
            "Affects another product",
            True,
        )


def test_owner_cannot_message_another_owner(workspace: Workspace) -> None:
    first = workspace.register_project("First", Path("first"), initialize_git=False)
    second = workspace.register_project("Second", Path("second"), initialize_git=False)
    session = workspace.runtime.register_session(
        f"{first.id}:owner",
        workspace.project_path(first.id),
        authority_kind=AuthorityKind.OWNER,
    )
    context = resolve_agent_context(
        {"ONECO_ROOT": str(workspace.root), "ONECO_SESSION_ID": str(session["session_id"])}
    )

    with pytest.raises(AuthorizationError, match="message route"):
        agent_tools.message_send(
            context,
            f"{second.id}:owner",
            "instruction",
            "Do work for my project",
            project_id=second.id,
        )


def test_bridge_snapshot_is_read_only_operational_view(
    active_project: tuple[Workspace, str, Path],
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
    monkeypatch.setattr("oneco_os.bridge._configure_cocoa_window_host", lambda: None)
    monkeypatch.setattr("oneco_os.bridge._active_display_count", lambda: 1)
    monkeypatch.setattr("oneco_os.bridge.ghostty._active_space_id", lambda: 647)
    monkeypatch.setattr("oneco_os.bridge.collect_health", lambda workspace: [])

    with tempfile.TemporaryDirectory(prefix="oneco-", dir="/tmp") as root:
        workspace = Workspace.initialize(Path(root), "Bridge Test", git_init=False)
        with pytest.raises(KeyboardInterrupt):
            run_bridge(workspace, launch_executives=False)

        assert not bridge_socket_path(workspace).exists()


def test_bridge_applies_all_spaces_behavior_after_native_window_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Event:
        def __init__(self) -> None:
            self.callbacks: list[object] = []

        def __iadd__(self, callback: object) -> Event:
            self.callbacks.append(callback)
            return self

    before_show = Event()
    loaded = Event()
    shown = Event()
    window = SimpleNamespace(
        events=SimpleNamespace(
            before_show=before_show,
            loaded=loaded,
            shown=shown,
            closed=Event(),
        ),
        native=None,
        show=lambda: None,
        restore=lambda: None,
    )

    placement_calls: list[tuple[str, object | None]] = []

    def start(**kwargs: object) -> None:
        assert kwargs["gui"] == "cocoa"
        before_show.callbacks[0]()
        window.native = object()
        loaded.callbacks[0]()
        shown.callbacks[0]()
        raise KeyboardInterrupt

    fake_webview = SimpleNamespace(
        create_window=lambda *args, **kwargs: window,
        start=start,
    )
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setattr("oneco_os.bridge._configure_cocoa_window_host", lambda: None)
    monkeypatch.setattr("oneco_os.bridge._active_display_count", lambda: 1)
    monkeypatch.setattr("oneco_os.bridge.ghostty._active_space_id", lambda: 647)
    monkeypatch.setattr("oneco_os.bridge.collect_health", lambda workspace: [])
    monkeypatch.setattr(
        "oneco_os.bridge._prepare_native_window",
        lambda current: placement_calls.append(("prepare", current.native)),
    )
    monkeypatch.setattr(
        "oneco_os.bridge._move_native_window_to_active_space",
        lambda current, target_space=None: placement_calls.append(
            ("move", current.native, target_space)
        ),
    )

    with tempfile.TemporaryDirectory(prefix="oneco-", dir="/tmp") as root:
        workspace = Workspace.initialize(Path(root), "Bridge Placement Test", git_init=False)
        with pytest.raises(KeyboardInterrupt):
            run_bridge(workspace, launch_executives=False)

    assert len(before_show.callbacks) == 1
    assert len(loaded.callbacks) == 1
    assert len(shown.callbacks) == 1
    assert placement_calls == [
        ("prepare", None),
        ("prepare", window.native),
        ("move", window.native, 647),
        ("prepare", window.native),
        ("move", window.native, 647),
    ]


def test_health_parses_real_text_plugin_list(
    monkeypatch: pytest.MonkeyPatch, workspace: Workspace
) -> None:
    monkeypatch.setattr(health.ghostty, "health", lambda: {"state": "ok", "diagnostic": "test"})
    monkeypatch.setattr(health.trae, "find_trae", lambda: Path("/usr/bin/traecli"))
    monkeypatch.setattr(health.shutil, "which", lambda command: f"/usr/bin/{command}")

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        if argv[1:3] == ["plugin", "list"]:
            return SimpleNamespace(
                returncode=0, stdout="Plugins:\n\n\u2713 oneco\n  id: oneco@local\n", stderr=""
            )
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
            assert {
                "oneco_company_snapshot",
                "oneco_bet_create",
                "oneco_bet_submit",
                "oneco_project_promote",
                "oneco_owner_instruct",
                "oneco_release_candidate_record",
                "oneco_release_endorse",
            } <= names
            result = await client.call_tool("oneco_company_snapshot", {})
            assert not result.isError
            assert result.structuredContent["company"]["company_name"] == "Test Company"
