"""Runtime-derived organizational authority for conversational tools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AuthorizationError, WorkspaceError
from .models import AuthorityKind
from .workspace import Workspace


@dataclass(frozen=True)
class AgentContext:
    workspace: Workspace
    session_id: str
    logical_id: str
    epoch: int
    authority_kind: str
    provider_thread_id: str | None

    @property
    def canonical_role(self) -> str | None:
        if self.authority_kind == AuthorityKind.CANONICAL and self.logical_id in {"CEO", "CTO"}:
            return self.logical_id
        return None


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
    )


def require_canonical(context: AgentContext, *roles: str) -> str:
    role = context.canonical_role
    if role is None or (roles and role not in roles):
        allowed = " or ".join(roles) if roles else "canonical executive"
        raise AuthorizationError(f"this operation requires {allowed} authority")
    return role


def require_owner(context: AgentContext, project_id: str | None = None) -> str:
    if context.authority_kind != AuthorityKind.OWNER or not context.logical_id.endswith(":owner"):
        raise AuthorizationError("this operation requires canonical Owner authority")
    owned = context.logical_id.split(":", 1)[0]
    if project_id and project_id != owned:
        raise AuthorizationError(f"{context.logical_id} cannot operate {project_id}")
    return owned


def public_context(context: AgentContext) -> dict[str, Any]:
    return {
        "logical_id": context.logical_id,
        "authority_kind": context.authority_kind,
        "epoch": context.epoch,
        "provider_thread_id": context.provider_thread_id,
    }
