"""OneCo Bridge: a small native WKWebView cockpit and local supervisor."""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path
from typing import Any

from .errors import WorkspaceError
from .health import collect as collect_health
from .models import ProjectState
from .orchestrator import focus_executive, wake_owner, watch_owner
from .util import load_json
from .workspace import Workspace


class BridgeAPI:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def snapshot(self) -> dict[str, Any]:
        manifest = self.workspace.manifest()
        runtime = self.workspace.runtime.snapshot(extended=True)
        identities = {item["logical_id"]: item for item in runtime["identities"]}
        work_by_owner: dict[str, list[dict[str, Any]]] = {}
        for item in runtime["work_queue"]:
            work_by_owner.setdefault(str(item["recipient"]), []).append(item)
        projects = []
        for project in self.workspace.portfolio().projects:
            ready, reasons = self.workspace.readiness(project.id)
            project_root = self.workspace.project_path(project.id)
            data_path = project_root / "project.json"
            data = load_json(data_path) if data_path.is_file() else {}
            checkpoints = sorted((project_root / "checkpoints").glob("*.json"), reverse=True)
            latest = load_json(checkpoints[0]) if checkpoints else None
            owner_id = f"{project.id}:owner"
            projects.append(
                {
                    **project.model_dump(mode="json"),
                    "ready": ready,
                    "readiness_reasons": reasons,
                    "current_spec": data.get("current_spec"),
                    "current_task": data.get("current_task"),
                    "latest_checkpoint": latest,
                    "owner": identities.get(owner_id),
                    "queue": work_by_owner.get(owner_id, []),
                }
            )
        attention = []
        for project in projects:
            if project["readiness_reasons"] and project["state"] in {"shaping", "ready"}:
                attention.append(
                    {
                        "kind": "readiness",
                        "project_id": project["id"],
                        "summary": "; ".join(project["readiness_reasons"][:2]),
                    }
                )
            if project["owner"] and project["owner"]["work_state"] == "blocked":
                attention.append(
                    {"kind": "blocker", "project_id": project["id"], "summary": "Owner blocked"}
                )
        for action in runtime["pending_actions"]:
            attention.append(
                {"kind": "action", "project_id": action["project_id"], "summary": action["summary"]}
            )
        return {
            "company": manifest.company_name,
            "root": str(self.workspace.root),
            "executives": {role: identities.get(role) for role in ("CEO", "CTO")},
            "projects": projects,
            "attention": attention,
            "capabilities": runtime["capabilities"],
            "unread": runtime["unread"],
        }

    def focus_executive(self, role: str) -> dict[str, Any]:
        return focus_executive(self.workspace, role)

    def watch_owner(self, project_id: str) -> dict[str, str]:
        project = self.workspace.find_project(project_id)
        if project.state == ProjectState.ACTIVE:
            wake_owner(self.workspace, project_id)
        return watch_owner(self.workspace, project_id)

    def open_folder(self, project_id: str) -> dict[str, str]:
        path = self.workspace.project_path(project_id)
        os.spawnlp(os.P_NOWAIT, "open", "open", str(path))
        return {"path": str(path)}

    def refresh_health(self) -> list[dict[str, Any]]:
        return collect_health(self.workspace)


class OwnerSupervisor:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.wait(1.5):
            for project in self.workspace.portfolio().projects:
                if project.state != ProjectState.ACTIVE:
                    continue
                owner = f"{project.id}:owner"
                queue = self.workspace.runtime.queued_work(owner)
                if not any(item["status"] == "queued" for item in queue):
                    continue
                if any(item["status"] in {"claimed", "running"} for item in queue):
                    continue
                try:
                    wake_owner(self.workspace, project.id)
                except (OSError, WorkspaceError):
                    continue

    def stop(self) -> None:
        self.stop_event.set()


def _resources() -> Path:
    return Path(__file__).resolve().parent / "resources" / "bridge"


def _send_existing(socket_path: Path, command: str = "focus") -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.4)
            client.connect(str(socket_path))
            client.sendall(command.encode())
        return True
    except OSError:
        return False


def run_bridge(workspace: Workspace, *, launch_executives: bool = True) -> None:
    if os.uname().sysname != "Darwin":
        raise WorkspaceError("OneCo Bridge v0.2 requires macOS")
    socket_path = workspace.root / ".oneco" / "bridge.sock"
    if _send_existing(socket_path):
        return
    try:
        socket_path.unlink(missing_ok=True)
        import webview
    except ImportError as exc:
        raise WorkspaceError("pywebview is not installed; reinstall OneCo on macOS") from exc

    collect_health(workspace)
    if launch_executives:
        for role in ("CEO", "CTO"):
            try:
                focus_executive(workspace, role)
            except WorkspaceError:
                pass

    api = BridgeAPI(workspace)
    index = (_resources() / "index.html").resolve()
    window = webview.create_window(
        f"OneCo Bridge — {workspace.manifest().company_name}",
        index.as_uri(),
        js_api=api,
        width=1320,
        height=860,
        min_size=(980, 640),
        background_color="#171713",
    )
    supervisor = OwnerSupervisor(workspace)
    threading.Thread(target=supervisor.run, name="oneco-owner-supervisor", daemon=True).start()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    server.listen(2)

    def listen() -> None:
        while not supervisor.stop_event.is_set():
            try:
                client, _ = server.accept()
                command = client.recv(32).decode()
                client.close()
                if command == "focus":
                    window.show()
                    window.restore()
                elif command in {"CEO", "CTO"}:
                    api.focus_executive(command)
            except OSError:
                break

    threading.Thread(target=listen, name="oneco-bridge-socket", daemon=True).start()

    cleanup_lock = threading.Lock()
    cleaned_up = False

    def closed() -> None:
        nonlocal cleaned_up
        with cleanup_lock:
            if cleaned_up:
                return
            cleaned_up = True
            supervisor.stop()
            server.close()
            socket_path.unlink(missing_ok=True)

    window.events.closed += closed
    try:
        webview.start(gui="cocoa", debug=False, private_mode=False)
    finally:
        closed()
