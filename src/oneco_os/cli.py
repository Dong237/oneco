"""Command-line interface for the local-first OneCo operating protocol."""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from . import __version__
from .adapters import tmux, trae
from .errors import OneCoError, ProjectError, WorkspaceError
from .launcher import create_launcher as create_finder_launcher
from .models import Instruction, InstructionAction, ProjectState, WorkState
from .services import (
    create_action_request,
    decide_action,
    notify_live_identity,
    rebuild_runtime,
    record_checkpoint,
    record_decision,
    record_instruction,
)
from .util import company_root, load_json
from .workspace import Workspace

console = Console()
error_console = Console(stderr=True)
P = ParamSpec("P")
R = TypeVar("R")

app = typer.Typer(
    name="oneco",
    help="Operate a local-first one-person AI-native company.",
    no_args_is_help=True,
    invoke_without_command=True,
)
company_app = typer.Typer(help="Create and inspect a company workspace.")
project_app = typer.Typer(help="Register and govern product projects.")
message_app = typer.Typer(help="Send durable coordination messages.")
session_app = typer.Typer(help="Manage fenced logical-role sessions.")
checkpoint_app = typer.Typer(help="Record durable Owner checkpoints.")
decision_app = typer.Typer(help="Record durable company decisions.")
action_app = typer.Typer(help="Request and decide approval-gated external actions.")
runtime_app = typer.Typer(help="Inspect or rebuild disposable runtime state.")
host_app = typer.Typer(help="Install local host integrations.")
spec_app = typer.Typer(help="Manage the local OneCo Spec Kit workflow.")
mcp_app = typer.Typer(help="Expose authorized OneCo operations to agent hosts.")
internal_app = typer.Typer(help="Internal adapter entry points.", hidden=True)
internal_hook_app = typer.Typer(help="Internal lifecycle observations.", hidden=True)

app.add_typer(company_app, name="company")
app.add_typer(project_app, name="project")
app.add_typer(message_app, name="message")
app.add_typer(session_app, name="session")
app.add_typer(checkpoint_app, name="checkpoint")
app.add_typer(decision_app, name="decision")
app.add_typer(action_app, name="action")
app.add_typer(runtime_app, name="runtime")
app.add_typer(host_app, name="host")
app.add_typer(spec_app, name="spec")
app.add_typer(mcp_app, name="mcp")
app.add_typer(internal_app, name="internal")
internal_app.add_typer(internal_hook_app, name="hook")


def _guard(function: Callable[P, R]) -> Callable[P, R]:
    """Render expected boundary failures without leaking a Python traceback."""

    @functools.wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except typer.Exit:
            raise
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            error_console.print(f"[red]Error:[/red] invalid data: {details}")
        except (OneCoError, subprocess.SubprocessError, sqlite3.Error, OSError, ValueError) as exc:
            error_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(2)

    return wrapped


def _workspace(root: Path | None = None) -> Workspace:
    return Workspace.open(root.resolve() if root else company_root())


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _emit(value: Any, *, json_output: bool = False) -> None:
    payload = _jsonable(value)
    if json_output:
        console.print_json(json.dumps(payload, default=_jsonable))
    elif isinstance(payload, dict):
        for key, item in payload.items():
            if isinstance(item, (dict, list)):
                console.print(f"[bold]{key}[/bold]: {json.dumps(item, default=_jsonable)}")
            else:
                console.print(f"[bold]{key}[/bold]: {item}")
    else:
        console.print(payload)


def _status_payload(workspace: Workspace) -> dict[str, Any]:
    manifest = workspace.manifest()
    portfolio = workspace.portfolio()
    snapshot = workspace.runtime.snapshot()
    projects: list[dict[str, Any]] = []
    for project in portfolio.projects:
        ready, reasons = workspace.readiness(project.id)
        projects.append(
            {
                **project.model_dump(mode="json"),
                "ready": ready,
                "readiness_reasons": reasons,
            }
        )
    return {
        "company": manifest.company_name,
        "root": str(workspace.root),
        "projects": projects,
        "identities": snapshot["identities"],
        "unread": snapshot["unread"],
        "pending_actions": snapshot["pending_actions"],
    }


def _print_status(payload: dict[str, Any]) -> None:
    console.print(f"[bold]{payload['company']}[/bold]  {payload['root']}")
    projects = Table("Project", "Name", "State", "Ready", title="Projects")
    for project in payload["projects"]:
        projects.add_row(
            project["id"],
            project["name"],
            project["state"],
            "yes" if project["ready"] else "no",
        )
    console.print(projects)
    identities = Table("Identity", "Epoch", "Lifecycle", "Work", title="Sessions")
    for identity in payload["identities"]:
        identities.add_row(
            str(identity["logical_id"]),
            str(identity["epoch"]),
            str(identity.get("lifecycle") or "offline"),
            str(identity.get("work_state") or "-"),
        )
    console.print(identities)
    console.print(
        f"Unread: {payload['unread']}  Pending actions: {len(payload['pending_actions'])}"
    )


def _project_id_from_owner(logical_id: str) -> str | None:
    match = re.fullmatch(r"(PROJ-[0-9]{3,}):owner", logical_id)
    return match.group(1) if match else None


def _role_cwd(workspace: Workspace, logical_id: str) -> Path:
    project_id = _project_id_from_owner(logical_id)
    if logical_id in {"CEO", "CTO"}:
        return workspace.root
    if project_id:
        return workspace.project_path(project_id)
    raise WorkspaceError("identity must be CEO, CTO, or PROJ-NNN:owner")


def _role_window(logical_id: str) -> str:
    if logical_id in {"CEO", "CTO"}:
        return logical_id.lower()
    project_id = _project_id_from_owner(logical_id)
    if project_id:
        return project_id.lower()
    raise WorkspaceError("identity must be CEO, CTO, or PROJ-NNN:owner")


def _role_run_command(
    workspace: Workspace, logical_id: str, model: str, permission: str, target: str
) -> list[str]:
    return [
        *tmux.oneco_command(),
        "role-run",
        logical_id,
        "--root",
        str(workspace.root),
        "--model",
        model,
        "--permission",
        permission,
        "--tmux-target",
        target,
    ]


def _ensure_company_session(workspace: Workspace, permission: str = "default") -> str:
    manifest = workspace.manifest()
    if manifest.provider != "trae":
        raise WorkspaceError(f"unsupported launch provider: {manifest.provider}")
    trae.require_model(manifest.model)
    name = tmux.session_name(manifest.company_name)
    tmux.ensure_session(name, workspace.root)
    for identity in ("CEO", "CTO"):
        window = _role_window(identity)
        target = f"{name}:{window}"
        tmux.ensure_window(
            name,
            window,
            workspace.root,
            _role_run_command(workspace, identity, manifest.model, permission, target),
        )
    return name


def _focus(target: str) -> None:
    if os.environ.get("TMUX"):
        result = subprocess.run(
            [tmux.require_tmux(), "select-window", "-t", target],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        result = subprocess.run([tmux.require_tmux(), "attach-session", "-t", target], check=False)
    if result.returncode:
        error = getattr(result, "stderr", "")
        raise WorkspaceError((error or f"failed to focus {target}").strip())


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", "-V", is_eager=True),
) -> None:
    """OneCo keeps durable company truth separate from local runtime coordination."""
    if version:
        console.print(__version__)
        raise typer.Exit()


def _trae_plugin_command() -> list[str]:
    binary = trae.find_trae()
    plugin = _repository_root() / "adapters" / "trae-plugin" / "oneco"
    return [str(binary), "plugin", "install", str(plugin), "--type", "local", "--yes"]


def _install_trae_plugin() -> str:
    result = subprocess.run(
        _trae_plugin_command(), capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise WorkspaceError(result.stderr.strip() or "Trae plugin installation failed")
    return result.stdout.strip() or "Trae OneCo plugin installed"


@app.command("create")
@_guard
def create(
    path: Path | None = typer.Argument(None, help="Company folder. Asked interactively if omitted."),
    name: str | None = typer.Option(None, "--name", help="Company name."),
    language: str = typer.Option("en", "--language"),
    model: str = typer.Option(trae.DEFAULT_MODEL, "--model"),
    install_trae: bool = typer.Option(True, "--install-trae/--no-install-trae"),
    finder_launcher: bool = typer.Option(True, "--finder-launcher/--no-finder-launcher"),
    git_init: bool = typer.Option(True, "--git-init/--no-git-init"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Create a complete local company in one guided step."""
    from .util import slugify

    company_name = (name or typer.prompt("Company name")).strip()
    if not company_name:
        raise WorkspaceError("company name cannot be empty")
    if path is None:
        default_path = Path.cwd() / slugify(company_name)
        path = Path(typer.prompt("Company folder", default=str(default_path)))
    path = path.expanduser().resolve()

    # Fail before writing company state when the requested host/model cannot launch its agents.
    trae.require_model(model)
    plugin_result = _install_trae_plugin() if install_trae else "skipped"
    workspace = Workspace.initialize(
        path,
        company_name,
        language=language,
        provider="trae",
        model=model,
        terminal="tmux",
        git_init=git_init,
    )
    launcher: Path | None = None
    if finder_launcher and sys.platform == "darwin":
        launcher = create_finder_launcher(workspace)
    payload = {
        "company": company_name,
        "root": str(workspace.root),
        "trae_plugin": plugin_result,
        "launcher": str(launcher) if launcher else None,
        "next": f"oneco start --root {workspace.root}",
    }
    if json_output:
        _emit(payload, json_output=True)
        return
    console.print(f"[bold green]{company_name} is ready.[/bold green]")
    console.print(f"Company: {workspace.root}")
    if launcher:
        console.print(f"Finder app: {launcher}")
    console.print(f"Start: oneco start --root {workspace.root}")


@company_app.command("init")
@_guard
def company_init(
    path: Path = typer.Argument(..., help="New company directory."),
    name: str = typer.Option(..., "--name", help="Company name."),
    language: str = typer.Option("en", "--language"),
    provider: str = typer.Option("trae", "--provider"),
    model: str = typer.Option(trae.DEFAULT_MODEL, "--model"),
    terminal: str = typer.Option("tmux", "--terminal"),
    git_init: bool = typer.Option(True, "--git-init/--no-git-init"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    workspace = Workspace.initialize(
        path,
        name,
        language=language,
        provider=provider,
        model=model,
        terminal=terminal,
        git_init=git_init,
    )
    _emit(
        {"company": workspace.manifest().model_dump(mode="json"), "root": workspace.root},
        json_output=json_output,
    )


@company_app.command("show")
@_guard
def company_show(
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    workspace = _workspace(root)
    _emit(
        {**workspace.manifest().model_dump(mode="json"), "root": str(workspace.root)},
        json_output=json_output,
    )


@project_app.command("register")
@_guard
def project_register(
    name: str = typer.Option(..., "--name"),
    path: Path = typer.Option(..., "--path"),
    root: Path | None = typer.Option(None, "--root"),
    initialize_git: bool = typer.Option(True, "--git-init/--no-git-init"),
    scaffold: bool = typer.Option(True, "--scaffold/--no-scaffold"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    project = _workspace(root).register_project(
        name, path, initialize_git=initialize_git, scaffold=scaffold
    )
    _emit(project, json_output=json_output)


@project_app.command("list")
@_guard
def project_list(
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    projects = _workspace(root).portfolio().projects
    if json_output:
        _emit([item.model_dump(mode="json") for item in projects], json_output=True)
        return
    table = Table("ID", "Name", "Path", "State")
    for project in projects:
        table.add_row(project.id, project.name, project.path, project.state.value)
    console.print(table)


@project_app.command("status")
@_guard
def project_status(
    project_id: str = typer.Argument(...),
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    workspace = _workspace(root)
    project = workspace.find_project(project_id)
    ready, reasons = workspace.readiness(project_id)
    data = {
        **project.model_dump(mode="json"),
        "absolute_path": str(workspace.project_path(project_id)),
        "ready": ready,
        "readiness_reasons": reasons,
    }
    _emit(data, json_output=json_output)


@project_app.command("readiness")
@_guard
def project_readiness(
    project_id: str = typer.Argument(...),
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    ready, reasons = _workspace(root).readiness(project_id)
    _emit({"project_id": project_id, "ready": ready, "reasons": reasons}, json_output=json_output)
    if not ready:
        raise typer.Exit(1)


@project_app.command("endorse")
@_guard
def project_endorse(
    project_id: str = typer.Argument(...),
    actor: str = typer.Option(..., "--actor", help="CEO or CTO."),
    note: str = typer.Option(..., "--note"),
    root: Path | None = typer.Option(None, "--root"),
) -> None:
    workspace = _workspace(root)
    path = workspace.endorse_launch(project_id, actor, note)
    project = workspace.find_project(project_id)
    _emit({"project_id": project_id, "actor": actor.upper(), "state": project.state, "path": path})


@project_app.command("activate")
@_guard
def project_activate(
    project_id: str = typer.Argument(...),
    actor: str = typer.Option("BOARD", "--actor"),
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _emit(
        _workspace(root).set_state(project_id, ProjectState.ACTIVE, actor=actor),
        json_output=json_output,
    )


@project_app.command("archive")
@_guard
def project_archive(
    project_id: str = typer.Argument(...),
    actor: str = typer.Option("BOARD", "--actor"),
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _emit(
        _workspace(root).set_state(project_id, ProjectState.ARCHIVED, actor=actor),
        json_output=json_output,
    )


@project_app.command("set-state")
@_guard
def project_set_state(
    project_id: str = typer.Argument(...),
    state: ProjectState = typer.Argument(...),
    actor: str = typer.Option(..., "--actor"),
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _emit(_workspace(root).set_state(project_id, state, actor=actor), json_output=json_output)


@app.command("status")
@_guard
def status(
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    payload = _status_payload(_workspace(root))
    _emit(payload, json_output=True) if json_output else _print_status(payload)


@message_app.command("send")
@_guard
def message_send(
    sender: str = typer.Option(..., "--sender"),
    recipient: str = typer.Option(..., "--to", "--recipient"),
    kind: str = typer.Option("context", "--kind"),
    body: str = typer.Option(..., "--body"),
    project_id: str | None = typer.Option(None, "--project", "--project-id"),
    requires_ack: bool = typer.Option(False, "--requires-ack"),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
    root: Path | None = typer.Option(None, "--root"),
) -> None:
    workspace = _workspace(root)
    if project_id:
        workspace.find_project(project_id)
    message_id = workspace.runtime.send_message(
        sender,
        recipient,
        kind,
        body,
        project_id=project_id,
        requires_ack=requires_ack,
        idempotency_key=idempotency_key,
    )
    notify_live_identity(workspace, recipient, f"{kind} from {sender}: {body}")
    console.print(message_id)


def _inbox_list(recipient: str, root: Path | None, unread_only: bool, json_output: bool) -> None:
    rows = _workspace(root).runtime.inbox(recipient, unread_only=unread_only)
    if json_output:
        _emit(rows, json_output=True)
        return
    table = Table("ID", "From", "Kind", "Project", "Ack", "Body")
    for row in rows:
        table.add_row(
            row["id"],
            row["sender"],
            row["kind"],
            row.get("project_id") or "-",
            "yes" if row.get("acked_at") else "no",
            row["body"],
        )
    console.print(table)


class InboxGroup(typer.core.TyperGroup):
    """Accept both ``inbox list ROLE`` and the documented ``inbox ROLE`` form."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        if args and args[0] not in {"list", "ack", "--help", "-h"} and not args[0].startswith("-"):
            args.insert(0, "list")
        return super().parse_args(ctx, args)


inbox_app = typer.Typer(
    help="Read and acknowledge durable messages.",
    no_args_is_help=True,
    cls=InboxGroup,
)
app.add_typer(inbox_app, name="inbox")


@inbox_app.command("list")
@_guard
def inbox_list(
    recipient: str = typer.Argument(...),
    unread_only: bool = typer.Option(False, "--unread-only"),
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _inbox_list(recipient, root, unread_only, json_output)


@inbox_app.command("ack")
@_guard
def inbox_ack(
    message_id: str = typer.Argument(...),
    actor: str = typer.Option(..., "--actor"),
    root: Path | None = typer.Option(None, "--root"),
) -> None:
    _workspace(root).runtime.acknowledge(message_id, actor)
    console.print(f"acknowledged {message_id}")


def _register_session(
    logical_id: str,
    cwd: Path | None,
    provider: str,
    provider_session_id: str | None,
    tmux_target: str | None,
    pid: int | None,
    replace: bool,
    root: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    workspace = _workspace(root)
    role_cwd = cwd or _role_cwd(workspace, logical_id)
    project_id = _project_id_from_owner(logical_id)
    if project_id:
        project = workspace.find_project(project_id)
        ready, reasons = workspace.readiness(project_id)
        if project.state != ProjectState.ACTIVE or not ready:
            detail = f"state={project.state}"
            if reasons:
                detail += "; " + "; ".join(reasons)
            raise ProjectError(f"Owner session requires ready+active: {detail}")
    session = workspace.runtime.register_session(
        logical_id,
        role_cwd,
        provider=provider,
        provider_session_id=provider_session_id,
        tmux_target=tmux_target,
        pid=pid,
        replace=replace,
    )
    if project_id:
        workspace.runtime.acquire_writer(project_id, session["session_id"], session["epoch"])
    _emit(session, json_output=json_output)
    return session


@session_app.command("register")
@_guard
def session_register(
    logical_id: str = typer.Argument(...),
    cwd: Path | None = typer.Option(None, "--cwd"),
    provider: str = typer.Option("generic", "--provider"),
    provider_session_id: str | None = typer.Option(None, "--provider-session-id"),
    tmux_target: str | None = typer.Option(None, "--tmux-target"),
    pid: int | None = typer.Option(None, "--pid"),
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _register_session(
        logical_id, cwd, provider, provider_session_id, tmux_target, pid, False, root, json_output
    )


@session_app.command("takeover")
@_guard
def session_takeover(
    logical_id: str = typer.Argument(...),
    cwd: Path | None = typer.Option(None, "--cwd"),
    provider: str = typer.Option("generic", "--provider"),
    provider_session_id: str | None = typer.Option(None, "--provider-session-id"),
    tmux_target: str | None = typer.Option(None, "--tmux-target"),
    pid: int | None = typer.Option(None, "--pid"),
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _register_session(
        logical_id, cwd, provider, provider_session_id, tmux_target, pid, True, root, json_output
    )


@session_app.command("close")
@_guard
def session_close(
    session_id: str = typer.Argument(...),
    epoch: int = typer.Option(..., "--epoch"),
    lost: bool = typer.Option(False, "--lost"),
    root: Path | None = typer.Option(None, "--root"),
) -> None:
    _workspace(root).runtime.close_session(session_id, epoch, lost=lost)
    console.print(f"closed {session_id}")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@session_app.command("reconcile")
@_guard
def session_reconcile(
    stale_after: int = typer.Option(
        300, "--stale-after", min=0, help="Seconds before an unobservable session is lost."
    ),
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    workspace = _workspace(root)
    now = datetime.now(UTC)
    with workspace.runtime.connect() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT id, epoch, pid, tmux_target, last_seen_at FROM sessions WHERE lifecycle='live'"
            )
        ]
    lost: list[str] = []
    for row in rows:
        observable = False
        alive = False
        if row["pid"] is not None:
            observable = True
            alive = _pid_alive(int(row["pid"]))
        if row["tmux_target"] and shutil.which("tmux"):
            observable = True
            alive = (
                alive
                or subprocess.run(
                    [
                        tmux.require_tmux(),
                        "display-message",
                        "-p",
                        "-t",
                        row["tmux_target"],
                        "#{pane_id}",
                    ],
                    capture_output=True,
                    check=False,
                ).returncode
                == 0
            )
        age = (now - datetime.fromisoformat(row["last_seen_at"])).total_seconds()
        if not alive and (observable or age >= stale_after):
            workspace.runtime.close_session(row["id"], int(row["epoch"]), lost=True)
            lost.append(row["id"])
    _emit({"examined": len(rows), "lost": lost}, json_output=json_output)


@checkpoint_app.command("create")
@_guard
def checkpoint_create(
    project_id: str = typer.Option(..., "--project", "--project-id"),
    spec_id: str = typer.Option(..., "--spec", "--spec-id"),
    task_id: str = typer.Option(..., "--task", "--task-id"),
    result: str = typer.Option(..., "--result"),
    verification: list[str] = typer.Option(..., "--verification"),
    next_task: str = typer.Option(..., "--next-task"),
    actor: str | None = typer.Option(None, "--actor"),
    session_id: str | None = typer.Option(None, "--session-id"),
    epoch: int | None = typer.Option(None, "--epoch"),
    commit: str | None = typer.Option(None, "--commit"),
    key_files: list[str] | None = typer.Option(None, "--key-file"),
    spec_deviation: str = typer.Option("none", "--spec-deviation"),
    complexity_change: str = typer.Option("none", "--complexity-change"),
    dependency_change: str = typer.Option("none", "--dependency-change"),
    risk_or_blocker: str = typer.Option("none", "--risk-or-blocker"),
    reuse_candidate: str | None = typer.Option(None, "--reuse-candidate"),
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    workspace = _workspace(root)
    checkpoint, path = record_checkpoint(
        workspace,
        project_id,
        spec_id,
        task_id,
        result,
        verification,
        next_task,
        actor=actor or os.environ.get("ONECO_IDENTITY", f"{project_id}:owner"),
        session_id=session_id or os.environ.get("ONECO_SESSION_ID"),
        epoch=epoch
        if epoch is not None
        else (int(os.environ["ONECO_EPOCH"]) if os.environ.get("ONECO_EPOCH") else None),
        commit=commit,
        key_files=key_files,
        spec_deviation=spec_deviation,
        complexity_change=complexity_change,
        dependency_change=dependency_change,
        risk_or_blocker=risk_or_blocker,
        reuse_candidate=reuse_candidate,
    )
    _emit({"checkpoint": checkpoint.model_dump(mode="json"), "path": path}, json_output=json_output)


@app.command("instruct")
@_guard
def instruct(
    sender: str = typer.Option(..., "--sender", "--sender-authority"),
    action: InstructionAction = typer.Option(..., "--action"),
    project_id: str = typer.Option(..., "--project", "--project-id"),
    spec_id: str = typer.Option(..., "--spec", "--spec-id"),
    task_id: str = typer.Option(..., "--task", "--task-id"),
    observed: str = typer.Option(..., "--observed"),
    evidence: str = typer.Option(..., "--evidence"),
    required_action: str = typer.Option(..., "--required-action"),
    do_not: str = typer.Option(..., "--do-not"),
    acceptance_delta: str = typer.Option("none", "--acceptance-delta"),
    effective_boundary: str = typer.Option("after-current-atomic-task", "--effective-boundary"),
    requires_spec_revision: bool = typer.Option(False, "--requires-spec-revision"),
    root: Path | None = typer.Option(None, "--root"),
) -> None:
    instruction = Instruction(
        instruction_id=f"ins_{uuid.uuid4().hex}",
        sender_authority=sender,
        action=action,
        project_id=project_id,
        spec_id=spec_id,
        task_id=task_id,
        observed=observed,
        evidence=evidence,
        required_action=required_action,
        do_not=do_not,
        acceptance_delta=acceptance_delta,
        effective_boundary=effective_boundary,
        requires_spec_revision=requires_spec_revision,
    )
    workspace = _workspace(root)
    path = record_instruction(workspace, instruction)
    from .orchestrator import wake_owner

    wake_owner(workspace, project_id)
    _emit({"instruction_id": instruction.instruction_id, "path": path})


@decision_app.command("record")
@_guard
def decision_record(
    title: str = typer.Option(..., "--title"),
    decision: str = typer.Option(..., "--decision"),
    rationale: str = typer.Option(..., "--rationale"),
    actor: str = typer.Option(..., "--actor"),
    project_id: str | None = typer.Option(None, "--project", "--project-id"),
    root: Path | None = typer.Option(None, "--root"),
) -> None:
    path = record_decision(
        _workspace(root), title, decision, rationale, actor=actor, project_id=project_id
    )
    _emit({"path": path})


@action_app.command("request")
@_guard
def action_request(
    project_id: str = typer.Option(..., "--project", "--project-id"),
    requester: str = typer.Option(..., "--requester"),
    domain: str = typer.Option(..., "--domain"),
    summary: str = typer.Option(..., "--summary"),
    effect: str = typer.Option(..., "--effect"),
    reversible: bool = typer.Option(False, "--reversible/--irreversible"),
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    action = create_action_request(
        _workspace(root), project_id, requester, domain, summary, effect, reversible
    )
    _emit(action, json_output=json_output)


def _decide(action_id: str, approver: str, decision: str, note: str, root: Path | None) -> None:
    status_value, path = decide_action(_workspace(root), action_id, approver, decision, note)
    _emit({"action_id": action_id, "status": status_value, "path": path})


@action_app.command("approve")
@_guard
def action_approve(
    action_id: str = typer.Argument(...),
    approver: str = typer.Option(..., "--approver"),
    note: str = typer.Option("", "--note"),
    root: Path | None = typer.Option(None, "--root"),
) -> None:
    _decide(action_id, approver, "approve", note, root)


@action_app.command("reject")
@_guard
def action_reject(
    action_id: str = typer.Argument(...),
    approver: str = typer.Option(..., "--approver"),
    note: str = typer.Option("", "--note"),
    root: Path | None = typer.Option(None, "--root"),
) -> None:
    _decide(action_id, approver, "reject", note, root)


@runtime_app.command("rebuild")
@_guard
def runtime_rebuild(
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _emit(rebuild_runtime(_workspace(root)), json_output=json_output)


@runtime_app.command("snapshot")
@_guard
def runtime_snapshot(
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _emit(_workspace(root).runtime.snapshot(), json_output=json_output)


@app.command("start")
@_guard
def start(
    root: Path | None = typer.Option(None, "--root"),
    attach: bool = typer.Option(True, "--attach/--no-attach", hidden=True),
    permission: str = typer.Option("default", "--permission"),
    no_executives: bool = typer.Option(False, "--no-executives"),
) -> None:
    workspace = _workspace(root)
    _ = attach, permission
    from .bridge import run_bridge

    run_bridge(workspace, launch_executives=not no_executives)


@app.command("cockpit")
@_guard
def cockpit(
    root: Path | None = typer.Option(None, "--root"),
    no_executives: bool = typer.Option(False, "--no-executives"),
) -> None:
    """Open or focus the native OneCo Bridge."""
    from .bridge import run_bridge

    run_bridge(_workspace(root), launch_executives=not no_executives)


@app.command("launch")
@_guard
def launch(
    identity: str = typer.Argument(..., help="CEO, CTO, or an Owner such as PROJ-001:owner."),
    root: Path | None = typer.Option(None, "--root"),
    permission: str = typer.Option("default", "--permission"),
) -> None:
    workspace = _workspace(root)
    identity = identity.upper() if identity.upper() in {"CEO", "CTO"} else identity
    if identity in {"CEO", "CTO"}:
        from .orchestrator import focus_executive

        focus_executive(workspace, identity)
        return
    project_id = _project_id_from_owner(identity)
    if not project_id:
        raise WorkspaceError("launch identity must be CEO, CTO, or PROJ-NNN:owner")
    project = workspace.find_project(project_id)
    ready, reasons = workspace.readiness(project_id)
    if project.state != ProjectState.ACTIVE:
        raise ProjectError(f"{project_id} is {project.state}; Owner launch requires active")
    if not ready:
        raise ProjectError("Owner launch requires readiness: " + "; ".join(reasons))
    from .orchestrator import wake_owner

    result = wake_owner(workspace, project_id, permission=permission)
    console.print(f"Owner ready: {result['tmux_target']}")


@app.command("talk")
@_guard
def talk(
    role: str = typer.Argument(..., help="CEO or CTO."),
    root: Path | None = typer.Option(None, "--root"),
) -> None:
    role = role.upper()
    if role not in {"CEO", "CTO"}:
        raise WorkspaceError("talk role must be CEO or CTO")
    from .orchestrator import focus_executive

    focus_executive(_workspace(root), role)


@app.command("branch")
@_guard
def branch(
    role: str = typer.Argument(..., help="CEO or CTO."),
    purpose: str = typer.Option(..., "--purpose", help="Short title for the parallel discussion."),
    recovery_candidate: bool = typer.Option(
        False, "--recovery-candidate", help="Create a fenced succession candidate."
    ),
    root: Path | None = typer.Option(None, "--root"),
) -> None:
    """Fork an executive's exact Trae context into a titled advisory window."""
    role = role.upper()
    if role not in {"CEO", "CTO"}:
        raise WorkspaceError("branch role must be CEO or CTO")
    from .orchestrator import branch_executive

    _emit(
        branch_executive(
            _workspace(root), role, purpose, candidate=recovery_candidate
        )
    )


@app.command("create-launcher")
@_guard
def create_launcher_command(
    root: Path | None = typer.Option(None, "--root"),
    destination: Path | None = typer.Option(None, "--destination"),
) -> None:
    """Create or refresh this company's Finder launcher."""
    path = create_finder_launcher(
        _workspace(root), destination.expanduser() if destination else None
    )
    console.print(path)


@app.command("open")
@_guard
def open_project(
    project_id: str = typer.Argument(...),
    root: Path | None = typer.Option(None, "--root"),
) -> None:
    workspace = _workspace(root)
    project = workspace.find_project(project_id)
    ready, reasons = workspace.readiness(project_id)
    if project.state != ProjectState.ACTIVE or not ready:
        detail = f"state={project.state}"
        if reasons:
            detail += "; " + "; ".join(reasons)
        raise ProjectError(f"Owner is not launchable: {detail}")
    from .orchestrator import wake_owner, watch_owner

    wake_owner(workspace, project_id)
    watch_owner(workspace, project_id)


@app.command("board")
@_guard
def board(
    root: Path | None = typer.Option(None, "--root"),
    once: bool = typer.Option(False, "--once", help="Print once without the interactive prompt."),
) -> None:
    workspace = _workspace(root)
    while True:
        console.clear() if sys.stdin.isatty() and not once else None
        _print_status(_status_payload(workspace))
        if once or not sys.stdin.isatty():
            return
        command = console.input("[bold]board>[/bold] ").strip()
        if command in {"q", "quit", "exit"}:
            return
        if command in {"", "r", "refresh"}:
            continue
        words = command.split()
        if len(words) == 2 and words[0] == "talk":
            talk(role=words[1], root=workspace.root)
        elif len(words) == 2 and words[0] == "open":
            open_project(project_id=words[1], root=workspace.root)
        else:
            console.print("Commands: refresh, talk CEO|CTO, open PROJ-NNN, quit")


@app.command("role-run", hidden=True)
@_guard
def role_run(
    identity: str = typer.Argument(...),
    root: Path = typer.Option(..., "--root"),
    model: str = typer.Option(trae.DEFAULT_MODEL, "--model"),
    permission: str = typer.Option("default", "--permission"),
    tmux_target: str | None = typer.Option(None, "--tmux-target"),
) -> None:
    workspace = _workspace(root)
    project_id = _project_id_from_owner(identity)
    if project_id:
        project = workspace.find_project(project_id)
        ready, reasons = workspace.readiness(project_id)
        if project.state != ProjectState.ACTIVE or not ready:
            raise ProjectError(
                f"Owner launch requires ready+active: state={project.state}; {'; '.join(reasons)}"
            )
    cwd = _role_cwd(workspace, identity)
    session = workspace.runtime.register_session(
        identity, cwd, provider="trae", tmux_target=tmux_target, pid=os.getpid(), replace=True
    )
    if project_id:
        workspace.runtime.acquire_writer(project_id, session["session_id"], int(session["epoch"]))
    binary = trae.require_model(model)
    prompt = trae.role_prompt(identity, workspace.root, cwd if project_id else None)
    environment = os.environ.copy()
    environment.update(
        {
            "ONECO_ROOT": str(workspace.root),
            "ONECO_IDENTITY": identity,
            "ONECO_SESSION_ID": session["session_id"],
            "ONECO_EPOCH": str(session["epoch"]),
        }
    )
    argv = [
        str(binary),
        "--cd",
        str(cwd),
        "--model",
        model,
        "--permission-mode",
        permission,
        prompt,
    ]
    os.execvpe(str(binary), argv, environment)


@internal_app.command("executive-run", hidden=True)
@_guard
def executive_run(
    identity: str = typer.Argument(...),
    root: Path = typer.Option(..., "--root"),
    session_id: str = typer.Option(..., "--session-id"),
    epoch: int = typer.Option(..., "--epoch"),
    model: str = typer.Option(trae.DEFAULT_MODEL, "--model"),
    permission: str = typer.Option("default", "--permission"),
    resume: str | None = typer.Option(None, "--resume"),
    fork_from: str | None = typer.Option(None, "--fork-from"),
    advisory_prompt: str | None = typer.Option(None, "--advisory-prompt"),
) -> None:
    from .orchestrator import run_executive

    run_executive(
        _workspace(root),
        identity,
        session_id,
        epoch,
        model=model,
        permission=permission,
        resume=resume,
        fork_from=fork_from,
        advisory_prompt=advisory_prompt,
    )


@internal_app.command("owner-turn", hidden=True)
@_guard
def owner_turn(
    project_id: str = typer.Argument(...),
    root: Path = typer.Option(..., "--root"),
    session_id: str = typer.Option(..., "--session-id"),
    epoch: int = typer.Option(..., "--epoch"),
    permission: str = typer.Option("default", "--permission"),
) -> None:
    from .orchestrator import run_owner_turn

    raise typer.Exit(
        run_owner_turn(
            _workspace(root), project_id, session_id, epoch, permission=permission
        )
    )


@internal_app.command("owner-wake", hidden=True)
@_guard
def owner_wake(
    project_id: str = typer.Argument(...),
    root: Path = typer.Option(..., "--root"),
    permission: str = typer.Option("default", "--permission"),
) -> None:
    from .orchestrator import wake_owner

    _emit(wake_owner(_workspace(root), project_id, permission=permission), json_output=True)


@mcp_app.command("serve")
def mcp_serve() -> None:
    """Run the OneCo MCP server on stdio."""
    from .mcp_server import run

    run()


def _repository_root() -> Path:
    source_checkout = Path(__file__).resolve().parents[2]
    installed_resources = Path(__file__).resolve().parent / "resources"
    for candidate in (source_checkout, installed_resources):
        if (candidate / "adapters" / "trae-plugin" / "oneco").is_dir() and (
            candidate / "integrations" / "speckit"
        ).is_dir():
            return candidate
    raise WorkspaceError("OneCo integration assets are unavailable in this installation")


@host_app.command("install-trae")
@_guard
def host_install_trae(
    preview: bool = typer.Option(False, "--preview"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    if preview and yes:
        raise WorkspaceError("choose either --preview or --yes")
    command = _trae_plugin_command()
    if preview or not yes:
        console.print("Preview: " + subprocess.list2cmdline(command))
        return
    console.print(_install_trae_plugin())


def _spec_paths(workspace: Workspace, project_id: str) -> tuple[Path, dict[str, Any], Path]:
    project = workspace.project_path(project_id)
    state_path = project / ".specify" / "feature.json"
    state = load_json(state_path)
    if not isinstance(state, dict) or not state.get("feature_directory"):
        raise WorkspaceError(f"invalid feature state: {state_path}")
    feature = project / str(state["feature_directory"])
    try:
        feature.resolve().relative_to(project.resolve())
    except ValueError as exc:
        raise WorkspaceError("feature directory escapes the project") from exc
    return project, state, feature


@spec_app.command("init")
@_guard
def spec_init(
    project_id: str = typer.Argument(...),
    feature_number: str = typer.Option("001", "--feature-number"),
    feature_slug: str = typer.Option("first-vertical", "--feature-slug"),
    spec_id: str | None = typer.Option(None, "--spec-id"),
    brief: Path | None = typer.Option(None, "--brief"),
    root: Path | None = typer.Option(None, "--root"),
) -> None:
    workspace = _workspace(root)
    project = workspace.project_path(project_id)
    repository = _repository_root()
    specify = shutil.which("specify")
    if not specify:
        raise WorkspaceError("Spec Kit CLI `specify` was not found")
    preset = repository / "integrations" / "speckit" / "preset"
    result = subprocess.run(
        [specify, "preset", "add", "--dev", str(preset)],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise WorkspaceError(
            result.stderr.strip()
            or result.stdout.strip()
            or "failed to install OneCo Spec Kit preset"
        )
    stage = repository / "integrations" / "speckit" / "bin" / "stage-project"
    command = [
        str(stage),
        "--project-dir",
        str(project),
        "--feature-number",
        feature_number,
        "--feature-slug",
        feature_slug,
        "--brief",
        str((brief or project / "BRIEF.md").resolve()),
    ]
    if spec_id:
        command.extend(["--spec-id", spec_id])
    staged = subprocess.run(command, cwd=project, capture_output=True, text=True, check=False)
    if staged.returncode:
        raise WorkspaceError(
            staged.stderr.strip() or staged.stdout.strip() or "failed to stage Spec Kit project"
        )
    console.print(staged.stdout.strip())


@spec_app.command("status")
@_guard
def spec_status(
    project_id: str = typer.Argument(...),
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    workspace = _workspace(root)
    project, state, feature = _spec_paths(workspace, project_id)
    files = {name: (feature / name).is_file() for name in ("spec.md", "plan.md", "tasks.md")}
    _emit(
        {"project_id": project_id, **state, "project_dir": str(project), "artifacts": files},
        json_output=json_output,
    )


@spec_app.command("validate")
@_guard
def spec_validate(
    project_id: str = typer.Argument(...),
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    workspace = _workspace(root)
    project, state, feature = _spec_paths(workspace, project_id)
    errors: list[str] = []
    if not re.fullmatch(
        r"specs/[0-9]{3}-[a-z0-9]+(?:-[a-z0-9]+)*", str(state["feature_directory"])
    ):
        errors.append("feature_directory must match specs/NNN-kebab-slug")
    if not re.fullmatch(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*", str(state.get("spec_id", ""))):
        errors.append("spec_id is invalid")
    for relative in ("BRIEF.md", ".specify/memory/constitution.md"):
        if not (project / relative).is_file():
            errors.append(f"missing {relative}")
    for filename in ("spec.md", "plan.md", "tasks.md"):
        path = feature / filename
        if not path.is_file():
            errors.append(f"missing {state['feature_directory']}/{filename}")
        elif not path.read_text(encoding="utf-8").strip():
            errors.append(f"empty {state['feature_directory']}/{filename}")
    tasks = feature / "tasks.md"
    if tasks.is_file():
        identifiers = re.findall(
            r"^- \[ [ xX]\] (T[0-9]{3})\b", tasks.read_text(encoding="utf-8"), re.MULTILINE
        )
        if not identifiers:
            errors.append("tasks.md has no TNNN tasks")
        elif len(identifiers) != len(set(identifiers)):
            errors.append("tasks.md contains duplicate task IDs")
    payload = {"project_id": project_id, "valid": not errors, "errors": errors}
    _emit(payload, json_output=json_output)
    if errors:
        raise typer.Exit(1)


@app.command("doctor")
@_guard
def doctor(
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, required: bool = True) -> None:
        checks.append({"name": name, "ok": ok, "required": required, "detail": detail})

    add("python", sys.version_info >= (3, 11), sys.version.split()[0])
    for executable in ("git", "tmux"):
        found = shutil.which(executable)
        add(executable, bool(found), found or "not found")
    specify = shutil.which("specify")
    add("specify", bool(specify), specify or "not found (needed only for spec commands)", False)
    workspace: Workspace | None = None
    try:
        workspace = _workspace(root)
        add("workspace", True, str(workspace.root), False)
    except OneCoError as exc:
        add("workspace", False, str(exc), False)
    model = workspace.manifest().model if workspace else trae.DEFAULT_MODEL
    try:
        binary = trae.require_model(model)
        add("trae", True, f"{binary}; model {model}")
    except OneCoError as exc:
        add("trae", False, str(exc))
    if sys.platform == "darwin":
        from .adapters import ghostty

        ghostty_check = ghostty.health()
        add(
            "ghostty",
            ghostty_check["state"] == "ok",
            str(ghostty_check["diagnostic"]),
        )
    if workspace is not None:
        from .health import collect as collect_health

        for item in collect_health(workspace):
            if item["name"] in {
                "oneco-plugin",
                "oneco-mcp-session-env",
                "user-mcp-profile",
            }:
                add(
                    str(item["name"]),
                    item["state"] == "ok",
                    str(item["diagnostic"]),
                    item["name"] in {"oneco-plugin", "oneco-mcp-session-env"},
                )
    ok = all(item["ok"] for item in checks if item["required"])
    if json_output:
        _emit({"ok": ok, "checks": checks}, json_output=True)
    else:
        table = Table("Check", "Result", "Detail")
        for check in checks:
            table.add_row(
                check["name"],
                "OK" if check["ok"] else "WARN" if not check["required"] else "FAIL",
                check["detail"],
            )
        console.print(table)
    if not ok:
        raise typer.Exit(1)


def _payload_value(payload: Any, *keys: str) -> str | None:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        for value in payload.values():
            found = _payload_value(value, *keys)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _payload_value(value, *keys)
            if found:
                return found
    return None


def _handle_hook(event_name: str, payload: dict[str, Any] | None = None) -> None:
    root = os.environ.get("ONECO_ROOT")
    identity = os.environ.get("ONECO_IDENTITY")
    session_id = os.environ.get("ONECO_SESSION_ID")
    epoch_text = os.environ.get("ONECO_EPOCH")
    if not all((root, identity, session_id, epoch_text)):
        return
    workspace = Workspace.open(Path(root))
    active = workspace.runtime.assert_active_session(session_id)
    epoch = int(active["epoch"])
    identity = str(active["logical_id"])
    normalized = event_name.lower()
    payload = payload or {}
    provider_session_id = _payload_value(payload, "session_id", "sessionId", "thread_id", "threadId")
    provider_turn_id = _payload_value(payload, "turn_id", "turnId")
    cwd = _payload_value(payload, "cwd")
    prompt = _payload_value(payload, "prompt", "user_prompt", "userPrompt", "input")
    if provider_session_id:
        workspace.runtime.bind_provider_session(
            session_id, epoch, provider_session_id, cwd=cwd
        )
    if normalized == "userpromptsubmit":
        workspace.runtime.touch(session_id, epoch, WorkState.WORKING)
        if provider_session_id and provider_turn_id and prompt:
            workspace.runtime.record_human_turn(provider_session_id, provider_turn_id, prompt)
            workspace.runtime.confirm_succession(session_id, provider_turn_id, prompt)
    elif normalized == "stop":
        workspace.runtime.touch(session_id, epoch, WorkState.IDLE)
    elif normalized == "sessionend":
        if str(active["authority_kind"]) == "owner":
            # The persistent Owner wrapper, not one finite Trae exec call, owns lifecycle.
            workspace.runtime.touch(session_id, epoch, WorkState.IDLE)
        else:
            workspace.runtime.mark_lost(session_id, epoch)
    elif normalized == "sessionstart":
        workspace.runtime.touch(session_id, epoch, WorkState.IDLE)
    workspace.runtime.event(
        f"hook.{event_name}",
        identity,
        {
            "session_id": session_id,
            "epoch": epoch,
            "provider_session_id": provider_session_id,
            "provider_turn_id": provider_turn_id,
        },
    )


class HookGroup(typer.core.TyperGroup):
    """Route the public ``hook EVENT`` shorthand to its hidden event command."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        if args and args[0] not in {"event", "--help", "-h"} and not args[0].startswith("-"):
            args.insert(0, "event")
        return super().parse_args(ctx, args)


hook_app = typer.Typer(
    help="Receive best-effort host lifecycle observations.",
    no_args_is_help=True,
    cls=HookGroup,
)
app.add_typer(hook_app, name="hook")


@hook_app.command("event", hidden=True)
@_guard
def hook_event(event_name: str = typer.Argument(...)) -> None:
    payload: dict[str, Any] = {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
        if raw.strip():
            value = json.loads(raw)
            if isinstance(value, dict):
                payload = value
    _handle_hook(event_name, payload)


@internal_hook_app.command("event", hidden=True)
@_guard
def internal_hook_event(event_name: str = typer.Argument(...)) -> None:
    hook_event(event_name)


if __name__ == "__main__":
    app()
