"""Validated protocol models shared by the CLI and runtime."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class ProjectState(StrEnum):
    REGISTERED = "registered"
    SHAPING = "shaping"
    READY = "ready"
    ACTIVE = "active"
    BLOCKED_EXTERNAL = "blocked_external"
    ARCHIVED = "archived"


class SessionLifecycle(StrEnum):
    STARTING = "starting"
    LIVE = "live"
    LOST = "lost"
    SUPERSEDED = "superseded"
    CLOSED = "closed"


class WorkState(StrEnum):
    IDLE = "idle"
    WORKING = "working"
    WAITING = "waiting"
    BLOCKED = "blocked"


class AuthorityKind(StrEnum):
    CANONICAL = "canonical"
    OWNER = "owner"
    SECRETARY = "secretary"
    CANDIDATE = "candidate"


class DesktopConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cockpit: str = "macos-webview"
    executives: str = "ghostty"
    owners: str = "tmux"


class ProjectRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^PROJ-[0-9]{3,}$")
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    path: str
    state: ProjectState = ProjectState.REGISTERED
    current_spec: str | None = None
    current_task: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("path")
    @classmethod
    def relative_path_only(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("project path must be relative and cannot escape the company root")
        return path.as_posix()


class Portfolio(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    next_project_number: int = Field(default=1, ge=1)
    projects: list[ProjectRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_projects(self) -> Portfolio:
        ids = [item.id for item in self.projects]
        paths = [item.path for item in self.projects]
        if len(ids) != len(set(ids)):
            raise ValueError("project IDs must be unique")
        if len(paths) != len(set(paths)):
            raise ValueError("project paths must be unique")
        return self


class CompanyManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=2, ge=1, le=2)
    company_name: str = Field(min_length=1, max_length=120)
    language: str = "en"
    provider: str = "trae"
    model: str = "GPT-5.6-Sol"
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)
    # Read-only compatibility for v0.1 manifests. Workspace.open migrates it away.
    terminal: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpoint_id: str
    project_id: str = Field(pattern=r"^PROJ-[0-9]{3,}$")
    spec_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    result: str = Field(min_length=1)
    commit: str | None = None
    key_files: list[str] = Field(default_factory=list)
    verification: list[str] = Field(min_length=1)
    spec_deviation: str = "none"
    complexity_change: str = "none"
    dependency_change: str = "none"
    risk_or_blocker: str = "none"
    next_task: str = Field(min_length=1)
    reuse_candidate: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("key_files")
    @classmethod
    def safe_key_files(cls, values: list[str]) -> list[str]:
        for value in values:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("key_files must be project-relative paths")
        return values


class InstructionAction(StrEnum):
    ALIGN = "ALIGN"
    NARROW = "NARROW"
    RESEQUENCE = "RESEQUENCE"
    CONTINUE = "CONTINUE"
    CORRECT = "CORRECT"
    SIMPLIFY = "SIMPLIFY"
    HARDEN = "HARDEN"
    ESCALATE = "ESCALATE"


CEO_ACTIONS = {
    InstructionAction.ALIGN,
    InstructionAction.NARROW,
    InstructionAction.RESEQUENCE,
    InstructionAction.CONTINUE,
    InstructionAction.ESCALATE,
}
CTO_ACTIONS = {
    InstructionAction.CORRECT,
    InstructionAction.SIMPLIFY,
    InstructionAction.HARDEN,
    InstructionAction.CONTINUE,
    InstructionAction.ESCALATE,
}


class Instruction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction_id: str
    sender_authority: str
    action: InstructionAction
    project_id: str = Field(pattern=r"^PROJ-[0-9]{3,}$")
    spec_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    observed: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    required_action: str = Field(min_length=1)
    do_not: str = Field(min_length=1)
    acceptance_delta: str = "none"
    effective_boundary: str = "after-current-atomic-task"
    requires_spec_revision: bool = False
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("sender_authority", mode="before")
    @classmethod
    def normalize_authority(cls, value: object) -> str:
        return str(value).upper()

    @model_validator(mode="after")
    def authority_owns_action(self) -> Instruction:
        authority = self.sender_authority.upper()
        if authority == "CEO" and self.action not in CEO_ACTIONS:
            raise ValueError(f"CEO cannot issue {self.action}")
        if authority == "CTO" and self.action not in CTO_ACTIONS:
            raise ValueError(f"CTO cannot issue {self.action}")
        if authority not in {"CEO", "CTO"}:
            raise ValueError("instructions must be issued by CEO or CTO")
        return self


class ActionDomain(StrEnum):
    PRODUCT = "product"
    TECHNICAL = "technical"
    CROSS_DOMAIN = "cross_domain"


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str
    requester: str
    project_id: str = Field(pattern=r"^PROJ-[0-9]{3,}$")
    domain: ActionDomain
    summary: str = Field(min_length=1)
    effect: str = Field(min_length=1)
    reversible: bool
    created_at: datetime = Field(default_factory=utc_now)


class StatusSnapshot(BaseModel):
    company: str
    projects: list[dict[str, Any]]
    identities: list[dict[str, Any]]
    unread: dict[str, int]
    warnings: list[str] = Field(default_factory=list)
