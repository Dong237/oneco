"""Signal-to-product services backed by append-only Git records."""

from __future__ import annotations

import fcntl
import json
import re
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Callable, ParamSpec, TypeVar

from .errors import AuthorizationError, ConflictError, ProjectError
from .models import (
    BetRecord,
    BetRevisionManifest,
    BetRevisionState,
    BoardDecision,
    BoardDecisionKind,
    EvidenceReference,
    ProjectState,
    PromotionRecord,
    ReleaseRecord,
    ReleaseState,
    ReviewRequest,
    ReviewVerdict,
    SignalRecord,
    SignalStatus,
    SourceReference,
    SupervisionPolicy,
    TechnicalReview,
    ValidationState,
)
from .records import (
    commit_exact,
    committed_file_matches_head,
    composite_digest,
    file_digest,
    load_committed_json,
    load_committed_text,
    operation_lock,
    sha256_bytes,
)
from .util import atomic_json, atomic_text, load_json, safe_child, slugify
from .workspace import Workspace

P = ParamSpec("P")
R = TypeVar("R")


@contextmanager
def _bet_mutation_lock(workspace: Workspace, bet_id: str):
    """Serialize all state-changing operations for one Bet across processes."""
    if not re.fullmatch(r"BET-[0-9]{4,}", bet_id):
        raise ConflictError(f"invalid Bet identifier: {bet_id}")
    path = workspace.root / ".oneco" / "locks" / f"bet-{bet_id}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _serialize_bet_mutation(
    function: Callable[P, R],
) -> Callable[P, R]:
    @wraps(function)
    def wrapped(workspace: Workspace, bet_id: str, *args: Any, **kwargs: Any) -> R:
        with _bet_mutation_lock(workspace, bet_id):
            return function(workspace, bet_id, *args, **kwargs)

    return wrapped  # type: ignore[return-value]


def _next_id(directory: Path, prefix: str) -> str:
    """Atomically reserve the next durable object directory across processes."""
    directory.mkdir(parents=True, exist_ok=True)
    while True:
        highest = 0
        for path in directory.iterdir():
            if not path.is_dir() or not path.name.startswith(prefix + "-"):
                continue
            try:
                highest = max(highest, int(path.name.split("-", 1)[1]))
            except ValueError:
                continue
        identifier = f"{prefix}-{highest + 1:04d}"
        try:
            (directory / identifier).mkdir()
        except FileExistsError:
            continue
        return identifier


def _serialize_release_mutation(function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def wrapped(workspace: Workspace, release_id: str, *args: Any, **kwargs: Any) -> R:
        with operation_lock(workspace.root, f"release:{release_id}"):
            return function(workspace, release_id, *args, **kwargs)

    return wrapped  # type: ignore[return-value]


def _json_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return sha256_bytes(encoded)


def _git_blob(repo: Path, commit: str, relative: str) -> bytes:
    safe_child(repo, relative)
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:{relative}"],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ConflictError(f"acceptance evidence is not present in product commit: {relative}")
    return result.stdout


def _record_index(
    workspace: Workspace,
    record_id: str,
    record_type: str,
    object_id: str,
    path: Path,
    state: str,
) -> None:
    workspace.runtime.index_record(
        record_id, record_type, object_id, path, file_digest(path), state
    )


def _validated_evidence(
    workspace: Workspace, evidence: list[dict[str, Any]] | None
) -> list[EvidenceReference]:
    references: list[EvidenceReference] = []
    for item in evidence or []:
        reference = EvidenceReference.model_validate(item)
        if reference.path:
            evidence_path = safe_child(workspace.root, reference.path)
            if not evidence_path.is_file():
                raise ConflictError(f"Bet evidence does not exist: {reference.path}")
            digest = file_digest(evidence_path)
            if reference.sha256 and reference.sha256 != digest:
                raise ConflictError(f"Bet evidence digest mismatch: {reference.evidence_id}")
            reference = reference.model_copy(update={"sha256": digest})
        references.append(reference)
    return references


def _bet_digest(
    bet_id: str,
    revision: int,
    packet_sha256: str,
    validation_state: ValidationState,
    evidence: list[EvidenceReference],
) -> str:
    return _json_digest(
        {
            "bet_id": bet_id,
            "revision": revision,
            "packet_sha256": packet_sha256,
            "validation_state": validation_state,
            "evidence": [item.model_dump(mode="json") for item in evidence],
        }
    )


def _assert_bet_manifest_current(
    workspace: Workspace,
    manifest: BetRevisionManifest,
    *,
    require_committed: bool = True,
) -> None:
    manifest_path = (
        workspace.root
        / "bets"
        / manifest.bet_id
        / "revisions"
        / f"R{manifest.revision:03d}"
        / "packet.manifest.json"
    )
    if require_committed and not committed_file_matches_head(workspace.root, manifest_path):
        raise ConflictError("submitted Bet manifest is not committed durable truth")
    packet_path = safe_child(workspace.root, manifest.packet_path)
    if not packet_path.is_file() or file_digest(packet_path) != manifest.packet_sha256:
        raise ConflictError("submitted Bet packet changed after its digest was recorded")
    for reference in manifest.evidence:
        if reference.path:
            evidence_path = safe_child(workspace.root, reference.path)
            if (
                not evidence_path.is_file()
                or not reference.sha256
                or file_digest(evidence_path) != reference.sha256
            ):
                raise ConflictError(
                    f"submitted Bet evidence changed after its digest was recorded: {reference.evidence_id}"
                )
    current_digest = _bet_digest(
        manifest.bet_id,
        manifest.revision,
        manifest.packet_sha256,
        manifest.validation_state,
        manifest.evidence,
    )
    if current_digest != manifest.composite_digest:
        raise ConflictError("submitted Bet digest is stale")


def _validate_packet_sections(packet: str) -> None:
    headings = [
        re.sub(r"[^a-z0-9]+", " ", match.group(1).lower()).strip()
        for match in re.finditer(r"(?m)^#{2,6}\s+(.+?)\s*$", packet)
    ]
    required = {
        "user and job": ("user", "job"),
        "alternatives": ("alternatives",),
        "source provenance and dates": ("source", "provenance", "date"),
        "counterevidence and unknowns": ("counterevidence", "unknown"),
        "product opportunity": ("product", "opportunity"),
        "business-system hypothesis": ("business", "system", "hypothesis"),
        "maximum unknown": ("maximum", "unknown"),
        "validation result": ("validation", "result"),
        "recommendation and confidence boundary": (
            "recommendation",
            "confidence",
            "boundary",
        ),
    }
    structural_labels = [
        re.sub(r"[^a-z0-9]+", " ", match.group(1).lower()).strip()
        for match in re.finditer(r"(?mi)^\s*(?:[-*]\s+)?([a-z][^:\n]{1,80}):\s*", packet)
    ]
    missing = []
    for name, terms in required.items():
        labels = headings if name != "maximum unknown" else [*headings, *structural_labels]
        if not any(all(term in label for term in terms) for label in labels):
            missing.append(name)
    if missing:
        raise ConflictError("Product Bet packet is missing sections: " + ", ".join(missing))


def list_signals(workspace: Workspace) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((workspace.root / "signals").glob("SIG-*/signal.json")):
        durable = load_committed_json(workspace.root, path)
        if durable is None:
            continue
        record = SignalRecord.model_validate(durable)
        rows.append(record.model_dump(mode="json"))
    return rows


def list_bets(workspace: Workspace) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((workspace.root / "bets").glob("BET-*/bet.json")):
        durable_bet = load_committed_json(workspace.root, path)
        if durable_bet is None:
            continue
        bet = BetRecord.model_validate(durable_bet)
        root = path.parent
        manifest_path = (
            root / "revisions" / f"R{bet.current_revision:03d}" / ("packet.manifest.json")
        )
        manifest = (
            BetRevisionManifest.model_validate(durable_manifest).model_dump(mode="json")
            if (durable_manifest := load_committed_json(workspace.root, manifest_path)) is not None
            else None
        )
        recorded_decision = latest_bet_decision(workspace, bet.bet_id)
        decision = (
            recorded_decision
            if manifest
            and recorded_decision
            and recorded_decision.revision == manifest["revision"]
            and recorded_decision.packet_digest == manifest["composite_digest"]
            else None
        )
        rows.append(
            {
                **bet.model_dump(mode="json"),
                "current_manifest": manifest,
                "latest_decision": decision.model_dump(mode="json") if decision else None,
                "latest_recorded_decision": (
                    recorded_decision.model_dump(mode="json") if recorded_decision else None
                ),
            }
        )
    return rows


def record_signal(
    workspace: Workspace,
    title: str,
    summary: str,
    sources: list[dict[str, Any]],
    *,
    assigned_by: str = "BOARD",
) -> SignalRecord:
    signal_id = _next_id(workspace.root / "signals", "SIG")
    source_models: list[SourceReference] = []
    for index, source in enumerate(sources, start=1):
        source_id = source.get("source_id") or f"SRC-{index:03d}"
        payload = {**source, "source_id": source_id}
        raw_uri = str(payload.get("uri") or "").strip()
        local_source = Path(raw_uri).expanduser() if raw_uri else None
        if local_source and local_source.is_file():
            captured = (
                workspace.root
                / "signals"
                / signal_id
                / "sources"
                / f"{source_id}{local_source.suffix}"
            )
            captured.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_source, captured)
            payload.update(
                {
                    "uri": local_source.resolve().as_uri(),
                    "captured_path": captured.relative_to(workspace.root).as_posix(),
                    "portable": True,
                }
            )
        elif (
            not raw_uri and not payload.get("captured_path") and payload.get("kind") == "board_idea"
        ):
            payload["uri"] = f"board://{signal_id}/{source_id}"
        candidate = SourceReference.model_validate(
            {
                **payload,
                "observed_at": source.get("observed_at") or datetime.now(UTC),
            }
        )
        if candidate.captured_path:
            captured = safe_child(workspace.root, candidate.captured_path)
            if not captured.is_file():
                raise ConflictError(
                    f"captured Signal source does not exist: {candidate.captured_path}"
                )
            digest = file_digest(captured)
            if candidate.sha256 and candidate.sha256 != digest:
                raise ConflictError(
                    f"captured Signal source digest mismatch: {candidate.source_id}"
                )
            candidate = candidate.model_copy(update={"sha256": digest})
        source_models.append(candidate)
    if not source_models:
        raise ConflictError("a Signal requires at least one traceable source")
    record = SignalRecord(
        signal_id=signal_id,
        title=title,
        summary=summary,
        status=SignalStatus.ASSIGNED,
        assigned_by=assigned_by.upper(),
        source_ids=[item.source_id for item in source_models],
    )
    root = workspace.root / "signals" / signal_id
    markdown = (
        f"# {title}\n\n"
        f"Signal ID: `{signal_id}`  \n"
        f"Status: `{record.status}`  \n"
        f"Assigned by: `{record.assigned_by}`\n\n"
        f"## Observation\n\n{summary}\n"
    )
    signal_path = root / "SIGNAL.md"
    record_path = root / "signal.json"
    sources_path = root / "sources.manifest.json"
    atomic_text(signal_path, markdown)
    atomic_json(record_path, record.model_dump(mode="json"))
    atomic_json(sources_path, [item.model_dump(mode="json") for item in source_models])
    captured_paths = [
        safe_child(workspace.root, item.captured_path)
        for item in source_models
        if item.captured_path
    ]
    commit_exact(
        workspace.root,
        [signal_path, record_path, sources_path, *captured_paths],
        f"oneco(board): assign {signal_id}",
    )
    _record_index(workspace, signal_id, "signal", signal_id, record_path, record.status)
    message_id = workspace.runtime.send_message(
        "BOARD",
        "CPO",
        "signal_assignment",
        f"Investigate {signal_id}: {title}\n{summary}",
        requires_ack=True,
        idempotency_key=f"signal:{signal_id}:cpo",
        subject=f"Assigned signal {signal_id}",
        thread_key=signal_id,
        object_id=signal_id,
        artifact_digest=composite_digest(root, [signal_path, sources_path]),
    )
    workspace.runtime.enqueue_role_work(
        "CPO",
        "signal_assignment",
        {"signal_id": signal_id, "message_id": message_id},
        object_id=signal_id,
        priority=20,
        idempotency_key=f"signal:{signal_id}:work",
    )
    workspace.runtime.event("signal.assigned", "BOARD", record.model_dump(mode="json"))
    return record


def create_bet_draft(
    workspace: Workspace,
    title: str,
    signal_ids: list[str],
    packet: str,
    *,
    variant_of: str | None = None,
    duplicate_of: str | None = None,
) -> BetRecord:
    if variant_of and duplicate_of:
        raise ConflictError("a Bet cannot be both a variant and a duplicate")
    for related in (variant_of, duplicate_of):
        if related and not (workspace.root / "bets" / related / "bet.json").is_file():
            raise ConflictError(f"unknown related Bet: {related}")
    if not signal_ids:
        raise ConflictError("a Product Bet requires at least one source Signal")
    signal_records: list[tuple[Path, Path, SignalRecord]] = []
    for signal_id in dict.fromkeys(signal_ids):
        if not re.fullmatch(r"SIG-[0-9]{3,}", signal_id):
            raise ConflictError(f"invalid Signal ID: {signal_id}")
        signal_root = safe_child(workspace.root / "signals", signal_id)
        signal_markdown = signal_root / "SIGNAL.md"
        signal_path = signal_root / "signal.json"
        durable_signal = load_committed_json(workspace.root, signal_path)
        durable_markdown = load_committed_text(workspace.root, signal_markdown)
        if durable_signal is None or durable_markdown is None:
            raise ConflictError(f"unknown Signal: {signal_id}")
        signal = SignalRecord.model_validate(durable_signal)
        if signal.signal_id != signal_id:
            raise ConflictError(f"Signal record identity mismatch: {signal_id}")
        signal_records.append(
            (
                signal_path,
                signal_markdown,
                signal,
            )
        )
    bet_id = _next_id(workspace.root / "bets", "BET")
    record = BetRecord(
        bet_id=bet_id,
        title=title,
        signal_ids=list(dict.fromkeys(signal_ids)),
        variant_of=variant_of,
        duplicate_of=duplicate_of,
        current_revision=1,
    )
    root = workspace.root / "bets" / bet_id
    record_path = root / "bet.json"
    packet_path = root / "revisions" / "R001" / "PRODUCT_BET_PACKET.md"
    atomic_json(record_path, record.model_dump(mode="json"))
    atomic_text(packet_path, packet)
    commit_paths = [record_path, packet_path]
    linked_records: list[tuple[SignalRecord, Path]] = []
    for signal_path, signal_markdown, signal in signal_records:
        linked = signal.model_copy(
            update={
                "status": SignalStatus.LINKED,
                "linked_bet_ids": list(dict.fromkeys([*signal.linked_bet_ids, bet_id])),
            }
        )
        atomic_json(signal_path, linked.model_dump(mode="json"))
        markdown = load_committed_text(workspace.root, signal_markdown)
        if markdown is None:
            raise ConflictError(f"Signal is not committed durable truth: {linked.signal_id}")
        markdown = re.sub(r"(?m)^Status: `[^`]+`\s*$", f"Status: `{linked.status}`  ", markdown)
        atomic_text(signal_markdown, markdown)
        commit_paths.extend((signal_path, signal_markdown))
        linked_records.append((linked, signal_path))
    commit_exact(workspace.root, commit_paths, f"oneco(cpo): draft {bet_id}")
    for linked, signal_path in linked_records:
        _record_index(
            workspace, linked.signal_id, "signal", linked.signal_id, signal_path, linked.status
        )
        workspace.runtime.resolve_messages(
            "CPO", kind="signal_assignment", object_id=linked.signal_id
        )
    workspace.runtime.event("bet.draft_created", "CPO", {"bet_id": bet_id, "revision": 1})
    return record


@_serialize_bet_mutation
def revise_bet_draft(workspace: Workspace, bet_id: str, packet: str) -> BetRecord:
    root = workspace.root / "bets" / bet_id
    record_path = root / "bet.json"
    durable_record = load_committed_json(workspace.root, record_path)
    if durable_record is None:
        raise ConflictError(f"Bet is not committed durable truth: {bet_id}")
    record = BetRecord.model_validate(durable_record)
    latest = latest_bet_decision(workspace, bet_id)
    current_manifest = (
        root / "revisions" / f"R{record.current_revision:03d}" / ("packet.manifest.json")
    )
    if load_committed_json(workspace.root, current_manifest) is None:
        raise ConflictError(
            "a Board decision authorizes exactly one new revision; submit the current draft"
        )
    manifest = BetRevisionManifest.model_validate(load_committed_json(workspace.root, current_manifest))
    if latest is None:
        raise ConflictError(
            "submitted Bet revisions are immutable until a Board more-evidence or reopen decision"
        )
    if latest and latest.kind not in {BoardDecisionKind.MORE_EVIDENCE, BoardDecisionKind.REOPEN}:
        raise ConflictError("only a Board more-evidence or reopen decision permits a new revision")
    if (
        latest.revision != record.current_revision
        or latest.packet_digest != manifest.composite_digest
    ):
        raise ConflictError("the Board decision does not authorize the current Bet revision")
    revision = record.current_revision + 1
    path = root / "revisions" / f"R{revision:03d}" / "PRODUCT_BET_PACKET.md"
    if path.exists() and path.read_text(encoding="utf-8") != packet:
        raise ConflictError(f"Bet revision R{revision:03d} already exists with different content")
    updated = record.model_copy(update={"current_revision": revision})
    atomic_json(record_path, updated.model_dump(mode="json"))
    if not path.exists():
        atomic_text(path, packet)
    commit_exact(workspace.root, [record_path, path], f"oneco(cpo): draft {bet_id} R{revision:03d}")
    workspace.runtime.event("bet.revision_created", "CPO", {"bet_id": bet_id, "revision": revision})
    return updated


@_serialize_bet_mutation
def submit_bet(
    workspace: Workspace,
    bet_id: str,
    *,
    validation_state: str = "not_run",
    evidence: list[dict[str, Any]] | None = None,
    submitted_by: str = "CPO",
) -> BetRevisionManifest:
    root = workspace.root / "bets" / bet_id
    durable_bet = load_committed_json(workspace.root, root / "bet.json")
    if durable_bet is None:
        raise ConflictError(f"Bet is not committed durable truth: {bet_id}")
    bet = BetRecord.model_validate(durable_bet)
    revision_root = root / "revisions" / f"R{bet.current_revision:03d}"
    packet_path = revision_root / "PRODUCT_BET_PACKET.md"
    manifest_path = revision_root / "packet.manifest.json"
    packet = packet_path.read_text(encoding="utf-8")
    _validate_packet_sections(packet)
    evidence_models = _validated_evidence(workspace, evidence)
    packet_sha = file_digest(packet_path)
    validated_state = ValidationState(validation_state)
    digest = _bet_digest(bet_id, bet.current_revision, packet_sha, validated_state, evidence_models)
    if manifest_path.exists():
        existing = BetRevisionManifest.model_validate(load_json(manifest_path))
        _assert_bet_manifest_current(workspace, existing, require_committed=False)
        if (
            existing.validation_state == validated_state
            and existing.evidence == evidence_models
            and existing.submitted_by == submitted_by
            and existing.packet_sha256 == packet_sha
            and existing.composite_digest == digest
        ):
            manifest = existing
        else:
            raise ConflictError("submitted Bet retry has different validation, evidence, or author")
    else:
        manifest = BetRevisionManifest(
            bet_id=bet_id,
            revision=bet.current_revision,
            state=BetRevisionState.READY_FOR_BOARD,
            validation_state=validated_state,
            packet_path=packet_path.relative_to(workspace.root).as_posix(),
            packet_sha256=packet_sha,
            evidence=evidence_models,
            composite_digest=digest,
            supersedes_revision=bet.current_revision - 1 if bet.current_revision > 1 else None,
            submitted_by=submitted_by,
        )
        atomic_json(manifest_path, manifest.model_dump(mode="json"))
    commit_paths = [root / "bet.json", packet_path, manifest_path]
    for item in evidence_models:
        if item.path:
            candidate = safe_child(workspace.root, item.path)
            if candidate.is_file():
                commit_paths.append(candidate)
    if not committed_file_matches_head(workspace.root, manifest_path):
        commit_exact(
            workspace.root,
            commit_paths,
            f"oneco(cpo): submit {bet_id} R{bet.current_revision:03d}",
        )
    _record_index(
        workspace,
        f"{bet_id}:R{bet.current_revision:03d}",
        "bet_revision",
        bet_id,
        manifest_path,
        manifest.state,
    )
    workspace.runtime.mark_object_messages_stale(bet_id, digest)
    workspace.runtime.send_message(
        "CPO",
        "BOARD",
        "bet_ready",
        f"{bet_id} R{bet.current_revision:03d} is ready for Board decision.",
        requires_ack=True,
        idempotency_key=f"bet:{bet_id}:R{bet.current_revision:03d}:ready",
        subject=f"Product Bet ready: {bet.title}",
        thread_key=bet_id,
        object_id=bet_id,
        artifact_digest=digest,
    )
    workspace.runtime.resolve_messages("CPO", kind="bet_decision", object_id=bet_id)
    return manifest


def _latest_bet_decision_entry(
    workspace: Workspace, bet_id: str, *, committed_only: bool
) -> tuple[Path, BoardDecision] | None:
    directory = workspace.root / "bets" / bet_id / "decisions"
    paths = sorted(directory.glob("*.json")) if directory.exists() else []
    for path in reversed(paths):
        if committed_only and not committed_file_matches_head(workspace.root, path):
            continue
        return path, BoardDecision.model_validate(load_json(path))
    return None


def latest_bet_decision(workspace: Workspace, bet_id: str) -> BoardDecision | None:
    entry = _latest_bet_decision_entry(workspace, bet_id, committed_only=True)
    return entry[1] if entry else None


@_serialize_bet_mutation
def board_decide_bet(
    workspace: Workspace,
    bet_id: str,
    revision: int,
    packet_digest: str,
    decision: str,
    reason: str,
    *,
    evidence_gap: str | None = None,
    provider_thread_id: str | None = None,
    provider_turn_id: str | None = None,
) -> BoardDecision:
    manifest_path = (
        workspace.root / "bets" / bet_id / "revisions" / f"R{revision:03d}" / "packet.manifest.json"
    )
    manifest = BetRevisionManifest.model_validate(load_json(manifest_path))
    durable_bet = load_committed_json(workspace.root, workspace.root / "bets" / bet_id / "bet.json")
    if durable_bet is None:
        raise ConflictError(f"Bet is not committed durable truth: {bet_id}")
    bet = BetRecord.model_validate(durable_bet)
    if revision != bet.current_revision:
        raise ConflictError("Board decision must target the current submitted Bet revision")
    if manifest.composite_digest != packet_digest:
        raise ConflictError("Board decision digest does not match the submitted Bet revision")
    _assert_bet_manifest_current(workspace, manifest)
    kind = BoardDecisionKind(decision)
    if kind not in {
        BoardDecisionKind.SELECT,
        BoardDecisionKind.REJECT,
        BoardDecisionKind.MORE_EVIDENCE,
        BoardDecisionKind.REOPEN,
    }:
        raise ConflictError(f"unsupported Bet decision: {decision}")
    previous_entry = _latest_bet_decision_entry(workspace, bet_id, committed_only=True)
    previous = previous_entry[1] if previous_entry else None
    pending_entry = _latest_bet_decision_entry(workspace, bet_id, committed_only=False)
    pending_path = pending_entry[0] if pending_entry else None
    pending = pending_entry[1] if pending_entry else None
    if pending_path and committed_file_matches_head(workspace.root, pending_path):
        pending_path = None
        pending = None
    candidate = pending or previous
    if (
        candidate
        and provider_turn_id
        and (
            candidate.provider_thread_id,
            candidate.provider_turn_id,
        )
        == (provider_thread_id, provider_turn_id)
    ):
        if (
            candidate.kind == kind
            and candidate.revision == revision
            and candidate.packet_digest == packet_digest
            and candidate.reason == reason
            and candidate.evidence_gap == evidence_gap
        ):
            record = candidate
            path = pending_path or previous_entry[0]
        else:
            raise ConflictError("this Board turn already recorded a different Bet decision")
    elif (
        candidate
        and candidate.kind == kind
        and candidate.revision == revision
        and candidate.packet_digest == packet_digest
        and candidate.reason == reason
        and candidate.evidence_gap == evidence_gap
    ):
        record = candidate
        path = pending_path or previous_entry[0]
    elif pending:
        raise ConflictError("an interrupted Board decision must be retried with the same payload")
    else:
        record = BoardDecision(
            decision_id=f"dec_{uuid.uuid4().hex}",
            kind=kind,
            bet_id=bet_id,
            revision=revision,
            packet_digest=packet_digest,
            reason=reason,
            evidence_gap=evidence_gap,
            provider_thread_id=provider_thread_id,
            provider_turn_id=provider_turn_id,
            supersedes=previous.decision_id if previous else None,
        )
        path = (
            workspace.root
            / "bets"
            / bet_id
            / "decisions"
            / (f"{record.created_at.strftime('%Y%m%dT%H%M%S%fZ')}-{record.decision_id}.json")
        )
        atomic_json(path, record.model_dump(mode="json"))
    if not committed_file_matches_head(workspace.root, path):
        commit_exact(workspace.root, [path], f"oneco(board): {kind} {bet_id} R{revision:03d}")
    _record_index(workspace, record.decision_id, "board_bet_decision", bet_id, path, kind)
    recipient = "CEO" if kind == BoardDecisionKind.SELECT else "CPO"
    workspace.runtime.resolve_messages("BOARD", kind="bet_ready", object_id=bet_id)
    message_id = workspace.runtime.send_message(
        "BOARD",
        recipient,
        "bet_decision",
        f"{kind}: {bet_id} R{revision:03d}\nReason: {reason}",
        requires_ack=True,
        idempotency_key=f"{record.decision_id}:{recipient}",
        subject=f"Board decision: {kind} {bet_id}",
        thread_key=bet_id,
        object_id=bet_id,
        artifact_digest=packet_digest,
    )
    if recipient in {"CEO", "CPO"}:
        workspace.runtime.enqueue_role_work(
            recipient,
            "bet_decision",
            {
                "decision_id": record.decision_id,
                "bet_id": bet_id,
                "message_id": message_id,
            },
            object_id=bet_id,
            priority=10,
            idempotency_key=f"{record.decision_id}:{recipient}:work",
        )
    return record


@_serialize_bet_mutation
def promote_selected_bet(
    workspace: Workspace,
    bet_id: str,
    *,
    name: str,
    slug: str | None = None,
) -> PromotionRecord:
    decision = latest_bet_decision(workspace, bet_id)
    if decision is None or decision.kind != BoardDecisionKind.SELECT:
        raise AuthorizationError("CEO may promote only the current Board-selected Bet revision")
    existing_project = next(
        (project for project in workspace.portfolio().projects if project.source_bet_id == bet_id),
        None,
    )
    durable_bet = load_committed_json(workspace.root, workspace.root / "bets" / bet_id / "bet.json")
    if durable_bet is None:
        raise ConflictError(f"Bet is not committed durable truth: {bet_id}")
    bet = BetRecord.model_validate(durable_bet)
    if decision.revision != bet.current_revision:
        raise ConflictError("selected Bet is no longer current; obtain a new Board selection")
    assert decision.revision is not None and decision.packet_digest is not None
    revision_root = workspace.root / "bets" / bet_id / "revisions" / f"R{decision.revision:03d}"
    manifest = BetRevisionManifest.model_validate(load_json(revision_root / "packet.manifest.json"))
    if manifest.composite_digest != decision.packet_digest:
        raise ConflictError("selected Bet digest is stale; obtain a new Board selection")
    _assert_bet_manifest_current(workspace, manifest)
    if existing_project:
        if (
            existing_project.source_bet_revision != decision.revision
            or existing_project.source_bet_digest != decision.packet_digest
            or not existing_project.promotion_id
        ):
            raise ConflictError(f"{bet_id} has an incompatible partial promotion")
        project = existing_project
        promotion_id = existing_project.promotion_id
    else:
        promotion_id = f"prom_{uuid.uuid4().hex}"
        project = workspace.register_project(
            name,
            Path(slug or slugify(name)),
            initialize_git=True,
            source_bet_id=bet_id,
            source_bet_revision=decision.revision,
            source_bet_digest=decision.packet_digest,
            promotion_id=promotion_id,
        )
    target = workspace.project_path(project.id)
    handoff = target / "handoffs" / "product-bet"
    control_path = workspace.root / "portfolio" / "decisions" / f"{promotion_id}.json"
    promotion_path = handoff / "promotion.json"
    if existing_project and committed_file_matches_head(workspace.root, control_path):
        existing_promotion = PromotionRecord.model_validate(load_json(control_path))
        expected = {
            "promotion_id": promotion_id,
            "bet_id": bet_id,
            "revision": decision.revision,
            "packet_digest": decision.packet_digest,
            "board_decision_id": decision.decision_id,
            "project_id": project.id,
        }
        actual = existing_promotion.model_dump(
            include=set(expected),
            mode="json",
        )
        if actual != expected:
            raise ConflictError(f"{bet_id} has an incompatible durable promotion")
        if (
            not committed_file_matches_head(target, promotion_path)
            or PromotionRecord.model_validate(load_json(promotion_path)) != existing_promotion
        ):
            raise ConflictError(f"{bet_id} has inconsistent promotion handoff provenance")
        _record_index(
            workspace,
            existing_promotion.promotion_id,
            "promotion",
            project.id,
            control_path,
            "promoted",
        )
        message_id = workspace.runtime.send_message(
            "CEO",
            "CTO",
            "technical_shaping_request",
            (
                f"Shape the minimum sound technical path for {project.id} "
                f"from {bet_id} R{decision.revision:03d}."
            ),
            project_id=project.id,
            requires_ack=True,
            idempotency_key=f"{promotion_id}:cto",
            subject=f"Technical shaping: {project.id}",
            thread_key=project.id,
            object_id=project.id,
            artifact_digest=decision.packet_digest,
        )
        workspace.runtime.enqueue_role_work(
            "CTO",
            "technical_shaping_request",
            {
                "promotion_id": promotion_id,
                "project_id": project.id,
                "message_id": message_id,
            },
            object_id=project.id,
            priority=10,
            idempotency_key=f"{promotion_id}:cto:work",
        )
        workspace.runtime.resolve_messages("CEO", kind="bet_decision", object_id=bet_id)
        return existing_promotion
    handoff.mkdir(parents=True, exist_ok=True)
    shutil.copy2(revision_root / "PRODUCT_BET_PACKET.md", handoff / "PRODUCT_BET_PACKET.md")
    shutil.copy2(revision_root / "packet.manifest.json", handoff / "packet.manifest.json")
    decision_path = next(
        path
        for path in (workspace.root / "bets" / bet_id / "decisions").glob("*.json")
        if load_json(path).get("decision_id") == decision.decision_id
    )
    shutil.copy2(decision_path, handoff / "board-decision.json")
    promotion = PromotionRecord(
        promotion_id=promotion_id,
        bet_id=bet_id,
        revision=decision.revision,
        packet_digest=decision.packet_digest,
        board_decision_id=decision.decision_id,
        project_id=project.id,
    )
    atomic_json(promotion_path, promotion.model_dump(mode="json"))
    project_paths = [
        path for path in target.rglob("*") if path.is_file() and ".git" not in path.parts
    ]
    product_commit = commit_exact(target, project_paths, f"oneco(ceo): promote {bet_id}")
    promotion = promotion.model_copy(update={"project_commit": product_commit})
    atomic_json(promotion_path, promotion.model_dump(mode="json"))
    if product_commit:
        commit_exact(target, [promotion_path], f"oneco(ceo): bind promotion {promotion_id}")
    atomic_json(control_path, promotion.model_dump(mode="json"))
    commit_exact(
        workspace.root,
        [workspace.portfolio_path, control_path, workspace.root / ".gitignore"],
        f"oneco(ceo): promote {bet_id} to {project.id}",
    )
    _record_index(workspace, promotion_id, "promotion", project.id, control_path, "promoted")
    message_id = workspace.runtime.send_message(
        "CEO",
        "CTO",
        "technical_shaping_request",
        f"Shape the minimum sound technical path for {project.id} from {bet_id} R{decision.revision:03d}.",
        project_id=project.id,
        requires_ack=True,
        idempotency_key=f"{promotion_id}:cto",
        subject=f"Technical shaping: {project.id}",
        thread_key=project.id,
        object_id=project.id,
        artifact_digest=decision.packet_digest,
    )
    workspace.runtime.enqueue_role_work(
        "CTO",
        "technical_shaping_request",
        {
            "promotion_id": promotion_id,
            "project_id": project.id,
            "message_id": message_id,
        },
        object_id=project.id,
        priority=10,
        idempotency_key=f"{promotion_id}:cto:work",
    )
    workspace.runtime.resolve_messages("CEO", kind="bet_decision", object_id=bet_id)
    return promotion


def supervision_policy(workspace: Workspace) -> SupervisionPolicy:
    return SupervisionPolicy.model_validate(
        load_committed_json(workspace.root, workspace.root / "operations" / "supervision-policy.json")
        or load_json(workspace.root / "operations" / "supervision-policy.json")
    )


def _assert_checkpoint_exists(
    workspace: Workspace, project_id: str, checkpoint_id: str | None
) -> None:
    workspace.find_project(project_id)
    if checkpoint_id is None:
        return
    target = workspace.project_path(project_id)
    for path in (target / "checkpoints").glob("*.json"):
        if (
            committed_file_matches_head(target, path)
            and load_json(path).get("checkpoint_id") == checkpoint_id
        ):
            return
    raise ProjectError(f"unknown checkpoint for {project_id}: {checkpoint_id}")


def configure_supervision(
    workspace: Workspace, interval_minutes: int, *, changed_by: str, reason: str
) -> SupervisionPolicy:
    policy = SupervisionPolicy(
        interval_minutes=interval_minutes,
        changed_by=changed_by.upper(),
        reason=reason,
    )
    path = workspace.root / "operations" / "supervision-policy.json"
    atomic_json(path, policy.model_dump(mode="json"))
    commit_exact(workspace.root, [path], "oneco(board): update CTO supervision interval")
    return policy


def request_gate_review(
    workspace: Workspace,
    project_id: str,
    *,
    requested_by: str,
    reason: str,
    evidence: str,
    checkpoint_id: str | None = None,
    review_id: str | None = None,
) -> ReviewRequest:
    _assert_checkpoint_exists(workspace, project_id, checkpoint_id)
    if requested_by != f"{project_id}:owner":
        raise AuthorizationError(f"only {project_id}:owner can request this review")
    identifier = review_id or f"review_{uuid.uuid4().hex}"
    if not re.fullmatch(r"review_[A-Za-z0-9_-]+", identifier):
        raise ConflictError("review_id must be a safe review_ identifier")
    review = ReviewRequest(
        review_id=identifier,
        project_id=project_id,
        checkpoint_id=checkpoint_id,
        reason=reason,
        evidence=evidence,
        requested_by=requested_by,
    )
    path = workspace.root / "operations" / project_id / "reviews" / f"{review.review_id}.json"
    if path.exists():
        existing = ReviewRequest.model_validate(load_json(path))
        comparable = ("project_id", "checkpoint_id", "reason", "evidence", "requested_by")
        if all(getattr(existing, key) == getattr(review, key) for key in comparable):
            review = existing
        else:
            raise ConflictError(f"review request retry changed payload: {review.review_id}")
    else:
        atomic_json(path, review.model_dump(mode="json"))
    if not committed_file_matches_head(workspace.root, path):
        commit_exact(workspace.root, [path], f"oneco(owner): request review {project_id}")
    _record_index(workspace, review.review_id, "review_request", project_id, path, "open")
    message_id = workspace.runtime.send_message(
        requested_by,
        "CTO",
        "review_request",
        f"{reason}\nEvidence: {evidence}",
        project_id=project_id,
        requires_ack=True,
        idempotency_key=f"{review.review_id}:cto",
        subject=f"Gate review: {project_id}",
        thread_key=project_id,
        object_id=review.review_id,
    )
    workspace.runtime.enqueue_role_work(
        "CTO",
        "gate_review",
        {"review_id": review.review_id, "project_id": project_id, "message_id": message_id},
        object_id=review.review_id,
        priority=5,
        idempotency_key=f"{review.review_id}:cto:work",
    )
    return review


def record_technical_review(
    workspace: Workspace,
    project_id: str,
    *,
    verdict: str,
    evidence: str,
    required_action: str,
    affected_scope: str,
    checkpoint_id: str | None = None,
    review_id: str | None = None,
) -> TechnicalReview:
    _assert_checkpoint_exists(workspace, project_id, checkpoint_id)
    if review_id and not re.fullmatch(r"review_[A-Za-z0-9_-]+", review_id):
        raise ConflictError("review_id must be a safe review_ identifier")
    policy = supervision_policy(workspace)
    created = datetime.now(UTC)
    review = TechnicalReview(
        review_id=review_id or f"review_{uuid.uuid4().hex}",
        project_id=project_id,
        checkpoint_id=checkpoint_id,
        verdict=ReviewVerdict(verdict),
        evidence=evidence,
        required_action=required_action,
        affected_scope=affected_scope,
        next_review_at=created + timedelta(minutes=policy.interval_minutes),
        created_at=created,
    )
    review_root = workspace.root / "operations" / project_id / "reviews"
    existing_paths = sorted(review_root.glob(f"*-{review.review_id}-cto.json"))
    if existing_paths:
        existing = TechnicalReview.model_validate(load_json(existing_paths[-1]))
        comparable = (
            "project_id",
            "checkpoint_id",
            "verdict",
            "evidence",
            "required_action",
            "affected_scope",
        )
        if all(getattr(existing, key) == getattr(review, key) for key in comparable):
            review = existing
            created = existing.created_at
            path = existing_paths[-1]
        else:
            raise ConflictError(f"technical review retry changed payload: {review.review_id}")
    else:
        path = review_root / (
            f"{created.strftime('%Y%m%dT%H%M%S%fZ')}-{review.review_id}-cto.json"
        )
        atomic_json(path, review.model_dump(mode="json"))
    if not committed_file_matches_head(workspace.root, path):
        commit_exact(workspace.root, [path], f"oneco(cto): review {project_id}")
    _record_index(workspace, review.review_id, "technical_review", project_id, path, verdict)
    workspace.runtime.set_supervision_due(
        project_id,
        last_review_at=created.isoformat(),
        next_review_at=review.next_review_at.isoformat(),
        verdict=verdict,
    )
    workspace.runtime.resolve_messages("CTO", kind="review_request", object_id=review.review_id)
    # A newer technical finding supersedes older discussion inputs for this project.
    workspace.runtime.resolve_messages("CEO", kind="technical_review", project_id=project_id)
    owner = f"{project_id}:owner"
    workspace.runtime.resolve_messages(owner, kind="technical_correction", project_id=project_id)
    message_id = workspace.runtime.send_message(
        "CTO",
        "CEO",
        "technical_review",
        f"{project_id}: {verdict}\n{evidence}\nRecommended: {required_action}",
        project_id=project_id,
        requires_ack=review.verdict != ReviewVerdict.ON_TRACK,
        idempotency_key=f"{review.review_id}:ceo",
        subject=f"CTO review: {project_id}",
        thread_key=project_id,
        object_id=review.review_id,
    )
    if review.verdict != ReviewVerdict.ON_TRACK:
        workspace.runtime.enqueue_role_work(
            "CEO",
            "review_discussion",
            {"review_id": review.review_id, "project_id": project_id, "message_id": message_id},
            object_id=review.review_id,
            priority=10,
            idempotency_key=f"{review.review_id}:ceo:work",
        )
    if review.verdict in {ReviewVerdict.NEEDS_CORRECTION, ReviewVerdict.BLOCKED}:
        owner_message = workspace.runtime.send_message(
            "CTO",
            owner,
            "technical_correction",
            (
                f"{project_id}: {review.verdict}\nAffected scope: {affected_scope}\n"
                f"Evidence: {evidence}\nRequired: {required_action}\n"
                "Continue compatible work outside the affected scope."
            ),
            project_id=project_id,
            requires_ack=True,
            idempotency_key=f"{review.review_id}:owner",
            subject=f"Technical correction: {project_id}",
            thread_key=project_id,
            object_id=review.review_id,
            artifact_digest=file_digest(path),
        )
        workspace.runtime.enqueue_work(
            owner,
            project_id,
            message_id=owner_message,
            priority=5 if review.verdict == ReviewVerdict.BLOCKED else 20,
        )
    elif review.verdict == ReviewVerdict.ON_TRACK:
        owner_message = workspace.runtime.send_message(
            "CTO",
            owner,
            "technical_clearance",
            (
                f"{project_id}: technical review passed for checkpoint "
                f"{checkpoint_id or 'current evidence'}. Prepare the next evidence-bound step."
            ),
            project_id=project_id,
            requires_ack=True,
            idempotency_key=f"{review.review_id}:owner-clearance",
            subject=f"Technical clearance: {project_id}",
            thread_key=project_id,
            object_id=review.review_id,
            artifact_digest=file_digest(path),
        )
        workspace.runtime.enqueue_work(
            owner,
            project_id,
            message_id=owner_message,
            priority=20,
        )
    return review


def record_release_candidate(
    workspace: Workspace,
    project_id: str,
    *,
    product_commit: str,
    acceptance_evidence: list[str],
) -> ReleaseRecord:
    project = workspace.find_project(project_id)
    if project.state not in {ProjectState.ACTIVE, ProjectState.BLOCKED_EXTERNAL}:
        raise ProjectError("release candidates require an active project")
    target = workspace.project_path(project_id)
    commit_check = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--verify", f"{product_commit}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if commit_check.returncode:
        raise ConflictError(f"unknown product commit: {product_commit}")
    evidence_hashes: dict[str, str] = {}
    for relative in acceptance_evidence:
        evidence_path = safe_child(target, relative)
        if not evidence_path.is_file():
            raise ConflictError(f"acceptance evidence does not exist: {relative}")
        normalized = evidence_path.relative_to(target).as_posix()
        blob_digest = sha256_bytes(_git_blob(target, commit_check.stdout.strip(), normalized))
        if file_digest(evidence_path) != blob_digest:
            raise ConflictError(f"acceptance evidence does not match product commit: {normalized}")
        evidence_hashes[normalized] = blob_digest
    digest = _json_digest(
        {
            "project_id": project_id,
            "product_commit": commit_check.stdout.strip(),
            "acceptance_evidence": evidence_hashes,
        }
    )
    releases_root = workspace.root / "operations" / project_id / "releases"
    record: ReleaseRecord | None = None
    path: Path | None = None
    for existing_path in sorted(releases_root.glob("rel_*/candidate.json")):
        existing = ReleaseRecord.model_validate(load_json(existing_path))
        if existing.release_digest == digest:
            if (
                existing.product_commit != commit_check.stdout.strip()
                or existing.acceptance_evidence != acceptance_evidence
            ):
                raise ConflictError("release digest collides with a different candidate payload")
            record = existing
            path = existing_path
            break
    if record is None:
        release_id = f"rel_{uuid.uuid4().hex}"
        record = ReleaseRecord(
            release_id=release_id,
            project_id=project_id,
            product_commit=commit_check.stdout.strip(),
            release_digest=digest,
            acceptance_evidence=list(evidence_hashes),
            acceptance_evidence_hashes=evidence_hashes,
        )
        path = releases_root / release_id / "candidate.json"
        atomic_json(path, record.model_dump(mode="json"))
    assert path is not None
    if not committed_file_matches_head(workspace.root, path):
        commit_exact(workspace.root, [path], f"oneco(owner): release candidate {record.release_id}")
    _record_index(workspace, record.release_id, "release", project_id, path, record.state)
    with workspace.runtime.connect() as connection:
        indexed = connection.execute(
            "SELECT state, release_digest FROM release_index WHERE release_id=?",
            (record.release_id,),
        ).fetchone()
    if indexed and indexed["release_digest"] != record.release_digest:
        raise ConflictError("release candidate conflicts with its runtime index")
    advanced = {
        ReleaseState.READY,
        ReleaseState.QUEUED,
        ReleaseState.REJECTED,
        ReleaseState.RELEASED,
        ReleaseState.FAILED,
    }
    if indexed is None or indexed["state"] not in advanced:
        workspace.runtime.index_release(
            record.release_id, project_id, record.release_digest, record.state, path
        )
        for recipient in ("CEO", "CTO"):
            message_id = workspace.runtime.send_message(
                f"{project_id}:owner",
                recipient,
                "release_candidate",
                f"Review release {record.release_id} for commit {record.product_commit}.",
                project_id=project_id,
                requires_ack=True,
                idempotency_key=f"{record.release_id}:{recipient}",
                subject=f"Release candidate: {project_id}",
                thread_key=project_id,
                object_id=record.release_id,
                artifact_digest=record.release_digest,
            )
            workspace.runtime.enqueue_role_work(
                recipient,
                "release_candidate",
                {
                    "release_id": record.release_id,
                    "project_id": project_id,
                    "message_id": message_id,
                },
                object_id=record.release_id,
                priority=10,
                idempotency_key=f"{record.release_id}:{recipient}:work",
            )
    workspace.runtime.resolve_messages(
        f"{project_id}:owner", kind="technical_clearance", project_id=project_id
    )
    return record


def validate_release_candidate(workspace: Workspace, candidate: ReleaseRecord) -> str:
    project_root = workspace.project_path(candidate.project_id)
    commit_check = subprocess.run(
        [
            "git",
            "-C",
            str(project_root),
            "rev-parse",
            "--verify",
            f"{candidate.product_commit}^{{commit}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if commit_check.returncode:
        raise ConflictError(f"release product commit is unavailable: {candidate.product_commit}")
    evidence_hashes: dict[str, str] = {}
    if not candidate.acceptance_evidence_hashes:
        raise ConflictError("release candidate lacks commit-bound acceptance evidence")
    for relative in candidate.acceptance_evidence:
        evidence_path = safe_child(project_root, relative)
        if not evidence_path.is_file():
            raise ConflictError(f"release acceptance evidence is missing: {relative}")
        normalized = evidence_path.relative_to(project_root).as_posix()
        expected = candidate.acceptance_evidence_hashes.get(normalized)
        if not expected:
            raise ConflictError(f"release acceptance evidence manifest is incomplete: {normalized}")
        committed = sha256_bytes(_git_blob(project_root, candidate.product_commit, normalized))
        if committed != expected or file_digest(evidence_path) != expected:
            raise ConflictError("release acceptance evidence changed; candidate digest is stale")
        evidence_hashes[normalized] = expected
    if set(candidate.acceptance_evidence_hashes) != set(evidence_hashes):
        raise ConflictError("release acceptance evidence manifest has unexpected entries")
    current = _json_digest(
        {
            "project_id": candidate.project_id,
            "product_commit": commit_check.stdout.strip(),
            "acceptance_evidence": evidence_hashes,
        }
    )
    if current != candidate.release_digest:
        raise ConflictError("release acceptance evidence changed; candidate digest is stale")
    return current


@_serialize_release_mutation
def endorse_release(workspace: Workspace, release_id: str, role: str, note: str) -> Path:
    role = role.upper()
    if role not in {"CEO", "CTO"}:
        raise AuthorizationError("only CEO or CTO can endorse a release candidate")
    with workspace.runtime.connect() as connection:
        row = connection.execute(
            "SELECT * FROM release_index WHERE release_id=?", (release_id,)
        ).fetchone()
    if row is None:
        raise ConflictError(f"unknown release: {release_id}")
    release_root = workspace.root / "operations" / row["project_id"] / "releases" / release_id
    candidate_path = release_root / "candidate.json"
    if not committed_file_matches_head(workspace.root, candidate_path):
        raise ConflictError("release candidate is not committed at the current company HEAD")
    candidate = ReleaseRecord.model_validate(load_json(candidate_path))
    if candidate.release_digest != row["release_digest"]:
        raise ConflictError("release candidate digest no longer matches its runtime index")
    validate_release_candidate(workspace, candidate)
    path = release_root / (f"{role.lower()}-endorsement.json")
    if path.exists():
        existing = load_json(path)
        expected = {
            "release_id": release_id,
            "project_id": row["project_id"],
            "release_digest": row["release_digest"],
            "role": role,
        }
        if any(existing.get(key) != value for key, value in expected.items()):
            raise ConflictError(f"{role} release endorsement is stale or incompatible")
    else:
        data = {
            "release_id": release_id,
            "project_id": row["project_id"],
            "release_digest": row["release_digest"],
            "role": role,
            "note": note,
            "created_at": datetime.now(UTC).isoformat(),
        }
        atomic_json(path, data)
    if not committed_file_matches_head(workspace.root, path):
        commit_exact(workspace.root, [path], f"oneco({role.lower()}): endorse {release_id}")
    endorsements = [
        release_root / "ceo-endorsement.json",
        release_root / "cto-endorsement.json",
    ]
    if row["state"] in {ReleaseState.CANDIDATE, ReleaseState.READY} and all(
        item.is_file()
        and committed_file_matches_head(workspace.root, item)
        and load_json(item).get("release_digest") == row["release_digest"]
        for item in endorsements
    ):
        workspace.runtime.index_release(
            release_id,
            row["project_id"],
            row["release_digest"],
            ReleaseState.READY,
            candidate_path,
        )
        workspace.runtime.send_message(
            "CEO",
            "BOARD",
            "release_ready",
            f"Release {release_id} is ready for Board decision.",
            project_id=row["project_id"],
            requires_ack=True,
            idempotency_key=f"{release_id}:board",
            subject=f"Production decision: {row['project_id']}",
            thread_key=row["project_id"],
            object_id=release_id,
            artifact_digest=row["release_digest"],
        )
    workspace.runtime.resolve_messages(role, kind="release_candidate", object_id=release_id)
    return path


@_serialize_release_mutation
def board_decide_release(
    workspace: Workspace,
    release_id: str,
    decision: str,
    reason: str,
    *,
    provider_thread_id: str | None = None,
    provider_turn_id: str | None = None,
) -> BoardDecision:
    with workspace.runtime.connect() as connection:
        row = connection.execute(
            "SELECT * FROM release_index WHERE release_id=?", (release_id,)
        ).fetchone()
    if row is None:
        raise ConflictError(f"unknown release: {release_id}")
    release_root = workspace.root / "operations" / row["project_id"] / "releases" / release_id
    candidate_path = release_root / "candidate.json"
    if not committed_file_matches_head(workspace.root, candidate_path):
        raise ConflictError("release candidate is not committed at the current company HEAD")
    candidate = ReleaseRecord.model_validate(load_json(candidate_path))
    if candidate.release_digest != row["release_digest"]:
        raise ConflictError("release candidate digest no longer matches its runtime index")
    validate_release_candidate(workspace, candidate)
    endorsement_paths = [
        release_root / "ceo-endorsement.json",
        release_root / "cto-endorsement.json",
    ]
    for endorsement_role, path in zip(("CEO", "CTO"), endorsement_paths, strict=True):
        if not path.is_file() or not committed_file_matches_head(workspace.root, path):
            raise ConflictError(
                "production release requires committed CEO and CTO readiness endorsements"
            )
        endorsement = load_json(path)
        expected = {
            "release_id": release_id,
            "project_id": row["project_id"],
            "release_digest": row["release_digest"],
            "role": endorsement_role,
        }
        if any(endorsement.get(key) != value for key, value in expected.items()):
            raise ConflictError("release endorsement is stale or incompatible")
    if decision not in {"approve", "reject"}:
        raise ConflictError("release decision must be approve or reject")
    kind = (
        BoardDecisionKind.APPROVE_RELEASE
        if decision == "approve"
        else BoardDecisionKind.REJECT_RELEASE
    )
    record = BoardDecision(
        decision_id=f"dec_{uuid.uuid4().hex}",
        kind=kind,
        project_id=row["project_id"],
        release_id=release_id,
        release_digest=row["release_digest"],
        reason=reason,
        provider_thread_id=provider_thread_id,
        provider_turn_id=provider_turn_id,
    )
    path = release_root / "board-decision.json"
    if path.exists():
        existing = BoardDecision.model_validate(load_json(path))
        if not (
            existing.kind == record.kind
            and existing.project_id == record.project_id
            and existing.release_id == record.release_id
            and existing.release_digest == record.release_digest
            and existing.reason == record.reason
        ):
            raise ConflictError("this release already has a different Board decision")
        record = existing
    else:
        atomic_json(path, record.model_dump(mode="json"))
    if not committed_file_matches_head(workspace.root, path):
        commit_exact(workspace.root, [path], f"oneco(board): {kind} {release_id}")
    state = (
        ReleaseState.QUEUED if kind == BoardDecisionKind.APPROVE_RELEASE else ReleaseState.REJECTED
    )
    if row["state"] not in {ReleaseState.RELEASED, ReleaseState.FAILED}:
        workspace.runtime.index_release(
            release_id, row["project_id"], row["release_digest"], state, path
        )
    workspace.runtime.resolve_messages("BOARD", kind="release_ready", object_id=release_id)
    if state == ReleaseState.QUEUED:
        message_id = workspace.runtime.send_message(
            "BOARD",
            f"{row['project_id']}:owner",
            "production_release",
            f"Release approved for exact digest {row['release_digest']}. Execute once and record evidence.",
            project_id=row["project_id"],
            requires_ack=True,
            idempotency_key=f"{release_id}:owner",
            subject=f"Production approved: {row['project_id']}",
            thread_key=row["project_id"],
            object_id=release_id,
            artifact_digest=row["release_digest"],
        )
        workspace.runtime.enqueue_work(
            f"{row['project_id']}:owner",
            row["project_id"],
            message_id=message_id,
            priority=1,
        )
    return record


@_serialize_release_mutation
def record_release_result(
    workspace: Workspace,
    release_id: str,
    *,
    actor: str,
    succeeded: bool,
    evidence: list[str],
    note: str,
    session_id: str | None = None,
    epoch: int | None = None,
) -> Path:
    with workspace.runtime.connect() as connection:
        row = connection.execute(
            "SELECT * FROM release_index WHERE release_id=?", (release_id,)
        ).fetchone()
    if row is None:
        raise ConflictError(f"unknown release: {release_id}")
    project_id = str(row["project_id"])
    if actor != f"{project_id}:owner":
        raise AuthorizationError(f"only {project_id}:owner may record this release result")
    if (session_id is None) != (epoch is None):
        raise AuthorizationError("session_id and epoch must be provided together")
    if session_id is not None and epoch is not None:
        session = workspace.runtime.assert_current(session_id, epoch)
        if session["logical_id"] != actor:
            raise AuthorizationError(f"session {session_id} does not own identity {actor}")
    release_root = workspace.root / "operations" / project_id / "releases" / release_id
    candidate_path = release_root / "candidate.json"
    if not committed_file_matches_head(workspace.root, candidate_path):
        raise ConflictError("release candidate is not committed at the current company HEAD")
    candidate = ReleaseRecord.model_validate(load_json(candidate_path))
    if candidate.release_digest != row["release_digest"]:
        raise ConflictError("release candidate digest no longer matches its runtime index")
    validate_release_candidate(workspace, candidate)
    decision_path = release_root / "board-decision.json"
    if not committed_file_matches_head(workspace.root, decision_path):
        raise ConflictError("release result requires an exact Board-approved queued release")
    decision = BoardDecision.model_validate(load_json(decision_path))
    if (
        decision.kind != BoardDecisionKind.APPROVE_RELEASE
        or decision.release_digest != candidate.release_digest
    ):
        raise ConflictError("release result requires the exact current Board approval")
    if not evidence:
        raise ConflictError("release result requires at least one evidence file")
    project_root = workspace.project_path(project_id)
    evidence_rows: list[dict[str, str]] = []
    for relative in evidence:
        evidence_path = safe_child(project_root, relative)
        if not evidence_path.is_file():
            raise ConflictError(f"release evidence does not exist: {relative}")
        evidence_rows.append(
            {
                "path": evidence_path.relative_to(project_root).as_posix(),
                "sha256": file_digest(evidence_path),
            }
        )
    path = release_root / "result.json"
    if path.exists():
        existing = load_json(path)
        if (
            existing.get("release_id") == release_id
            and existing.get("project_id") == project_id
            and existing.get("release_digest") == row["release_digest"]
            and existing.get("recorded_by") == actor
            and existing.get("succeeded") == succeeded
            and existing.get("evidence") == evidence_rows
            and existing.get("note") == note
        ):
            data = existing
        else:
            raise ConflictError("this release already has a different execution result")
    else:
        if row["state"] != ReleaseState.QUEUED:
            raise ConflictError("release result requires an exact Board-approved queued release")
        data = {
            "release_id": release_id,
            "project_id": project_id,
            "release_digest": row["release_digest"],
            "succeeded": succeeded,
            "evidence": evidence_rows,
            "note": note,
            "recorded_by": actor,
            "recorded_session_id": session_id,
            "recorded_epoch": epoch,
            "created_at": datetime.now(UTC).isoformat(),
        }
        atomic_json(path, data)
    if not committed_file_matches_head(workspace.root, path):
        commit_exact(workspace.root, [path], f"oneco(owner): record release result {release_id}")
    state = ReleaseState.RELEASED if succeeded else ReleaseState.FAILED
    workspace.runtime.index_release(
        release_id, project_id, str(row["release_digest"]), state, release_root / "candidate.json"
    )
    workspace.runtime.resolve_messages(actor, kind="production_release", object_id=release_id)
    for recipient in ("BOARD", "CEO", "CTO"):
        workspace.runtime.send_message(
            actor,
            recipient,
            "release_result",
            f"{release_id}: {'released' if succeeded else 'failed'}\n{note}",
            project_id=project_id,
            requires_ack=False,
            idempotency_key=f"{release_id}:result:{recipient}",
            subject=f"Release result: {project_id}",
            thread_key=project_id,
            object_id=release_id,
            artifact_digest=str(row["release_digest"]),
        )
    return path
