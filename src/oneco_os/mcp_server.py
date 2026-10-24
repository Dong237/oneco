"""OneCo's stdio MCP surface for authorized conversational operations."""

from __future__ import annotations

import os
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from . import agent_tools
from .authority import resolve_agent_context
from .models import ActionDomain, EvidenceReference, InstructionAction, ReviewVerdict

mcp = FastMCP(
    "oneco",
    instructions=(
        "Operate the current local OneCo company. Organizational authority is derived from the "
        "active OneCo session and cannot be supplied or escalated by tool arguments."
    ),
)


def _context():
    return resolve_agent_context(dict(os.environ))


@mcp.tool()
def oneco_company_snapshot() -> dict[str, Any]:
    """Return bounded company, project, inbox, queue, and capability status."""
    return agent_tools.company_snapshot(_context())


@mcp.tool()
def oneco_project_get(project_id: str) -> dict[str, Any]:
    """Read a project's current launch packet and readiness."""
    return agent_tools.project_get(_context(), project_id)


@mcp.tool()
def oneco_inbox_list(unread_only: bool = True) -> list[dict[str, Any]]:
    """Read messages addressed to this exact logical identity."""
    return agent_tools.inbox_list(_context(), unread_only)


@mcp.tool()
def oneco_message_send(
    recipient: str,
    kind: str,
    body: str,
    project_id: str | None = None,
    requires_ack: bool = False,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Send a durable OneCo message within this identity's authority."""
    return agent_tools.message_send(
        _context(), recipient, kind, body, project_id, requires_ack, idempotency_key
    )


@mcp.tool()
def oneco_message_ack(message_id: str) -> dict[str, str]:
    """Acknowledge a message addressed to this identity."""
    return agent_tools.message_ack(_context(), message_id)


@mcp.tool()
def oneco_project_create(
    name: str, slug: str | None = None, initialize_git: bool = True
) -> dict[str, Any]:
    """Reject direct creation; OneCo v1 projects come from Board-selected Bets."""
    return agent_tools.project_create(_context(), name, slug, initialize_git=initialize_git)


@mcp.tool()
def oneco_bet_create(
    title: str,
    signal_ids: list[str],
    packet: str,
    variant_of: str | None = None,
    duplicate_of: str | None = None,
) -> dict[str, Any]:
    """Create a CPO-owned Product Bet draft from Board-assigned Signals."""
    return agent_tools.bet_create(
        _context(),
        title,
        signal_ids,
        packet,
        variant_of=variant_of,
        duplicate_of=duplicate_of,
    )


@mcp.tool()
def oneco_bet_revise(bet_id: str, packet: str) -> dict[str, Any]:
    """Create the next Bet revision after a Board evidence-gap or reopen decision."""
    return agent_tools.bet_revise(_context(), bet_id, packet)


@mcp.tool()
def oneco_bet_submit(
    bet_id: str,
    validation_state: Literal[
        "not_run", "tbd", "passed", "mixed", "failed", "invalid"
    ] = "not_run",
    evidence: list[EvidenceReference] | None = None,
) -> dict[str, Any]:
    """Freeze and submit the current Bet revision for a human Board decision."""
    return agent_tools.bet_submit(_context(), bet_id, validation_state, evidence)


@mcp.tool()
def oneco_project_promote(
    bet_id: str, name: str, slug: str | None = None
) -> dict[str, Any]:
    """Promote the exact current Board-selected Bet; CEO only."""
    return agent_tools.project_promote(_context(), bet_id, name, slug)


@mcp.tool()
def oneco_project_endorse(project_id: str, note: str) -> dict[str, Any]:
    """Endorse the exact current launch-packet digest as canonical CEO or CTO."""
    return agent_tools.project_endorse(_context(), project_id, note)


@mcp.tool()
def oneco_project_launch(project_id: str) -> dict[str, Any]:
    """Launch an Owner after both endorsements or a provenance-checked Board override."""
    return agent_tools.project_launch(_context(), project_id)


@mcp.tool()
def oneco_owner_instruct(
    project_id: str,
    action: InstructionAction,
    spec_id: str,
    task_id: str,
    observed: str,
    evidence: str,
    required_action: str,
    do_not: str,
    acceptance_delta: str = "none",
    requires_spec_revision: bool = False,
) -> dict[str, Any]:
    """Queue a bounded CEO or CTO instruction for the Owner's next atomic boundary."""
    return agent_tools.owner_instruct(
        _context(),
        project_id,
        action,
        spec_id,
        task_id,
        observed,
        evidence,
        required_action,
        do_not,
        acceptance_delta,
        requires_spec_revision,
    )


@mcp.tool()
def oneco_checkpoint_record(
    project_id: str,
    spec_id: str,
    task_id: str,
    result: str,
    verification: list[str],
    next_task: str,
    key_files: list[str] | None = None,
    risk_or_blocker: str = "none",
    source_instruction_id: str | None = None,
) -> dict[str, Any]:
    """Record an evidence-backed checkpoint for this Owner's own project."""
    return agent_tools.checkpoint_record(
        _context(),
        project_id,
        spec_id,
        task_id,
        result,
        verification,
        next_task,
        key_files,
        risk_or_blocker,
        source_instruction_id,
    )


@mcp.tool()
def oneco_gate_review_request(
    project_id: str,
    reason: str,
    evidence: str,
    checkpoint_id: str | None = None,
    review_id: str | None = None,
) -> dict[str, Any]:
    """Stop affected work at a real gate and request CTO review; this Owner only."""
    return agent_tools.gate_review_request(
        _context(), project_id, reason, evidence, checkpoint_id, review_id
    )


@mcp.tool()
def oneco_technical_review_record(
    project_id: str,
    verdict: ReviewVerdict,
    evidence: str,
    required_action: str,
    affected_scope: str,
    checkpoint_id: str | None = None,
    review_id: str | None = None,
) -> dict[str, Any]:
    """Record an evidence-backed CTO review and route the product discussion to CEO."""
    return agent_tools.technical_review_record(
        _context(),
        project_id,
        verdict,
        evidence,
        required_action,
        affected_scope,
        checkpoint_id,
        review_id,
    )


@mcp.tool()
def oneco_supervision_configure(interval_minutes: int, reason: str) -> dict[str, Any]:
    """Change the CTO review interval as CTO; the durable policy records provenance."""
    return agent_tools.supervision_configure(_context(), interval_minutes, reason)


@mcp.tool()
def oneco_release_candidate_record(
    project_id: str, product_commit: str, acceptance_evidence: list[str]
) -> dict[str, Any]:
    """Record a verified release candidate for this Owner's project."""
    return agent_tools.release_candidate_record(
        _context(), project_id, product_commit, acceptance_evidence
    )


@mcp.tool()
def oneco_release_endorse(release_id: str, note: str) -> dict[str, Any]:
    """Endorse an exact release digest from the CEO or CTO lens."""
    return agent_tools.release_endorse(_context(), release_id, note)


@mcp.tool()
def oneco_release_result_record(
    release_id: str,
    project_id: str,
    succeeded: bool,
    evidence: list[str],
    note: str,
) -> dict[str, Any]:
    """Record production execution evidence after exact Board approval; this Owner only."""
    return agent_tools.release_result_record(
        _context(), release_id, project_id, succeeded, evidence, note
    )


@mcp.tool()
def oneco_blocker_record(
    project_id: str, summary: str, evidence: str, requested_decision: str
) -> dict[str, Any]:
    """Escalate a real Owner blocker to CEO and CTO."""
    return agent_tools.blocker_record(
        _context(), project_id, summary, evidence, requested_decision
    )


@mcp.tool()
def oneco_blocker_resolve(
    project_id: str, blocker_id: str, checkpoint_id: str, evidence: str
) -> dict[str, Any]:
    """Resolve this Owner's blocker using a later evidence-backed checkpoint."""
    return agent_tools.blocker_resolve(
        _context(), project_id, blocker_id, checkpoint_id, evidence
    )


@mcp.tool()
def oneco_action_request(
    project_id: str,
    domain: ActionDomain,
    summary: str,
    effect: str,
    reversible: bool,
) -> dict[str, Any]:
    """Request approval for an external product, technical, or cross-domain action."""
    return agent_tools.action_request(
        _context(), project_id, domain, summary, effect, reversible
    )


@mcp.tool()
def oneco_action_decide(
    action_id: str,
    decision: Literal["approve", "reject"],
    note: str = "",
) -> dict[str, Any]:
    """Approve or reject an external action within canonical executive authority."""
    return agent_tools.action_decide(_context(), action_id, decision, note)


@mcp.tool()
def oneco_executive_branch(
    purpose: str, recovery_candidate: bool = False
) -> dict[str, Any]:
    """Fork this persistent role's exact Trae context into an advisory or recovery window."""
    return agent_tools.executive_branch(
        _context(), purpose, recovery_candidate=recovery_candidate
    )


@mcp.tool()
def oneco_executive_focus(role: Literal["CPO", "CEO", "CTO"]) -> dict[str, Any]:
    """Focus or reopen the persistent CPO, CEO, or CTO Ghostty window."""
    return agent_tools.executive_focus(_context(), role)


@mcp.tool()
def oneco_succession_request(role: Literal["CPO", "CEO", "CTO"]) -> dict[str, Any]:
    """Request a one-time human confirmation phrase from a recovery candidate."""
    return agent_tools.succession_request(_context(), role)


@mcp.tool()
def oneco_owner_watch(project_id: str) -> dict[str, str]:
    """Open a read-only Ghostty view of one Owner's tmux worker."""
    return agent_tools.owner_watch(_context(), project_id)


def run() -> None:
    mcp.run(transport="stdio")
