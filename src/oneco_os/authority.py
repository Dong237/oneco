"""Runtime-derived organizational authority for conversational tools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AuthorizationError, WorkspaceError
from .models import AuthorityKind, IdentityKind
from .workspace import Workspace


@dataclass(frozen=True)
class AgentContext:
    workspace: Workspace
    session_id: str
    logical_id: str
    epoch: int
    authority_kind: str
    provider_thread_id: str | None
    identity_kind: str | None = None

    @property
    def canonical_role(self) -> str | None:
        identity = self.identity_kind or IdentityKind.PERSISTENT_ROLE
        if (
            self.authority_kind == AuthorityKind.CANONICAL
            and identity == IdentityKind.PERSISTENT_ROLE
            and self.logical_id in {"CEO", "CTO"}
        ):
            return self.logical_id
        return None

    @property
    def persistent_role(self) -> str | None:
        identity = self.identity_kind or (
            IdentityKind.PERSISTENT_ROLE
            if self.authority_kind == AuthorityKind.CANONICAL
            else None
        )
        expected_authority = {
            "CPO": AuthorityKind.ADVISORY,
            "CEO": AuthorityKind.CANONICAL,
            "CTO": AuthorityKind.CANONICAL,
        }
        if (
            identity == IdentityKind.PERSISTENT_ROLE
            and self.logical_id in expected_authority
            and self.authority_kind == expected_authority[self.logical_id]
        ):
            return self.logical_id
        return None


ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "CPO": frozenset(
        {
            "bet.write",
            "bet.submit",
            "bet.advise",
            "delegate.create",
            "message.send",
        }
    ),
    "CEO": frozenset(
        {
            "project.promote",
            "project.shape_product",
            "project.endorse",
            "project.launch",
            "owner.instruct_product",
            "action.decide_product",
            "release.endorse_product",
            "delegate.create",
            "message.send",
        }
    ),
    "CTO": frozenset(
        {
            "project.shape_technical",
            "project.endorse",
            "project.launch",
            "owner.instruct_technical",
            "owner.review",
            "action.decide_technical",
            "release.endorse_technical",
            "supervision.configure",
            "delegate.create",
            "message.send",
        }
    ),
}


def resolve_agent_context(environment: dict[str, str]) -> AgentContext:
    root = environment.get("ONECO_ROOT")
    session_id = environment.get("ONECO_SESSION_ID")
    if not root or not session_id:
        raise WorkspaceError("OneCo tools are available only inside a launched OneCo agent")
    workspace = Workspace.open(Path(root))
    row = workspace.runtime.assert_active_session(session_id)
    return AgentContext(
        workspace=workspace,
        session_id=session_id,
        logical_id=str(row["logical_id"]),
        epoch=int(row["epoch"]),
        authority_kind=str(row["authority_kind"]),
        provider_thread_id=row["provider_session_id"],
        identity_kind=row["identity_kind"],
    )


def require_canonical(context: AgentContext, *roles: str) -> str:
    context.workspace.runtime.assert_current(context.session_id, context.epoch)
    role = context.canonical_role
    if role is None or (roles and role not in roles):
        allowed = " or ".join(roles) if roles else "canonical executive"
        raise AuthorizationError(f"this operation requires {allowed} authority")
    return role


def require_role(context: AgentContext, *roles: str) -> str:
    context.workspace.runtime.assert_current(context.session_id, context.epoch)
    role = context.persistent_role
    if role is None or (roles and role not in roles):
        allowed = " or ".join(roles) if roles else "persistent company role"
        raise AuthorizationError(f"this operation requires {allowed} authority")
    return role


def require_capability(context: AgentContext, capability: str) -> str:
    context.workspace.runtime.assert_current(context.session_id, context.epoch)
    role = context.persistent_role
    if role is None or capability not in ROLE_CAPABILITIES.get(role, frozenset()):
        raise AuthorizationError(f"{context.logical_id} lacks capability {capability}")
    return role


def require_owner(context: AgentContext, project_id: str | None = None) -> str:
    context.workspace.runtime.assert_current(context.session_id, context.epoch)
    if (
        context.authority_kind != AuthorityKind.OWNER
        or context.identity_kind not in {None, IdentityKind.OWNER}
        or not context.logical_id.endswith(":owner")
    ):
        raise AuthorizationError("this operation requires canonical Owner authority")
    owned = context.logical_id.split(":", 1)[0]
    if project_id and project_id != owned:
        raise AuthorizationError(f"{context.logical_id} cannot operate {project_id}")
    return owned


def validate_message_route(
    context: AgentContext, recipient: str, *, project_id: str | None = None
) -> str:
    context.workspace.runtime.assert_current(context.session_id, context.epoch)
    normalized = recipient.strip()
    if normalized.upper() in {"CPO", "CEO", "CTO", "BOARD", "ALL"}:
        normalized = normalized.upper()
    elif normalized.lower().endswith(":owner"):
        normalized = f"{normalized.rsplit(':', 1)[0].upper()}:owner"
    if context.authority_kind in {AuthorityKind.SECRETARY, AuthorityKind.CANDIDATE}:
        parent = context.logical_id.split(":", 1)[0]
        if normalized != parent:
            raise PermissionError(f"{context.authority_kind} may message only canonical {parent}")
        return normalized
    if context.authority_kind == AuthorityKind.OWNER:
        if normalized not in {"CEO", "CTO"}:
            raise AuthorizationError(
                f"message route {context.logical_id} -> {normalized} is not authorized"
            )
        if not project_id:
            raise AuthorizationError("Owner messages require their matching project scope")
        owned = require_owner(context, project_id)
        if project_id != owned:
            raise AuthorizationError(f"{context.logical_id} cannot operate {project_id}")
        return normalized
    role = require_role(context, "CPO", "CEO", "CTO")
    allowed_roles = {
        "CPO": {"BOARD", "CEO", "CTO"},
        "CEO": {"BOARD", "CPO", "CTO"},
        "CTO": {"BOARD", "CPO", "CEO"},
    }
    if normalized.endswith(":owner") and role in {"CEO", "CTO"}:
        recipient_project = normalized.split(":", 1)[0]
        if not project_id or recipient_project != project_id:
            raise AuthorizationError("Owner messages require the matching project scope")
        context.workspace.find_project(project_id)
        return normalized
    if normalized not in allowed_roles[role]:
        raise AuthorizationError(f"message route {role} -> {normalized} is not authorized")
    return normalized


def public_context(context: AgentContext) -> dict[str, Any]:
    capabilities = sorted(ROLE_CAPABILITIES.get(context.persistent_role or "", frozenset()))
    return {
        "logical_id": context.logical_id,
        "authority_kind": context.authority_kind,
        "identity_kind": context.identity_kind,
        "epoch": context.epoch,
        "provider_thread_id": context.provider_thread_id,
        "capabilities": capabilities,
    }
