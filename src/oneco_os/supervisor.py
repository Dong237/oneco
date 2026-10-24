"""Company supervision daemon independent from the Cockpit window."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .adapters.trae import DEFAULT_PERMISSION_MODE
from .errors import OneCoError, WorkspaceError
from .models import ProjectState
from .orchestrator import wake_owner
from .services import reconcile_owner_work, terminal_release_result
from .util import local_socket_path
from .workspace import Workspace


def socket_path(workspace: Workspace) -> Path:
    return local_socket_path(workspace.root, "supervisor")


def _lock_path(workspace: Workspace, kind: str) -> Path:
    path = socket_path(workspace).with_suffix(f".{kind}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _lock_is_held(path: Path) -> bool:
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


def _send(workspace: Workspace, command: str) -> str | None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            client.connect(str(socket_path(workspace)))
            client.sendall(command.encode("utf-8"))
            return client.recv(64).decode("utf-8")
    except OSError:
        return None


def ensure_supervisor(
    workspace: Workspace, *, permission: str = DEFAULT_PERMISSION_MODE
) -> dict[str, Any]:
    startup_path = _lock_path(workspace, "startup")
    with startup_path.open("a+") as startup_lock:
        fcntl.flock(startup_lock.fileno(), fcntl.LOCK_EX)
        try:
            if _send(workspace, "ping") == "pong":
                return {"status": "running"}
            if _lock_is_held(_lock_path(workspace, "daemon")):
                return {"status": "unresponsive"}
            path = socket_path(workspace)
            path.unlink(missing_ok=True)
            log_path = workspace.root / ".oneco" / "logs" / "supervisor.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "oneco_os.cli",
                        "internal",
                        "supervisor-run",
                        "--root",
                        str(workspace.root),
                        "--permission",
                        permission,
                    ],
                    cwd=workspace.root,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            (workspace.root / ".oneco" / "supervisor.pid").write_text(
                json.dumps({"pid": process.pid, "started_at": datetime.now(UTC).isoformat()})
                + "\n",
                encoding="utf-8",
            )
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if _send(workspace, "ping") == "pong":
                    return {"status": "started", "pid": process.pid, "log": str(log_path)}
                if process.poll() is not None:
                    raise WorkspaceError(
                        f"supervisor exited during startup; inspect {log_path}"
                    )
                time.sleep(0.05)
            raise WorkspaceError(f"supervisor did not become ready; inspect {log_path}")
        finally:
            fcntl.flock(startup_lock.fileno(), fcntl.LOCK_UN)


def stop_supervisor(workspace: Workspace) -> dict[str, str]:
    response = _send(workspace, "stop")
    if response != "stopping":
        return {"status": "not_running"}
    return {"status": "stopping"}


def supervision_tick(workspace: Workspace, *, permission: str = DEFAULT_PERMISSION_MODE) -> None:
    active = {
        project.id: project
        for project in workspace.portfolio().projects
        if project.state == ProjectState.ACTIVE
    }
    for project_id in active:
        reconcile_owner_work(workspace, project_id)
        owner = f"{project_id}:owner"
        queue = workspace.runtime.queued_work(owner)
        has_pending_work = any(item["status"] != "done" for item in queue)
        if terminal_release_result(workspace, project_id) is not None and not has_pending_work:
            continue
        if any(item["status"] in {"queued", "claimed", "running"} for item in queue):
            try:
                wake_owner(workspace, project_id, permission=permission)
            except (OSError, OneCoError):
                pass
    policy_path = workspace.root / "operations" / "supervision-policy.json"
    if not policy_path.is_file():
        return
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    interval = int(policy.get("interval_minutes", 240))
    pulse_target = int(policy.get("progress_pulse_target_minutes", 15))
    now = datetime.now(UTC)
    indexed = {row["project_id"]: row for row in workspace.runtime.due_supervision()}
    with workspace.runtime.connect() as connection:
        all_supervision = {
            row["project_id"]: dict(row)
            for row in connection.execute("SELECT * FROM supervision")
        }
    for project_id in active:
        owner_queue = workspace.runtime.queued_work(f"{project_id}:owner")
        if terminal_release_result(workspace, project_id) is not None and not any(
            item["status"] != "done" for item in owner_queue
        ):
            continue
        if project_id not in all_supervision:
            workspace.runtime.set_supervision_due(
                project_id, next_review_at=(now + timedelta(minutes=interval)).isoformat()
            )
            continue
        supervision = all_supervision[project_id]
        pulse_reference = supervision.get("progress_requested_at") or supervision.get(
            "updated_at"
        )
        if pulse_reference and datetime.fromisoformat(str(pulse_reference)) <= now - timedelta(
            minutes=pulse_target
        ):
            workspace.runtime.send_message(
                "SYSTEM",
                f"{project_id}:owner",
                "progress_pulse",
                (
                    f"Progress pulse for {project_id}: record a durable checkpoint or blocker "
                    "at the next safe atomic boundary."
                ),
                project_id=project_id,
                requires_ack=False,
                idempotency_key=f"progress:{project_id}:{pulse_reference}",
                subject=f"Progress pulse: {project_id}",
                thread_key=project_id,
                object_id=project_id,
            )
            workspace.runtime.request_progress(project_id)
        due = indexed.get(project_id)
        if not due:
            continue
        due_at = str(due["next_review_at"])
        message_id = workspace.runtime.send_message(
            "SYSTEM",
            "CTO",
            "supervision_due",
            f"Scheduled review is due for {project_id}. Inspect durable checkpoints and only stop affected work at a real gate.",
            project_id=project_id,
            requires_ack=True,
            idempotency_key=f"supervision:{project_id}:{due_at}",
            subject=f"CTO supervision due: {project_id}",
            thread_key=project_id,
            object_id=project_id,
        )
        workspace.runtime.enqueue_role_work(
            "CTO",
            "scheduled_review",
            {"project_id": project_id, "message_id": message_id},
            object_id=project_id,
            priority=30,
            idempotency_key=f"supervision:{project_id}:{due_at}:work",
        )
        workspace.runtime.request_progress(project_id)
        workspace.runtime.set_supervision_due(
            project_id, next_review_at=(now + timedelta(minutes=interval)).isoformat()
        )


def run_supervisor(workspace: Workspace, *, permission: str = DEFAULT_PERMISSION_MODE) -> None:
    daemon_path = _lock_path(workspace, "daemon")
    with daemon_path.open("a+") as daemon_lock:
        try:
            fcntl.flock(daemon_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        path = socket_path(workspace)
        if _send(workspace, "ping") == "pong":
            return
        path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(2)
        server.settimeout(1.5)
        running = True
        try:
            while running:
                try:
                    client, _ = server.accept()
                except TimeoutError:
                    supervision_tick(workspace, permission=permission)
                    continue
                with client:
                    command = client.recv(32).decode("utf-8")
                    if command == "ping":
                        client.sendall(b"pong")
                    elif command == "stop":
                        client.sendall(b"stopping")
                        running = False
                if running:
                    supervision_tick(workspace, permission=permission)
        finally:
            server.close()
            path.unlink(missing_ok=True)
            (workspace.root / ".oneco" / "supervisor.pid").unlink(missing_ok=True)
            fcntl.flock(daemon_lock.fileno(), fcntl.LOCK_UN)
