"""Versioned company and project workspace operations."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import AuthorizationError, ConflictError, ProjectError, WorkspaceError
from .models import CompanyManifest, DesktopConfig, Portfolio, ProjectRecord, ProjectState
from .runtime import Runtime
from .util import atomic_json, load_json, safe_child, slugify

COMPANY_TEMPLATE = """# {name}

## Mission

Build useful products as a one-person AI-native company through clear context, autonomous execution, and short verified loops.

## Operating principles

- Always Day 1. Keep teams small and paths short.
- Use the minimum sufficient process and implementation.
- Be direct, candid, and evidence-led.
- Inspect first-hand when a decision depends on reality.
- Prefer real outcomes over polished reporting.
- Provide context, then let the responsible owner execute.
- Hold the core path to a high standard without polishing everything forever.
- Promote reusable wins and failure lessons into the playbook.

## Roles

- The human Board shapes products with CEO and CTO and decides company direction, product activation/archival, and strategic conflicts.
- CEO co-designs products and protects user intent, scope, momentum, and simplicity. CEO does not allocate coding tokens or stop products for compute scarcity.
- CTO co-designs technical direction and protects correctness, safety, maintainability, and simplicity. CTO may block an unsafe merge or external action, not the whole product.
- Product Owners continuously execute their approved Specs inside their own project boundary.

## External actions

Owners request approval before external actions. CEO approves product/market actions; CTO approves technical/data/infrastructure actions; cross-domain actions require both. Strategic conflicts return to the Board.
"""

ROLE_TEMPLATES = {
    "CEO.md": """# CEO

Co-design products with the Board and CTO. Guard user intent, scope, momentum, and simplicity. Review structured checkpoints, not terminal transcripts. Use ALIGN, NARROW, RESEQUENCE, CONTINUE, or ESCALATE. Never allocate coding tokens, rank unrelated products, modify product code, or archive a product.
""",
    "CTO.md": """# CTO

Co-design the minimum sound technical path. Guard architecture, correctness, safety, maintainability, and complexity. Review structured checkpoints, not terminal transcripts. Use CORRECT, SIMPLIFY, HARDEN, CONTINUE, or ESCALATE. Block only affected unsafe work; never change product intent or archive a product.
""",
    "OWNER.md": """# Product Owner

Own the approved Spec end to end inside this project. Complete the smallest runnable vertical result, verify it, checkpoint honestly, read the inbox, and continue the next compatible task without waiting. Never silently change product intent, write another project, or perform an unapproved external action.

Compete on closure, commitment, simplicity, truth, and reuse. Never compete on code volume, token use, online time, feature count, or report length.
""",
}

PROJECT_AGENTS = """# Project operating contract

Read BRIEF.md, project.json, the current Spec, and the OneCo inbox before work. Write only this project. Use the approved Spec Kit task queue. A task is complete only with verification evidence. After a meaningful checkpoint, continue the next compatible task; wait only for an unsafe, irreversible, conflicting, or product-changing decision.

## Five races

- Closure: deliver the smallest runnable vertical result.
- Commitment: complete the Spec you explicitly accepted.
- Simplicity: use fewer layers, services, and dependencies.
- Truth: expose failure, drift, and uncertainty early.
- Reuse: promote useful patterns for other projects.

Do not compete on code volume, token use, online time, feature count, or report length.
"""

BRIEF_TEMPLATE = """# Product Brief

Status: DRAFT — Board, CEO, and CTO must shape this before Owner launch.

## User and situation

TBD

## Problem and desired outcome

TBD

## Smallest useful vertical result

TBD

## Scope and explicit non-goals

TBD

## Acceptance evidence

TBD

## Known unknowns and escalation triggers

TBD
"""

CONSTITUTION_TEMPLATE = """# Project Constitution

## Stable engineering principles

1. Deliver the current vertical Spec before generalized infrastructure.
2. Prefer one process, one datastore, and direct calls until evidence requires more.
3. Correctness, recovery, privacy, and security are part of done.
4. New frameworks, services, databases, shared abstractions, and irreversible migrations require explicit justification.
5. Tests and observable runtime evidence determine completion.

## Project-specific overlay

TBD by CTO before Owner launch.
"""

SPEC_SEED_TEMPLATE = """# DRAFT-SEED — First Vertical Result

## User and situation
TBD by CEO with Board.

## Desired behavior and outcome
TBD by CEO with Board.

## Smallest vertical result
TBD.

## Acceptance examples
TBD.

## Explicit non-goals
TBD.

## Technical constraints and quality floor
TBD by CTO.

## Known unknowns and escalation triggers
TBD.
"""


def _write_new(path: Path, text: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ensure_lines(path: Path, required: list[str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    changed = False
    for line in required:
        if line not in lines:
            lines.append(line)
            changed = True
    if changed or not path.exists():
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _git_init(path: Path) -> None:
    if (path / ".git").exists():
        return
    result = subprocess.run(
        ["git", "init", "-q", str(path)], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise WorkspaceError(result.stderr.strip() or f"git init failed in {path}")


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.manifest_path = self.root / ".oneco" / "manifest.json"
        self.portfolio_path = self.root / "portfolio" / "projects.json"
        self.runtime = Runtime(self.root / ".oneco" / "runtime.sqlite")

    @classmethod
    def initialize(
        cls,
        root: Path,
        name: str,
        *,
        language: str = "en",
        provider: str = "trae",
        model: str = "GPT-5.6-Sol",
        terminal: str = "tmux",
        git_init: bool = True,
    ) -> Workspace:
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        workspace = cls(root)
        if workspace.manifest_path.exists():
            raise ConflictError(f"already a OneCo company: {root}")
        for directory in (
            root / "roles",
            root / "portfolio" / "decisions",
            root / "portfolio" / "approvals",
            root / "playbook",
            root / ".oneco" / "backups",
            root / ".oneco" / "logs",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        desktop = DesktopConfig(owners=terminal)
        manifest = CompanyManifest(
            company_name=name, language=language, provider=provider, model=model, desktop=desktop
        )
        atomic_json(workspace.manifest_path, manifest.model_dump(mode="json", exclude_none=True))
        atomic_json(workspace.portfolio_path, Portfolio().model_dump(mode="json"))
        _write_new(root / "COMPANY.md", COMPANY_TEMPLATE.format(name=name))
        for filename, content in ROLE_TEMPLATES.items():
            _write_new(root / "roles" / filename, content)
        _write_new(
            root / "playbook" / "README.md",
            "# Playbook\n\nPromote only demonstrated, reusable patterns and failure lessons.\n",
        )
        _ensure_lines(
            root / ".gitignore",
            [
                ".oneco/runtime.sqlite*",
                ".oneco/bridge.sock",
                ".oneco/backups/",
                ".oneco/logs/",
                ".DS_Store",
            ],
        )
        workspace.runtime.initialize()
        if git_init:
            _git_init(root)
        return workspace

    @classmethod
    def open(cls, root: Path) -> Workspace:
        workspace = cls(root)
        if not workspace.manifest_path.is_file():
            raise WorkspaceError(f"not a OneCo company: {root}")
        workspace._migrate_manifest()
        workspace.manifest()
        workspace.portfolio()
        workspace._backup_runtime_v1()
        workspace.runtime.initialize()
        return workspace

    def manifest(self) -> CompanyManifest:
        return CompanyManifest.model_validate(load_json(self.manifest_path))

    def _migrate_manifest(self) -> None:
        data = load_json(self.manifest_path)
        if int(data.get("schema_version", 1)) >= 2 and "desktop" in data:
            return
        backup = self.root / ".oneco" / "backups" / "manifest-v1.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(self.manifest_path, backup)
        legacy_terminal = str(data.pop("terminal", "tmux"))
        data["schema_version"] = 2
        data["desktop"] = {
            "cockpit": "macos-webview",
            "executives": "ghostty",
            "owners": legacy_terminal,
        }
        atomic_json(self.manifest_path, data)

    def _backup_runtime_v1(self) -> None:
        if not self.runtime.database.is_file():
            return
        backup = self.root / ".oneco" / "backups" / "runtime-v1.sqlite"
        if backup.exists():
            return
        try:
            with self.runtime.connect() as connection:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
        except sqlite3.Error:
            return
        if row and str(row[0]) == "1":
            self.runtime.backup_to(backup)

    def portfolio(self) -> Portfolio:
        return Portfolio.model_validate(load_json(self.portfolio_path))

    def save_portfolio(self, portfolio: Portfolio) -> None:
        atomic_json(self.portfolio_path, portfolio.model_dump(mode="json"))

    def find_project(self, project_id: str) -> ProjectRecord:
        for project in self.portfolio().projects:
            if project.id == project_id:
                return project
        raise ProjectError(f"unknown project: {project_id}")

    def project_path(self, project_id: str) -> Path:
        project = self.find_project(project_id)
        return safe_child(self.root, project.path)

    def register_project(
        self, name: str, path: Path, *, initialize_git: bool = True, scaffold: bool = True
    ) -> ProjectRecord:
        target = path.resolve() if path.is_absolute() else (self.root / path).resolve()
        if target.parent != self.root:
            raise ProjectError(
                "v0.1 product repositories must be direct children of the company root"
            )
        target.mkdir(parents=True, exist_ok=True)
        relative = target.relative_to(self.root).as_posix()
        portfolio = self.portfolio()
        if any(item.path == relative for item in portfolio.projects):
            raise ConflictError(f"project path is already registered: {relative}")
        project_id = f"PROJ-{portfolio.next_project_number:03d}"
        project = ProjectRecord(id=project_id, name=name, slug=slugify(name), path=relative)

        # Ignore the nested repository before initializing it so the company repository never stages a gitlink.
        _ensure_lines(self.root / ".gitignore", [f"/{relative}/"])
        if initialize_git:
            _git_init(target)
        if scaffold:
            self._scaffold_project(target, project)
        portfolio.projects.append(project)
        portfolio.next_project_number += 1
        self.save_portfolio(portfolio)
        self.runtime.event(
            "project.registered", "BOARD", project.model_dump(mode="json"), project_id
        )
        return project

    def _scaffold_project(self, target: Path, project: ProjectRecord) -> None:
        for directory in (
            "spec-seeds",
            "specs",
            "checkpoints",
            "instructions",
            "decisions",
            "evidence",
            "handoffs",
            ".specify/memory",
        ):
            (target / directory).mkdir(parents=True, exist_ok=True)
        project_data: dict[str, Any] = {
            "schema_version": 1,
            "id": project.id,
            "name": project.name,
            "state": project.state,
            "current_spec": None,
            "current_task": None,
            "latest_checkpoint": None,
        }
        if not (target / "project.json").exists():
            atomic_json(target / "project.json", project_data)
        _write_new(target / "BRIEF.md", BRIEF_TEMPLATE)
        _write_new(target / "AGENTS.md", PROJECT_AGENTS)
        _write_new(target / ".specify" / "memory" / "constitution.md", CONSTITUTION_TEMPLATE)
        _write_new(target / "spec-seeds" / "001-first-vertical.md", SPEC_SEED_TEMPLATE)

    def readiness(self, project_id: str) -> tuple[bool, list[str]]:
        target = self.project_path(project_id)
        missing: list[str] = []
        required = [
            "project.json",
            "BRIEF.md",
            "AGENTS.md",
            ".specify/memory/constitution.md",
            "spec-seeds/001-first-vertical.md",
        ]
        for item in required:
            path = target / item
            if not path.is_file():
                missing.append(f"missing {item}")
                continue
            text = path.read_text(encoding="utf-8")
            if "TBD" in text or "DRAFT —" in text:
                missing.append(f"incomplete {item}")
        digest = self.launch_digest(project_id) if not missing else None
        packet = self.runtime.launch_packet(project_id)
        endorsements = target / "decisions" / "launch-endorsements.json"
        if packet:
            if packet.get("ceo_endorsed_digest") != digest:
                missing.append("missing CEO endorsement for current launch packet")
            if packet.get("cto_endorsed_digest") != digest:
                missing.append("missing CTO endorsement for current launch packet")
        elif not endorsements.is_file():
            missing.append("missing CEO/CTO launch endorsements")
        else:
            data = load_json(endorsements)

            def endorsed(value: object) -> bool:
                return value is True or (isinstance(value, dict) and value.get("endorsed") is True)

            if not endorsed(data.get("CEO")) or not endorsed(data.get("CTO")):
                missing.append("both CEO and CTO endorsements are required")
        if not (target / ".git").exists():
            missing.append("project is not an independent Git repository")
        return not missing, missing

    def launch_digest(self, project_id: str) -> str:
        target = self.project_path(project_id)
        digest = hashlib.sha256()
        paths = [
            target / "BRIEF.md",
            target / "AGENTS.md",
            target / ".specify" / "memory" / "constitution.md",
        ]
        spec_paths = sorted((target / "specs").glob("*/spec.md"))
        paths.extend(spec_paths or [target / "spec-seeds" / "001-first-vertical.md"])
        for path in paths:
            digest.update(path.relative_to(target).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes() if path.is_file() else b"<missing>")
            digest.update(b"\0")
        return digest.hexdigest()

    def endorse_launch(self, project_id: str, actor: str, note: str) -> Path:
        """Record CEO or CTO readiness endorsement and advance shaping state deterministically."""
        actor = actor.upper()
        if actor not in {"CEO", "CTO"}:
            raise AuthorizationError("only CEO or CTO can endorse an Owner launch")
        target = self.project_path(project_id)
        path = target / "decisions" / "launch-endorsements.json"
        data = (
            load_json(path)
            if path.exists()
            else {
                "schema_version": 1,
                "project_id": project_id,
                "CEO": None,
                "CTO": None,
            }
        )
        digest = self.launch_digest(project_id)
        data[actor] = {
            "endorsed": True,
            "digest": digest,
            "note": note,
            "created_at": datetime.now(UTC).isoformat(),
        }
        atomic_json(path, data)
        self.runtime.record_launch_endorsement(project_id, actor, digest)
        portfolio = self.portfolio()
        for index, project in enumerate(portfolio.projects):
            if project.id == project_id:
                new_state = ProjectState.SHAPING
                packet = self.runtime.launch_packet(project_id)
                if (
                    packet
                    and packet.get("ceo_endorsed_digest") == digest
                    and packet.get("cto_endorsed_digest") == digest
                ):
                    # Ready still requires complete artifacts; do not make endorsement a substitute.
                    new_state = (
                        ProjectState.READY
                        if self._artifacts_complete(project_id)
                        else ProjectState.SHAPING
                    )
                updated = project.model_copy(
                    update={"state": new_state, "updated_at": datetime.now(UTC)}
                )
                portfolio.projects[index] = updated
                self.save_portfolio(portfolio)
                project_data = load_json(target / "project.json")
                project_data["state"] = new_state
                atomic_json(target / "project.json", project_data)
                break
        self.runtime.event("project.launch_endorsed", actor, {"note": note}, project_id)
        return path

    def _artifacts_complete(self, project_id: str) -> bool:
        target = self.project_path(project_id)
        for item in (
            "BRIEF.md",
            ".specify/memory/constitution.md",
            "spec-seeds/001-first-vertical.md",
        ):
            path = target / item
            if not path.is_file():
                return False
            text = path.read_text(encoding="utf-8")
            if "TBD" in text or "DRAFT —" in text:
                return False
        return True

    def set_state(self, project_id: str, state: ProjectState, *, actor: str) -> ProjectRecord:
        actor = actor.upper()
        if state in {ProjectState.ACTIVE, ProjectState.ARCHIVED} and actor != "BOARD":
            raise AuthorizationError(f"only BOARD can set a project to {state}")
        portfolio = self.portfolio()
        for index, project in enumerate(portfolio.projects):
            if project.id != project_id:
                continue
            if state == ProjectState.ACTIVE:
                ready, reasons = self.readiness(project_id)
                if not ready:
                    raise ProjectError("project is not ready: " + "; ".join(reasons))
            updated = project.model_copy(update={"state": state, "updated_at": datetime.now(UTC)})
            portfolio.projects[index] = updated
            self.save_portfolio(portfolio)
            data = load_json(self.project_path(project_id) / "project.json")
            data["state"] = state
            atomic_json(self.project_path(project_id) / "project.json", data)
            self.runtime.event("project.state_changed", actor, {"state": state}, project_id)
            return updated
        raise ProjectError(f"unknown project: {project_id}")

    def activate_from_launch(
        self, project_id: str, *, actor: str, board_override_turn_id: str | None = None
    ) -> ProjectRecord:
        actor = actor.upper()
        if actor not in {"CEO", "CTO"}:
            raise AuthorizationError("only a canonical CEO or CTO can launch an Owner")
        if not self._artifacts_complete(project_id):
            raise ProjectError("launch packet is incomplete; Board override cannot bypass artifacts")
        digest = self.launch_digest(project_id)
        packet = self.runtime.launch_packet(project_id) or {}
        endorsed = (
            packet.get("ceo_endorsed_digest") == digest
            and packet.get("cto_endorsed_digest") == digest
        )
        if not endorsed and not board_override_turn_id:
            raise ProjectError("current launch packet requires CEO and CTO endorsement")
        portfolio = self.portfolio()
        for index, project in enumerate(portfolio.projects):
            if project.id != project_id:
                continue
            updated = project.model_copy(
                update={"state": ProjectState.ACTIVE, "updated_at": datetime.now(UTC)}
            )
            portfolio.projects[index] = updated
            self.save_portfolio(portfolio)
            data = load_json(self.project_path(project_id) / "project.json")
            data["state"] = ProjectState.ACTIVE
            atomic_json(self.project_path(project_id) / "project.json", data)
            self.runtime.mark_launched(project_id, digest, board_override_turn_id)
            self.runtime.event(
                "project.launched",
                actor,
                {"digest": digest, "override": board_override_turn_id},
                project_id,
            )
            return updated
        raise ProjectError(f"unknown project: {project_id}")
