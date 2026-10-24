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
    ADVISORY = "advisory"
    OWNER = "owner"
    SECRETARY = "secretary"
    CANDIDATE = "candidate"


class IdentityKind(StrEnum):
    PERSISTENT_ROLE = "persistent_role"
    OWNER = "owner"
    DELEGATE = "delegate"
    CANDIDATE = "candidate"


class CompanyRole(StrEnum):
    BOARD = "BOARD"
    CPO = "CPO"
    CEO = "CEO"
    CTO = "CTO"
    OWNER = "OWNER"


class SignalStatus(StrEnum):
    RECORDED = "recorded"
    ASSIGNED = "assigned"
    INVESTIGATING = "investigating"
    LINKED = "linked"
    CLOSED = "closed"


class BetRevisionState(StrEnum):
    DRAFT = "draft"
    READY_FOR_BOARD = "ready_for_board"
    MORE_EVIDENCE = "more_evidence"
    SELECTED = "selected"
    REJECTED = "rejected"
    PROMOTED = "promoted"
    SUPERSEDED = "superseded"


class BoardDecisionKind(StrEnum):
    SELECT = "select"
    REJECT = "reject"
    MORE_EVIDENCE = "more_evidence"
    REOPEN = "reopen"
    APPROVE_RELEASE = "approve_release"
    REJECT_RELEASE = "reject_release"


class ValidationState(StrEnum):
    NOT_RUN = "not_run"
    TBD = "tbd"
    PASSED = "passed"
    MIXED = "mixed"
    FAILED = "failed"
    INVALID = "invalid"


class ReviewVerdict(StrEnum):
    ON_TRACK = "on_track"
    NEEDS_CORRECTION = "needs_correction"
    BLOCKED = "blocked"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ReleaseState(StrEnum):
    CANDIDATE = "candidate"
    STALE = "stale"
    READY = "ready"
    APPROVED = "approved"
    QUEUED = "queued"
    RELEASED = "released"
    REJECTED = "rejected"
    FAILED = "failed"


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
    source_bet_id: str | None = None
    source_bet_revision: int | None = Field(default=None, ge=1)
    source_bet_digest: str | None = None
    promotion_id: str | None = None
    bet_change_pending: bool = False
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

    schema_version: int = Field(default=3, ge=1, le=3)
    company_name: str = Field(min_length=1, max_length=120)
    language: str = "en"
    provider: str = "trae"
    model: str = "GPT-5.6-Sol"
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)
    # Read-only compatibility for v0.1 manifests. Workspace.open migrates it away.
    terminal: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class SourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(pattern=r"^SRC-[0-9]{3,}$")
    kind: str = Field(min_length=1)
    title: str = Field(min_length=1)
    uri: str | None = None
    observed_at: datetime
    captured_path: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    portable: bool = True

    @model_validator(mode="after")
    def has_location(self) -> SourceReference:
        if not self.uri and not self.captured_path:
            raise ValueError("source requires a URI or captured path")
        return self


class SignalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_id: str = Field(pattern=r"^SIG-[0-9]{3,}$")
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1)
    status: SignalStatus = SignalStatus.RECORDED
    assigned_by: str = "BOARD"
    source_ids: list[str] = Field(min_length=1)
    linked_bet_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    path: str | None = None
    uri: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    observed_at: datetime
    source_skill: str | None = None
    portable: bool = True

    @model_validator(mode="after")
    def has_location(self) -> EvidenceReference:
        if not self.path and not self.uri:
            raise ValueError("evidence requires a path or URI")
        return self


class BetRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bet_id: str = Field(pattern=r"^BET-[0-9]{3,}$")
    title: str = Field(min_length=1, max_length=160)
    signal_ids: list[str] = Field(min_length=1)
    variant_of: str | None = None
    duplicate_of: str | None = None
    current_revision: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)


class BetRevisionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bet_id: str = Field(pattern=r"^BET-[0-9]{3,}$")
    revision: int = Field(ge=1)
    state: BetRevisionState
    validation_state: ValidationState = ValidationState.NOT_RUN
    packet_path: str
    packet_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence: list[EvidenceReference] = Field(default_factory=list)
    composite_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    supersedes_revision: int | None = Field(default=None, ge=1)
    submitted_by: str
    submitted_at: datetime = Field(default_factory=utc_now)


class BoardDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str
    kind: BoardDecisionKind
    bet_id: str | None = None
    revision: int | None = Field(default=None, ge=1)
    packet_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    project_id: str | None = None
    release_id: str | None = None
    release_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1)
    evidence_gap: str | None = None
    provider_thread_id: str | None = None
    provider_turn_id: str | None = None
    supersedes: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def decision_target_is_complete(self) -> BoardDecision:
        if self.kind in {
            BoardDecisionKind.SELECT,
            BoardDecisionKind.REJECT,
            BoardDecisionKind.MORE_EVIDENCE,
            BoardDecisionKind.REOPEN,
        }:
            if not self.bet_id or self.revision is None or not self.packet_digest:
                raise ValueError("Bet decisions require bet, revision, and packet digest")
        if self.kind in {
            BoardDecisionKind.APPROVE_RELEASE,
            BoardDecisionKind.REJECT_RELEASE,
        } and (not self.project_id or not self.release_id or not self.release_digest):
            raise ValueError("release decisions require project, release, and digest")
        if self.kind == BoardDecisionKind.MORE_EVIDENCE and not self.evidence_gap:
            raise ValueError("more-evidence decisions require a named evidence gap")
        return self


class PromotionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    promotion_id: str
    bet_id: str = Field(pattern=r"^BET-[0-9]{3,}$")
    revision: int = Field(ge=1)
    packet_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    board_decision_id: str
    project_id: str = Field(pattern=r"^PROJ-[0-9]{3,}$")
    project_commit: str | None = None
    promoted_by: str = "CEO"
    created_at: datetime = Field(default_factory=utc_now)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_id: str
    project_id: str = Field(pattern=r"^PROJ-[0-9]{3,}$")
    checkpoint_id: str | None = None
    reason: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    requested_by: str
    status: str = "open"
    created_at: datetime = Field(default_factory=utc_now)


class TechnicalReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_id: str
    project_id: str = Field(pattern=r"^PROJ-[0-9]{3,}$")
    checkpoint_id: str | None = None
    reviewer: str = "CTO"
    verdict: ReviewVerdict
    evidence: str = Field(min_length=1)
    required_action: str = Field(min_length=1)
    affected_scope: str = Field(min_length=1)
    next_review_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class SupervisionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_minutes: int = Field(default=240, ge=15)
    progress_pulse_target_minutes: int = Field(default=15, ge=1)
    changed_by: str = "BOARD"
    reason: str = "OneCo v1 default"
    updated_at: datetime = Field(default_factory=utc_now)


class ReleaseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_id: str = Field(pattern=r"^rel_[a-f0-9]{32}$")
    project_id: str = Field(pattern=r"^PROJ-[0-9]{3,}$")
    product_commit: str = Field(min_length=1)
    release_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    acceptance_evidence: list[str] = Field(min_length=1)
    acceptance_evidence_hashes: dict[str, str] = Field(default_factory=dict)
    state: ReleaseState = ReleaseState.CANDIDATE
    ceo_endorsed_digest: str | None = None
    cto_endorsed_digest: str | None = None
    board_decision_id: str | None = None
    result_evidence: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


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
    source_instruction_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("key_files")
    @classmethod
    def safe_key_files(cls, values: list[str]) -> list[str]:
        for value in values:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("key_files must be project-relative paths")
        return values


class BlockerRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocker_id: str
    project_id: str = Field(pattern=r"^PROJ-[0-9]{3,}$")
    reported_by: str
    recorded_session_id: str | None = None
    recorded_epoch: int | None = Field(default=None, ge=1)
    summary: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    requested_decision: str = Field(min_length=1)
    status: str = "open"
    created_at: datetime = Field(default_factory=utc_now)


class BlockerResolutionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution_id: str
    blocker_id: str
    project_id: str = Field(pattern=r"^PROJ-[0-9]{3,}$")
    resolved_by: str
    recorded_session_id: str | None = None
    recorded_epoch: int | None = Field(default=None, ge=1)
    checkpoint_id: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


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
