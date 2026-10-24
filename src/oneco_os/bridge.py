"""OneCo Bridge: a small native WKWebView cockpit and local supervisor."""

from __future__ import annotations

import os
import socket
import threading
import uuid
from pathlib import Path
from typing import Any

from .adapters import ghostty
from .adapters.trae import DEFAULT_PERMISSION_MODE
from .company_services import (
    board_decide_bet,
    board_decide_release,
    list_bets,
    list_signals,
    record_signal,
    validate_release_candidate,
)
from .errors import ConflictError, WorkspaceError
from .health import collect as collect_health
from .models import ProjectState, ReleaseRecord, ReleaseState
from .orchestrator import executive_session_alive, focus_executive, wake_owner, watch_owner
from .records import (
    committed_file_matches_head,
    latest_committed_launch_record,
    load_committed_json,
)
from .util import load_json, local_socket_path
from .workspace import Workspace


def _active_display_count() -> int:
    """Return the number of displays on which native windows can actually render."""

    try:
        from Quartz import CGGetActiveDisplayList
    except ImportError as exc:
        raise WorkspaceError(
            "macOS display APIs are unavailable; reinstall OneCo's macOS dependencies"
        ) from exc
    error, _displays, count = CGGetActiveDisplayList(16, None, None)
    if error:
        raise WorkspaceError(f"cannot query active macOS displays (CoreGraphics error {error})")
    return int(count)


def _require_active_display() -> None:
    if _active_display_count() < 1:
        raise WorkspaceError(
            "No active macOS display is available. Wake or unlock this Mac, then run "
            "`oneco start` again; OneCo did not create Cockpit or executive windows."
        )


class BridgeAPI:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def snapshot(self) -> dict[str, Any]:
        company_manifest = self.workspace.manifest()
        runtime = self.workspace.runtime.snapshot(extended=True)
        identities: dict[str, dict[str, Any]] = {}
        for raw_identity in runtime["identities"]:
            identity = dict(raw_identity)
            session = self.workspace.runtime.current_session(str(identity["logical_id"]))
            if (
                identity.get("lifecycle") == "live"
                and identity.get("terminal_transport") == "ghostty"
                and identity.get("terminal_handle")
                and ghostty.available()
                and session is not None
                and not executive_session_alive(session)
            ):
                # This is a read-model correction only. ``oneco session reconcile``
                # persists the lost lifecycle while retaining exact-thread identity.
                identity["runtime_lifecycle"] = identity["lifecycle"]
                identity["runtime_work_state"] = identity["work_state"]
                identity["lifecycle"] = "lost"
                identity["work_state"] = "idle"
            identities[str(identity["logical_id"])] = identity
        work_by_owner: dict[str, list[dict[str, Any]]] = {}
        for item in runtime["work_queue"]:
            work_by_owner.setdefault(str(item["recipient"]), []).append(item)

        releases: list[dict[str, Any]] = []
        releases_by_project: dict[str, list[dict[str, Any]]] = {}
        for runtime_release in runtime["releases"]:
            release_id = str(runtime_release["release_id"])
            project_id = str(runtime_release["project_id"])
            release_root = self.workspace.root / "operations" / project_id / "releases" / release_id
            candidate_path = release_root / "candidate.json"
            candidate = (
                load_json(candidate_path)
                if candidate_path.is_file()
                and committed_file_matches_head(self.workspace.root, candidate_path)
                else {}
            )
            endorsements: dict[str, dict[str, Any] | None] = {}
            for role in ("CEO", "CTO"):
                path = release_root / f"{role.lower()}-endorsement.json"
                endorsements[role] = (
                    load_json(path)
                    if path.is_file() and committed_file_matches_head(self.workspace.root, path)
                    else None
                )
            decision_path = release_root / "board-decision.json"
            decision = (
                load_json(decision_path)
                if decision_path.is_file()
                and committed_file_matches_head(self.workspace.root, decision_path)
                else None
            )
            result_path = release_root / "result.json"
            result = (
                load_json(result_path)
                if result_path.is_file()
                and committed_file_matches_head(self.workspace.root, result_path)
                else None
            )
            state = ReleaseState.CANDIDATE
            current_candidate = False
            if candidate:
                try:
                    validated = ReleaseRecord.model_validate(candidate)
                    validate_release_candidate(self.workspace, validated)
                    current_candidate = True
                except (ConflictError, OSError, ValueError):
                    state = ReleaseState.STALE
            if current_candidate and all(
                endorsements[role]
                and endorsements[role].get("release_digest") == runtime_release["release_digest"]
                for role in ("CEO", "CTO")
            ):
                state = ReleaseState.READY
            if decision and decision.get("release_digest") == runtime_release["release_digest"]:
                state = (
                    ReleaseState.QUEUED
                    if decision.get("kind") == "approve_release"
                    else ReleaseState.REJECTED
                )
            if result and result.get("release_digest") == runtime_release["release_digest"]:
                state = ReleaseState.RELEASED if result.get("succeeded") else ReleaseState.FAILED
            view = {
                **candidate,
                **runtime_release,
                "runtime_state": runtime_release["state"],
                "state": state,
                "endorsements": endorsements,
                "board_decision": decision,
                "result": result,
            }
            releases.append(view)
            releases_by_project.setdefault(project_id, []).append(view)

        releases.sort(
            key=lambda release: (
                str(release.get("created_at", "")),
                str(release.get("release_id", "")),
            )
        )
        releases_by_project = {}
        for release in releases:
            releases_by_project.setdefault(str(release["project_id"]), []).append(release)

        projects = []
        blockers: list[dict[str, Any]] = []
        reviews: list[dict[str, Any]] = []
        for project in self.workspace.portfolio().projects:
            ready, reasons = self.workspace.readiness(project.id)
            project_root = self.workspace.project_path(project.id)
            data_path = project_root / "project.json"
            data = load_committed_json(project_root, data_path) or {}
            checkpoints = [
                path
                for path in sorted(
                    (project_root / "checkpoints").glob("*.json"), reverse=True
                )
                if committed_file_matches_head(project_root, path)
            ]
            latest = load_committed_json(project_root, checkpoints[0]) if checkpoints else None
            owner_id = f"{project.id}:owner"
            blocker_resolutions = {
                str(value["blocker_id"]): value
                for path in sorted((project_root / "blockers" / "resolutions").glob("*.json"))
                for value in [load_committed_json(project_root, path)]
                if value is not None
            }
            project_blockers = []
            for path in sorted((project_root / "blockers").glob("*.json")):
                blocker = load_committed_json(project_root, path)
                if blocker is None:
                    continue
                resolution = blocker_resolutions.get(str(blocker.get("blocker_id")))
                if resolution:
                    blocker = {**blocker, "status": "resolved", "resolution": resolution}
                project_blockers.append(blocker)
            project_reviews = [
                value
                for path in sorted(
                    (self.workspace.root / "operations" / project.id / "reviews").glob("*.json")
                )
                for value in [load_committed_json(self.workspace.root, path)]
                if value is not None
            ]
            blockers.extend(project_blockers)
            reviews.extend(project_reviews)
            technical_reviews = [item for item in project_reviews if item.get("verdict")]
            latest_review = (
                sorted(
                    technical_reviews,
                    key=lambda item: (
                        str(item.get("created_at", "")),
                        str(item.get("review_id", "")),
                    ),
                )[-1]
                if technical_reviews
                else None
            )
            project_releases = releases_by_project.get(project.id, [])
            latest_release = project_releases[-1] if project_releases else None
            raw_queue = work_by_owner.get(owner_id, [])
            pending_queue = [item for item in raw_queue if item.get("status") != "done"]
            terminal = (
                latest_release
                if latest_release
                and latest_release["state"] in {"released", "failed"}
                and not pending_queue
                else None
            )
            launch_record = latest_committed_launch_record(
                project_root, project_root / "decisions"
            )
            current_launch_digest = self.workspace.launch_digest(project.id)
            launched = launch_record is not None or project.state in {
                ProjectState.ACTIVE,
                ProjectState.BLOCKED_EXTERNAL,
                ProjectState.ARCHIVED,
            }
            launch_changed = bool(
                launch_record and launch_record.get("launch_digest") != current_launch_digest
            )
            queue = [] if terminal else raw_queue
            owner = identities.get(owner_id)
            owner_runtime_state = owner.get("work_state") if owner else "offline"
            owner_effective_state = (
                "complete"
                if terminal and terminal["state"] == "released"
                else "release_failed"
                if terminal
                else owner_runtime_state
            )
            if pending_queue and latest_release and latest_release["state"] in {
                "released",
                "failed",
            }:
                delivery_state = "executing"
                current_task = "Execute queued work for the next increment"
            elif latest_release and latest_release["state"] == "released":
                delivery_state = "released"
                current_task = "Production release completed"
            elif latest_release and latest_release["state"] == "failed":
                delivery_state = "release_failed"
                current_task = "Review failed production release"
            elif latest_release and latest_release["state"] == "queued":
                delivery_state = "release_approved"
                current_task = "Execute the exact Board-approved production release"
            elif latest_release and latest_release["state"] == "ready":
                delivery_state = "awaiting_board_release"
                current_task = "Await Board production decision"
            elif latest_release and latest_release["state"] == "stale":
                delivery_state = "release_stale"
                current_task = "Refresh the stale release candidate"
            elif latest_release and latest_release["state"] == "rejected":
                delivery_state = "release_rejected"
                current_task = "Review the Board's release rejection"
            elif latest_release:
                delivery_state = "awaiting_release_endorsements"
                current_task = "Await CEO and CTO release endorsements"
            elif latest_review and latest_review.get("verdict") in {
                "needs_correction",
                "blocked",
                "insufficient_evidence",
            }:
                delivery_state = "technical_correction"
                current_task = str(latest_review.get("required_action") or "Resolve CTO review")
            elif latest_review and latest_review.get("verdict") == "on_track":
                delivery_state = "technical_clearance"
                current_task = str(
                    latest_review.get("required_action") or "Prepare the next verified increment"
                )
            elif launched and latest:
                delivery_state = "executing"
                current_task = str(
                    latest.get("next_task") or data.get("current_task") or "Continue"
                )
            elif launched:
                delivery_state = "launched"
                current_task = str(data.get("current_task") or "Execute the approved launch scope")
            else:
                delivery_state = str(project.state)
                current_task = str(
                    data.get("current_task") or (reasons[0] if reasons else "Await launch")
                )
            projects.append(
                {
                    **project.model_dump(mode="json"),
                    "ready": ready,
                    "readiness_reasons": reasons,
                    "launch": {
                        "status": "launched" if launched else "not_launched",
                        "record": launch_record,
                        "current_digest": current_launch_digest,
                        "changed_since_launch": launch_changed,
                        "current_gate_ready": ready,
                    },
                    "delivery_state": delivery_state,
                    "current_spec": data.get("current_spec"),
                    "current_task": current_task,
                    "latest_checkpoint": latest,
                    "owner": owner,
                    "owner_effective_state": owner_effective_state,
                    "queue": queue,
                    "blockers": project_blockers,
                    "reviews": project_reviews,
                }
            )
        attention = []
        bets = list_bets(self.workspace)
        signals = list_signals(self.workspace)
        for bet in bets:
            bet_manifest = bet.get("current_manifest")
            decision = bet.get("latest_decision")
            if bet_manifest and not decision:
                attention.append(
                    {
                        "kind": "bet_decision",
                        "object_id": bet["bet_id"],
                        "summary": f"{bet['bet_id']} R{bet['current_revision']:03d} awaits Board decision",
                    }
                )
            elif decision and decision["kind"] == "more_evidence":
                attention.append(
                    {
                        "kind": "evidence_gap",
                        "object_id": bet["bet_id"],
                        "summary": decision.get("evidence_gap") or decision["reason"],
                    }
                )
        for project in projects:
            terminal_delivery = project["delivery_state"] in {"released", "release_failed"}
            if (
                not project["launch"]["record"]
                and project["readiness_reasons"]
                and project["state"] in {"shaping", "ready"}
            ):
                attention.append(
                    {
                        "kind": "readiness",
                        "project_id": project["id"],
                        "summary": "; ".join(project["readiness_reasons"][:2]),
                    }
                )
            if (
                not terminal_delivery
                and project["owner"]
                and project["owner"]["work_state"] == "blocked"
            ):
                attention.append(
                    {"kind": "blocker", "project_id": project["id"], "summary": "Owner blocked"}
                )
            for blocker in project["blockers"]:
                if blocker.get("status") == "open":
                    attention.append(
                        {
                            "kind": "blocker",
                            "project_id": project["id"],
                            "object_id": blocker.get("blocker_id"),
                            "summary": blocker.get("summary", "Open Owner blocker"),
                        }
                    )
            if not terminal_delivery:
                latest_reviews: dict[str, dict[str, Any]] = {}
                for review in sorted(
                    project["reviews"],
                    key=lambda item: (
                        str(item.get("created_at", "")),
                        str(item.get("review_id", "")),
                    ),
                ):
                    if review.get("verdict"):
                        latest_reviews[str(review.get("affected_scope", ""))] = review
                for review in latest_reviews.values():
                    if review.get("verdict") in {"needs_correction", "blocked"}:
                        attention.append(
                            {
                                "kind": "technical_review",
                                "project_id": project["id"],
                                "object_id": review.get("review_id"),
                                "summary": (
                                    f"{review['verdict']}: {review.get('affected_scope', '')} — "
                                    f"{review.get('required_action', '')}"
                                ),
                            }
                        )
        for action in runtime["pending_actions"]:
            attention.append(
                {"kind": "action", "project_id": action["project_id"], "summary": action["summary"]}
            )
        latest_releases = [
            project_releases[-1]
            for project_releases in releases_by_project.values()
            if project_releases
        ]
        for release in latest_releases:
            release_state = release["state"]
            if release_state == "ready":
                attention.append(
                    {
                        "kind": "release_decision",
                        "project_id": release["project_id"],
                        "object_id": release["release_id"],
                        "summary": "CEO and CTO endorsed; Board production decision required",
                    }
                )
            elif release_state == "stale":
                attention.append(
                    {
                        "kind": "release_stale",
                        "project_id": release["project_id"],
                        "object_id": release["release_id"],
                        "summary": "Release evidence changed; create a new candidate.",
                    }
                )
        roles = {role: identities.get(role) for role in ("CPO", "CEO", "CTO")}
        with self.workspace.runtime.connect() as connection:
            messages = [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM messages
                    WHERE stale_version=0
                    ORDER BY created_at DESC, id DESC LIMIT 200"""
                )
            ]
        return {
            "company": company_manifest.company_name,
            "root": str(self.workspace.root),
            "roles": roles,
            "executives": roles,
            "signals": signals,
            "bets": bets,
            "projects": projects,
            "releases": releases,
            "messages": messages,
            "reviews": reviews,
            "blockers": blockers,
            "attention": attention,
            "capabilities": runtime["capabilities"],
            "unread": runtime["unread"],
        }

    def focus_executive(self, role: str) -> dict[str, Any]:
        return focus_executive(self.workspace, role)

    def add_signal(
        self, title: str, summary: str, source_kind: str, source_title: str, source_uri: str
    ) -> dict[str, Any]:
        return record_signal(
            self.workspace,
            title.strip(),
            summary.strip(),
            [
                {
                    "kind": source_kind.strip() or "board_idea",
                    "title": source_title.strip() or "Board direction",
                    "uri": source_uri.strip() or None,
                }
            ],
        ).model_dump(mode="json")

    def decide_bet(
        self,
        bet_id: str,
        revision: int,
        digest: str,
        decision: str,
        reason: str,
        evidence_gap: str = "",
    ) -> dict[str, Any]:
        return board_decide_bet(
            self.workspace,
            bet_id,
            revision,
            digest,
            decision,
            reason,
            evidence_gap=evidence_gap or None,
            provider_thread_id="oneco-cockpit",
            provider_turn_id=f"cockpit_{uuid.uuid4().hex}",
        ).model_dump(mode="json")

    def decide_release(self, release_id: str, decision: str, reason: str) -> dict[str, Any]:
        return board_decide_release(
            self.workspace,
            release_id,
            decision,
            reason,
            provider_thread_id="oneco-cockpit",
            provider_turn_id=f"cockpit_{uuid.uuid4().hex}",
        ).model_dump(mode="json")

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


def _resources() -> Path:
    return Path(__file__).resolve().parent / "resources" / "bridge"


def socket_path(workspace: Workspace) -> Path:
    return local_socket_path(workspace.root, "bridge")


def _send_existing(socket_path: Path, command: str = "focus") -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.4)
            client.connect(str(socket_path))
            client.sendall(command.encode())
        return True
    except OSError:
        return False


_COCOA_WINDOW_HOST: Any | None = None


def _configure_cocoa_window_host() -> None:
    """Use a normal activating window with standard macOS app stacking."""
    global _COCOA_WINDOW_HOST

    from AppKit import NSApplication, NSApplicationActivationPolicyRegular, NSWindow

    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyRegular
    )

    from webview.platforms.cocoa import BrowserView

    if _COCOA_WINDOW_HOST is None:

        class OneCoWindowHost(NSWindow):
            def canBecomeKeyWindow(self) -> bool:  # noqa: N802 - Cocoa selector
                return bool(getattr(self, "focus", True))

        _COCOA_WINDOW_HOST = OneCoWindowHost
    BrowserView.WindowHost = _COCOA_WINDOW_HOST


def _prepare_native_window(window: Any) -> None:
    """Keep Cockpit at normal level and remove panel-only Space behavior."""
    native = getattr(window, "native", None)
    if native is None:
        return
    try:
        from AppKit import (
            NSNormalWindowLevel,
            NSWindowCollectionBehaviorCanJoinAllApplications,
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorFullScreenAuxiliary,
            NSWindowCollectionBehaviorManaged,
            NSWindowCollectionBehaviorStationary,
            NSWindowStyleMaskNonactivatingPanel,
        )

        native.setLevel_(NSNormalWindowLevel)
        native.setStyleMask_(native.styleMask() & ~NSWindowStyleMaskNonactivatingPanel)
        if hasattr(native, "setFloatingPanel_"):
            native.setFloatingPanel_(False)
        native.setHidesOnDeactivate_(False)
        native.setCollectionBehavior_(
            (native.collectionBehavior() & ~(
                NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorCanJoinAllApplications
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorStationary
            )) | NSWindowCollectionBehaviorManaged
        )
    except (AttributeError, ImportError, TypeError):
        # Non-Cocoa test doubles and future pywebview backends can still use
        # the normal show/restore path below.
        return


def _move_native_window_to_active_space(
    window: Any, target_space: int | None = None
) -> None:
    """Keep the Cockpit on the Space where the Board invoked it."""

    native = getattr(window, "native", None)
    if native is None:
        return
    if target_space is None:
        target_space = ghostty._active_space_id()

    def apply() -> None:
        native = getattr(window, "native", None)
        if native is None:
            return
        try:
            from AppKit import NSApplication

            ghostty._move_window_to_space(int(native.windowNumber()), target_space)
            application = NSApplication.sharedApplication()
            application.unhide_(None)
            window.show()
            window.restore()
            native.makeKeyAndOrderFront_(None)
            native.orderFrontRegardless()
            application.activateIgnoringOtherApps_(True)
        except (AttributeError, ImportError, TypeError, ValueError):
            # pywebview backends other than Cocoa do not expose NSWindow behavior.
            return

    if threading.current_thread() is threading.main_thread():
        apply()
        return
    try:
        from PyObjCTools import AppHelper

        AppHelper.callAfter(apply)
    except ImportError:
        return


def run_bridge(
    workspace: Workspace,
    *,
    launch_executives: bool = True,
    permission: str = DEFAULT_PERMISSION_MODE,
) -> None:
    if os.uname().sysname != "Darwin":
        raise WorkspaceError("OneCo Bridge v1 requires macOS")
    _require_active_display()
    bridge_socket = socket_path(workspace)
    if _send_existing(bridge_socket):
        return
    invocation_space = ghostty._active_space_id()
    try:
        bridge_socket.unlink(missing_ok=True)
        _configure_cocoa_window_host()
        import webview
    except ImportError as exc:
        raise WorkspaceError("pywebview is not installed; reinstall OneCo on macOS") from exc

    collect_health(workspace)
    if launch_executives:
        for role in ("CPO", "CEO", "CTO"):
            try:
                focus_executive(workspace, role, permission=permission)
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
    before_show = getattr(getattr(window, "events", None), "before_show", None)
    if before_show is not None:
        before_show += lambda: _prepare_native_window(window)
    loaded = getattr(getattr(window, "events", None), "loaded", None)
    if loaded is not None:
        loaded += lambda: (
            _prepare_native_window(window),
            _move_native_window_to_active_space(window, invocation_space),
        )
    shown = getattr(getattr(window, "events", None), "shown", None)
    if shown is not None:
        shown += lambda: (
            _prepare_native_window(window),
            _move_native_window_to_active_space(window, invocation_space),
        )
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(bridge_socket))
    os.chmod(bridge_socket, 0o600)
    server.listen(2)

    def listen() -> None:
        while True:
            try:
                client, _ = server.accept()
                command = client.recv(32).decode()
                client.close()
                if command == "focus":
                    _move_native_window_to_active_space(window)
                elif command in {"CPO", "CEO", "CTO"}:
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
            server.close()
            bridge_socket.unlink(missing_ok=True)

    window.events.closed += closed
    try:
        webview.start(gui="cocoa", debug=False, private_mode=False)
    finally:
        closed()
