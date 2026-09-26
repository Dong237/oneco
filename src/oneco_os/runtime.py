"""Deterministic local coordination backed by SQLite WAL."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import AuthorizationError, ConflictError, SessionError, StaleSessionError
from .models import ActionDomain, AuthorityKind, SessionLifecycle, WorkState

SCHEMA_VERSION = 2


def now_text() -> str:
    return datetime.now(UTC).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS identities (
  logical_id TEXT PRIMARY KEY, active_session_id TEXT, epoch INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, logical_id TEXT NOT NULL, epoch INTEGER NOT NULL,
  provider TEXT, provider_session_id TEXT, cwd TEXT NOT NULL, tmux_target TEXT, pid INTEGER,
  lifecycle TEXT NOT NULL, work_state TEXT NOT NULL, last_seen_at TEXT NOT NULL, ended_at TEXT,
  FOREIGN KEY(logical_id) REFERENCES identities(logical_id)
);
CREATE INDEX IF NOT EXISTS sessions_logical_idx ON sessions(logical_id, epoch);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, sender TEXT NOT NULL, recipient TEXT NOT NULL,
  project_id TEXT, kind TEXT NOT NULL, body TEXT NOT NULL, requires_ack INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, acked_at TEXT, acked_by TEXT
);
CREATE INDEX IF NOT EXISTS messages_recipient_idx ON messages(recipient, acked_at, created_at);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, actor TEXT NOT NULL,
  project_id TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS writer_leases (
  project_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, epoch INTEGER NOT NULL, acquired_at TEXT NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS checkpoint_index (
  checkpoint_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, session_id TEXT, epoch INTEGER,
  file_path TEXT NOT NULL UNIQUE, spec_id TEXT NOT NULL, task_id TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_requests (
  action_id TEXT PRIMARY KEY, requester TEXT NOT NULL, project_id TEXT NOT NULL, domain TEXT NOT NULL,
  summary TEXT NOT NULL, effect TEXT NOT NULL, reversible INTEGER NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS action_approvals (
  action_id TEXT NOT NULL, approver TEXT NOT NULL, decision TEXT NOT NULL, note TEXT NOT NULL,
  created_at TEXT NOT NULL, PRIMARY KEY(action_id, approver),
  FOREIGN KEY(action_id) REFERENCES action_requests(action_id)
);
CREATE TABLE IF NOT EXISTS work_queue (
  id TEXT PRIMARY KEY, message_id TEXT, recipient TEXT NOT NULL, project_id TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 100, status TEXT NOT NULL, sequence INTEGER NOT NULL,
  claim_token TEXT, claimed_at TEXT, completed_at TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
  provider_turn_id TEXT, error TEXT, created_at TEXT NOT NULL,
  FOREIGN KEY(message_id) REFERENCES messages(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS work_queue_message_idx ON work_queue(message_id) WHERE message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS work_queue_recipient_idx ON work_queue(recipient, status, priority, sequence);
CREATE TABLE IF NOT EXISTS launch_packets (
  project_id TEXT PRIMARY KEY, revision_digest TEXT NOT NULL, ceo_endorsed_digest TEXT,
  cto_endorsed_digest TEXT, board_override_turn_id TEXT, launched_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS human_turns (
  provider_thread_id TEXT NOT NULL, provider_turn_id TEXT NOT NULL, prompt TEXT NOT NULL,
  prompt_digest TEXT NOT NULL, received_at TEXT NOT NULL, consumed_scope TEXT,
  PRIMARY KEY(provider_thread_id, provider_turn_id)
);
CREATE TABLE IF NOT EXISTS succession_requests (
  id TEXT PRIMARY KEY, role TEXT NOT NULL, old_session_id TEXT, candidate_session_id TEXT NOT NULL,
  nonce_hash TEXT NOT NULL, expires_at TEXT NOT NULL, confirmed_turn_id TEXT, status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capability_health (
  capability_name TEXT PRIMARY KEY, source TEXT NOT NULL, state TEXT NOT NULL,
  diagnostic TEXT NOT NULL, checked_at TEXT NOT NULL
);
"""

SESSION_V2_COLUMNS = {
    "authority_kind": "TEXT NOT NULL DEFAULT 'canonical'",
    "terminal_transport": "TEXT",
    "terminal_handle": "TEXT",
    "launch_nonce": "TEXT",
    "parent_session_id": "TEXT",
}


class Runtime:
    def __init__(self, database: Path):
        self.database = database

    def initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(sessions)")
            }
            for name, declaration in SESSION_V2_COLUMNS.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE sessions ADD COLUMN {name} {declaration}")
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        os.chmod(self.database, 0o600)

    def backup_to(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(self.database)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    @contextmanager
    def connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def event(
        self, event_type: str, actor: str, payload: dict[str, Any], project_id: str | None = None
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO events(event_type, actor, project_id, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (event_type, actor, project_id, json.dumps(payload), now_text()),
            )

    def register_session(
        self,
        logical_id: str,
        cwd: Path,
        *,
        provider: str = "generic",
        provider_session_id: str | None = None,
        tmux_target: str | None = None,
        pid: int | None = None,
        replace: bool = False,
        authority_kind: AuthorityKind | str | None = None,
        terminal_transport: str | None = None,
        terminal_handle: str | None = None,
        launch_nonce: str | None = None,
        parent_session_id: str | None = None,
    ) -> dict[str, Any]:
        session_id = f"ses_{uuid.uuid4().hex}"
        now = now_text()
        with self.connect(immediate=True) as connection:
            identity = connection.execute(
                "SELECT active_session_id, epoch FROM identities WHERE logical_id=?", (logical_id,)
            ).fetchone()
            if identity is None:
                epoch = 1
                connection.execute(
                    "INSERT INTO identities(logical_id, active_session_id, epoch) VALUES (?, ?, ?)",
                    (logical_id, session_id, epoch),
                )
            else:
                active = identity["active_session_id"]
                if active and not replace:
                    row = connection.execute(
                        "SELECT lifecycle FROM sessions WHERE id=?", (active,)
                    ).fetchone()
                    if row and row["lifecycle"] not in {"lost", "closed", "superseded"}:
                        raise ConflictError(
                            f"{logical_id} already has an active session; use session takeover"
                        )
                epoch = int(identity["epoch"]) + 1
                if active:
                    connection.execute(
                        "UPDATE sessions SET lifecycle=?, ended_at=? WHERE id=?",
                        (SessionLifecycle.SUPERSEDED, now, active),
                    )
                    connection.execute("DELETE FROM writer_leases WHERE session_id=?", (active,))
                connection.execute(
                    "UPDATE identities SET active_session_id=?, epoch=? WHERE logical_id=?",
                    (session_id, epoch, logical_id),
                )
            kind = str(authority_kind or self._infer_authority(logical_id))
            connection.execute(
                """INSERT INTO sessions(
                  id, logical_id, epoch, provider, provider_session_id, cwd, tmux_target, pid,
                  lifecycle, work_state, last_seen_at, authority_kind, terminal_transport,
                  terminal_handle, launch_nonce, parent_session_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    logical_id,
                    epoch,
                    provider,
                    provider_session_id,
                    str(cwd.resolve()),
                    tmux_target,
                    pid,
                    SessionLifecycle.LIVE,
                    WorkState.IDLE,
                    now,
                    kind,
                    terminal_transport or ("tmux" if tmux_target else None),
                    terminal_handle,
                    launch_nonce,
                    parent_session_id,
                ),
            )
        self.event("session.registered", logical_id, {"session_id": session_id, "epoch": epoch})
        return {"session_id": session_id, "logical_id": logical_id, "epoch": epoch}

    @staticmethod
    def _infer_authority(logical_id: str) -> AuthorityKind:
        if ":secretary:" in logical_id:
            return AuthorityKind.SECRETARY
        if ":candidate:" in logical_id:
            return AuthorityKind.CANDIDATE
        if logical_id.endswith(":owner"):
            return AuthorityKind.OWNER
        return AuthorityKind.CANONICAL

    def current_session(self, logical_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT s.* FROM identities i JOIN sessions s ON s.id=i.active_session_id
                WHERE i.logical_id=?""",
                (logical_id,),
            ).fetchone()
        return dict(row) if row else None

    def resume_session(
        self,
        session_id: str,
        epoch: int,
        *,
        terminal_transport: str | None = None,
        terminal_handle: str | None = None,
        pid: int | None = None,
        launch_nonce: str | None = None,
    ) -> dict[str, Any]:
        with self.connect(immediate=True) as connection:
            row = connection.execute(
                """SELECT s.*, i.active_session_id, i.epoch AS current_epoch
                FROM sessions s JOIN identities i ON i.logical_id=s.logical_id WHERE s.id=?""",
                (session_id,),
            ).fetchone()
            if row is None:
                raise SessionError(f"unknown session: {session_id}")
            if row["active_session_id"] != session_id or int(row["current_epoch"]) != epoch:
                raise StaleSessionError(f"stale session {session_id}")
            connection.execute(
                """UPDATE sessions SET lifecycle='live', work_state='idle', ended_at=NULL,
                last_seen_at=?, terminal_transport=COALESCE(?, terminal_transport),
                terminal_handle=COALESCE(?, terminal_handle), pid=COALESCE(?, pid),
                launch_nonce=COALESCE(?, launch_nonce) WHERE id=?""",
                (now_text(), terminal_transport, terminal_handle, pid, launch_nonce, session_id),
            )
        return {"session_id": session_id, "logical_id": row["logical_id"], "epoch": epoch}

    def update_terminal(
        self, session_id: str, epoch: int, *, transport: str, handle: str | None, pid: int | None = None
    ) -> None:
        self.assert_current(session_id, epoch)
        with self.connect() as connection:
            connection.execute(
                "UPDATE sessions SET terminal_transport=?, terminal_handle=?, pid=COALESCE(?, pid), last_seen_at=? WHERE id=?",
                (transport, handle, pid, now_text(), session_id),
            )

    def bind_provider_session(
        self, session_id: str, epoch: int, provider_session_id: str, *, cwd: str | None = None
    ) -> None:
        row = self.assert_current(session_id, epoch)
        existing = row["provider_session_id"]
        if existing and existing != provider_session_id:
            raise ConflictError(
                f"OneCo session {session_id} is already bound to provider thread {existing}"
            )
        with self.connect() as connection:
            connection.execute(
                "UPDATE sessions SET provider_session_id=?, cwd=COALESCE(?, cwd), last_seen_at=? WHERE id=?",
                (provider_session_id, cwd, now_text(), session_id),
            )

    def mark_lost(self, session_id: str, epoch: int) -> None:
        self.assert_current(session_id, epoch)
        with self.connect() as connection:
            connection.execute(
                "UPDATE sessions SET lifecycle='lost', ended_at=?, last_seen_at=? WHERE id=?",
                (now_text(), now_text(), session_id),
            )

    def assert_current(self, session_id: str, epoch: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT s.*, i.active_session_id, i.epoch AS current_epoch
                FROM sessions s JOIN identities i ON i.logical_id=s.logical_id WHERE s.id=?""",
                (session_id,),
            ).fetchone()
        if row is None:
            raise SessionError(f"unknown session: {session_id}")
        if row["active_session_id"] != session_id or int(row["current_epoch"]) != int(epoch):
            raise StaleSessionError(
                f"stale session {session_id}: epoch {epoch}, current epoch {row['current_epoch']}"
            )
        if row["lifecycle"] != SessionLifecycle.LIVE:
            raise StaleSessionError(f"session {session_id} is {row['lifecycle']}")
        return row

    def assert_active_session(self, session_id: str) -> sqlite3.Row:
        """Resolve authority from runtime state instead of trusting model-supplied role data."""
        with self.connect() as connection:
            row = connection.execute(
                """SELECT s.*, i.active_session_id, i.epoch AS current_epoch
                FROM sessions s JOIN identities i ON i.logical_id=s.logical_id WHERE s.id=?""",
                (session_id,),
            ).fetchone()
        if row is None:
            raise SessionError(f"unknown session: {session_id}")
        if (
            row["active_session_id"] != session_id
            or int(row["current_epoch"]) != int(row["epoch"])
            or row["lifecycle"] != SessionLifecycle.LIVE
        ):
            raise StaleSessionError(f"stale session {session_id}")
        return row

    def touch(self, session_id: str, epoch: int, work_state: WorkState | str | None = None) -> None:
        self.assert_current(session_id, epoch)
        with self.connect() as connection:
            if work_state:
                connection.execute(
                    "UPDATE sessions SET last_seen_at=?, work_state=? WHERE id=?",
                    (now_text(), str(work_state), session_id),
                )
            else:
                connection.execute(
                    "UPDATE sessions SET last_seen_at=? WHERE id=?", (now_text(), session_id)
                )

    def close_session(self, session_id: str, epoch: int, *, lost: bool = False) -> None:
        row = self.assert_current(session_id, epoch)
        state = SessionLifecycle.LOST if lost else SessionLifecycle.CLOSED
        with self.connect(immediate=True) as connection:
            connection.execute(
                "UPDATE sessions SET lifecycle=?, ended_at=? WHERE id=?",
                (state, now_text(), session_id),
            )
            connection.execute(
                "UPDATE identities SET active_session_id=NULL WHERE logical_id=? AND active_session_id=?",
                (row["logical_id"], session_id),
            )
            connection.execute("DELETE FROM writer_leases WHERE session_id=?", (session_id,))

    def send_message(
        self,
        sender: str,
        recipient: str,
        kind: str,
        body: str,
        *,
        project_id: str | None = None,
        requires_ack: bool = False,
        idempotency_key: str | None = None,
    ) -> str:
        message_id = f"msg_{uuid.uuid4().hex}"
        with self.connect() as connection:
            try:
                connection.execute(
                    """INSERT INTO messages(
                      id, idempotency_key, sender, recipient, project_id, kind, body, requires_ack, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        message_id,
                        idempotency_key,
                        sender,
                        recipient,
                        project_id,
                        kind,
                        body,
                        int(requires_ack),
                        now_text(),
                    ),
                )
            except sqlite3.IntegrityError:
                if not idempotency_key:
                    raise
                existing = connection.execute(
                    "SELECT id FROM messages WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                return str(existing["id"])
        self.event(
            "message.sent", sender, {"message_id": message_id, "recipient": recipient}, project_id
        )
        return message_id

    def enqueue_work(
        self, recipient: str, project_id: str, *, message_id: str | None = None, priority: int = 100
    ) -> str:
        work_id = f"work_{uuid.uuid4().hex}"
        with self.connect(immediate=True) as connection:
            if message_id:
                existing = connection.execute(
                    "SELECT id FROM work_queue WHERE message_id=?", (message_id,)
                ).fetchone()
                if existing:
                    return str(existing["id"])
            sequence = int(
                connection.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM work_queue").fetchone()[0]
            )
            connection.execute(
                """INSERT INTO work_queue(
                id, message_id, recipient, project_id, priority, status, sequence, created_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (work_id, message_id, recipient, project_id, priority, sequence, now_text()),
            )
        return work_id

    def claim_work(self, recipient: str) -> dict[str, Any] | None:
        with self.connect(immediate=True) as connection:
            running = connection.execute(
                "SELECT id FROM work_queue WHERE recipient=? AND status IN ('claimed','running')",
                (recipient,),
            ).fetchone()
            if running:
                return None
            row = connection.execute(
                """SELECT * FROM work_queue WHERE recipient=? AND status='queued'
                ORDER BY priority, sequence LIMIT 1""",
                (recipient,),
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(18)
            connection.execute(
                """UPDATE work_queue SET status='claimed', claim_token=?, claimed_at=?,
                attempt_count=attempt_count+1 WHERE id=? AND status='queued'""",
                (token, now_text(), row["id"]),
            )
            result = dict(row)
            result.update({"status": "claimed", "claim_token": token})
            return result

    def requeue_interrupted_work(self, recipient: str) -> list[str]:
        """Release work abandoned by a dead Owner worker for exact-thread retry."""
        with self.connect(immediate=True) as connection:
            rows = connection.execute(
                "SELECT id FROM work_queue WHERE recipient=? AND status IN ('claimed','running')",
                (recipient,),
            ).fetchall()
            work_ids = [str(row["id"]) for row in rows]
            if work_ids:
                connection.execute(
                    """UPDATE work_queue SET status='queued', claim_token=NULL, claimed_at=NULL,
                    provider_turn_id=NULL, error='interrupted worker; queued for exact-thread retry'
                    WHERE recipient=? AND status IN ('claimed','running')""",
                    (recipient,),
                )
        return work_ids

    def start_work(self, work_id: str, claim_token: str, provider_turn_id: str | None = None) -> None:
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE work_queue SET status='running', provider_turn_id=?
                WHERE id=? AND status='claimed' AND claim_token=?""",
                (provider_turn_id, work_id, claim_token),
            ).rowcount
        if not changed:
            raise ConflictError(f"work claim is no longer current: {work_id}")

    def finish_work(self, work_id: str, claim_token: str, *, error: str | None = None) -> None:
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE work_queue SET status=?, completed_at=?, error=?
                WHERE id=? AND status IN ('claimed','running') AND claim_token=?""",
                ("failed" if error else "done", now_text(), error, work_id, claim_token),
            ).rowcount
        if not changed:
            raise ConflictError(f"work claim is no longer current: {work_id}")

    def queued_work(self, recipient: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM work_queue"
        params: tuple[Any, ...] = ()
        if recipient:
            query += " WHERE recipient=?"
            params = (recipient,)
        query += " ORDER BY priority, sequence"
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, params)]

    def record_launch_endorsement(self, project_id: str, actor: str, digest: str) -> None:
        actor = actor.upper()
        if actor not in {"CEO", "CTO"}:
            raise AuthorizationError("only CEO or CTO can endorse a launch packet")
        column = "ceo_endorsed_digest" if actor == "CEO" else "cto_endorsed_digest"
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO launch_packets(project_id, revision_digest, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET revision_digest=excluded.revision_digest,
                updated_at=excluded.updated_at""",
                (project_id, digest, now_text()),
            )
            connection.execute(
                f"UPDATE launch_packets SET {column}=? WHERE project_id=?", (digest, project_id)
            )

    def launch_packet(self, project_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM launch_packets WHERE project_id=?", (project_id,)
            ).fetchone()
        return dict(row) if row else None

    def mark_launched(self, project_id: str, digest: str, board_override_turn_id: str | None) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO launch_packets(
                project_id, revision_digest, board_override_turn_id, launched_at, updated_at
                ) VALUES (?, ?, ?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET
                revision_digest=excluded.revision_digest, board_override_turn_id=excluded.board_override_turn_id,
                launched_at=excluded.launched_at, updated_at=excluded.updated_at""",
                (project_id, digest, board_override_turn_id, now_text(), now_text()),
            )

    def record_human_turn(
        self, provider_thread_id: str, provider_turn_id: str, prompt: str
    ) -> None:
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        with self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO human_turns(
                provider_thread_id, provider_turn_id, prompt, prompt_digest, received_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (provider_thread_id, provider_turn_id, prompt, digest, now_text()),
            )

    def consume_launch_override(self, project_id: str, provider_thread_id: str) -> str | None:
        cutoff = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
        pattern = re.compile(
            rf"(?is)(?:\bjust\s+launch\b|\bdirectly\s+launch\b|直接启动).*\b{re.escape(project_id)}\b"
        )
        with self.connect(immediate=True) as connection:
            rows = connection.execute(
                """SELECT * FROM human_turns WHERE provider_thread_id=? AND consumed_scope IS NULL
                AND received_at>=? ORDER BY received_at DESC""",
                (provider_thread_id, cutoff),
            ).fetchall()
            row = next((item for item in rows if pattern.search(item["prompt"])), None)
            if row is None:
                return None
            connection.execute(
                "UPDATE human_turns SET consumed_scope=? WHERE provider_thread_id=? AND provider_turn_id=?",
                (f"launch:{project_id}", provider_thread_id, row["provider_turn_id"]),
            )
            return str(row["provider_turn_id"])

    def request_succession(self, role: str, candidate_session_id: str) -> str:
        role = role.upper()
        if role not in {"CEO", "CTO"}:
            raise AuthorizationError("succession is only defined for CEO or CTO")
        with self.connect(immediate=True) as connection:
            candidate = connection.execute(
                "SELECT * FROM sessions WHERE id=?", (candidate_session_id,)
            ).fetchone()
            if candidate is None or candidate["authority_kind"] != AuthorityKind.CANDIDATE:
                raise AuthorizationError("succession must be requested by a recovery candidate")
            old = connection.execute(
                "SELECT active_session_id FROM identities WHERE logical_id=?", (role,)
            ).fetchone()
            code = secrets.token_hex(2).upper()
            phrase = f"PROMOTE {role} {code}"
            request_id = f"succ_{uuid.uuid4().hex}"
            expires = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
            connection.execute(
                """INSERT INTO succession_requests(
                id, role, old_session_id, candidate_session_id, nonce_hash, expires_at, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    request_id,
                    role,
                    old["active_session_id"] if old else None,
                    candidate_session_id,
                    hashlib.sha256(phrase.encode()).hexdigest(),
                    expires,
                    now_text(),
                ),
            )
        return phrase

    def confirm_succession(
        self, candidate_session_id: str, provider_turn_id: str, prompt: str
    ) -> dict[str, Any] | None:
        prompt_hash = hashlib.sha256(prompt.strip().encode()).hexdigest()
        with self.connect(immediate=True) as connection:
            request = connection.execute(
                """SELECT * FROM succession_requests WHERE candidate_session_id=?
                AND status='pending' ORDER BY created_at DESC LIMIT 1""",
                (candidate_session_id,),
            ).fetchone()
            if request is None or request["nonce_hash"] != prompt_hash:
                return None
            if datetime.fromisoformat(request["expires_at"]) < datetime.now(UTC):
                connection.execute(
                    "UPDATE succession_requests SET status='expired' WHERE id=?", (request["id"],)
                )
                return None
            role = str(request["role"])
            identity = connection.execute(
                "SELECT active_session_id, epoch FROM identities WHERE logical_id=?", (role,)
            ).fetchone()
            next_epoch = (int(identity["epoch"]) if identity else 0) + 1
            if identity and identity["active_session_id"]:
                connection.execute(
                    "UPDATE sessions SET lifecycle='superseded', ended_at=? WHERE id=?",
                    (now_text(), identity["active_session_id"]),
                )
            candidate = connection.execute(
                "SELECT logical_id FROM sessions WHERE id=?", (candidate_session_id,)
            ).fetchone()
            if candidate is None:
                raise SessionError(f"unknown candidate session: {candidate_session_id}")
            candidate_logical = str(candidate["logical_id"])
            connection.execute(
                "UPDATE identities SET active_session_id=NULL WHERE logical_id=?",
                (candidate_logical,),
            )
            if identity:
                connection.execute(
                    "UPDATE identities SET active_session_id=?, epoch=? WHERE logical_id=?",
                    (candidate_session_id, next_epoch, role),
                )
            else:
                connection.execute(
                    "INSERT INTO identities(logical_id, active_session_id, epoch) VALUES (?, ?, ?)",
                    (role, candidate_session_id, next_epoch),
                )
            connection.execute(
                """UPDATE sessions SET logical_id=?, epoch=?, authority_kind='canonical',
                lifecycle='live', ended_at=NULL, last_seen_at=? WHERE id=?""",
                (role, next_epoch, now_text(), candidate_session_id),
            )
            connection.execute("DELETE FROM identities WHERE logical_id=?", (candidate_logical,))
            connection.execute(
                "UPDATE succession_requests SET status='confirmed', confirmed_turn_id=? WHERE id=?",
                (provider_turn_id, request["id"]),
            )
        return {"role": role, "session_id": candidate_session_id, "epoch": next_epoch}

    def set_capability_health(
        self, name: str, source: str, state: str, diagnostic: str = ""
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO capability_health(
                capability_name, source, state, diagnostic, checked_at
                ) VALUES (?, ?, ?, ?, ?) ON CONFLICT(capability_name) DO UPDATE SET
                source=excluded.source, state=excluded.state, diagnostic=excluded.diagnostic,
                checked_at=excluded.checked_at""",
                (name, source, state, diagnostic, now_text()),
            )

    def inbox(self, recipient: str, *, unread_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM messages WHERE recipient IN (?, ?)"
        parameters: list[Any] = [recipient, "ALL"]
        if unread_only:
            query += " AND acked_at IS NULL"
        query += " ORDER BY created_at, id"
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters)]

    def acknowledge(self, message_id: str, actor: str) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT recipient FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if row is None:
                raise ConflictError(f"message not found: {message_id}")
            if row["recipient"] not in {actor, "ALL"}:
                raise AuthorizationError(
                    f"{actor} cannot acknowledge a message for {row['recipient']}"
                )
            connection.execute(
                "UPDATE messages SET acked_at=?, acked_by=? WHERE id=?",
                (now_text(), actor, message_id),
            )

    def acquire_writer(self, project_id: str, session_id: str, epoch: int) -> None:
        row = self.assert_current(session_id, epoch)
        expected = f"{project_id}:owner"
        if row["logical_id"] != expected:
            raise AuthorizationError(f"only {expected} may hold the canonical writer lease")
        with self.connect(immediate=True) as connection:
            lease = connection.execute(
                "SELECT session_id, epoch FROM writer_leases WHERE project_id=?", (project_id,)
            ).fetchone()
            if lease and (lease["session_id"], lease["epoch"]) != (session_id, epoch):
                raise ConflictError(f"{project_id} already has a canonical writer")
            connection.execute(
                "INSERT OR REPLACE INTO writer_leases(project_id, session_id, epoch, acquired_at) VALUES (?, ?, ?, ?)",
                (project_id, session_id, epoch, now_text()),
            )

    def release_writer(self, project_id: str, session_id: str, epoch: int) -> None:
        self.assert_current(session_id, epoch)
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM writer_leases WHERE project_id=? AND session_id=? AND epoch=?",
                (project_id, session_id, epoch),
            )

    def index_checkpoint(
        self,
        checkpoint_id: str,
        project_id: str,
        path: Path,
        spec_id: str,
        task_id: str,
        *,
        session_id: str | None = None,
        epoch: int | None = None,
    ) -> None:
        if (session_id is None) != (epoch is None):
            raise SessionError("session_id and epoch must be provided together")
        if session_id is not None and epoch is not None:
            self.assert_current(session_id, epoch)
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO checkpoint_index(
                  checkpoint_id, project_id, session_id, epoch, file_path, spec_id, task_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    checkpoint_id,
                    project_id,
                    session_id,
                    epoch,
                    str(path),
                    spec_id,
                    task_id,
                    now_text(),
                ),
            )

    def create_action(self, action: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO action_requests(
                  action_id, requester, project_id, domain, summary, effect, reversible, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    action["action_id"],
                    action["requester"],
                    action["project_id"],
                    action["domain"],
                    action["summary"],
                    action["effect"],
                    int(action["reversible"]),
                    str(action["created_at"]),
                ),
            )

    def action(self, action_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            request = connection.execute(
                "SELECT * FROM action_requests WHERE action_id=?", (action_id,)
            ).fetchone()
            if request is None:
                raise ConflictError(f"action not found: {action_id}")
            approvals = [
                dict(row)
                for row in connection.execute(
                    "SELECT approver, decision, note, created_at FROM action_approvals WHERE action_id=? ORDER BY created_at",
                    (action_id,),
                )
            ]
        return {**dict(request), "approvals": approvals}

    def decide_action(self, action_id: str, approver: str, decision: str, note: str = "") -> str:
        approver = approver.upper()
        if approver not in {"CEO", "CTO"}:
            raise AuthorizationError("only CEO or CTO can approve external actions")
        if decision not in {"approve", "reject"}:
            raise ConflictError("decision must be approve or reject")
        with self.connect(immediate=True) as connection:
            request = connection.execute(
                "SELECT domain, status FROM action_requests WHERE action_id=?", (action_id,)
            ).fetchone()
            if request is None:
                raise ConflictError(f"action not found: {action_id}")
            domain = ActionDomain(request["domain"])
            allowed = (
                (domain == ActionDomain.PRODUCT and approver == "CEO")
                or (domain == ActionDomain.TECHNICAL and approver == "CTO")
                or domain == ActionDomain.CROSS_DOMAIN
            )
            if not allowed:
                raise AuthorizationError(f"{approver} cannot decide a {domain} action")
            connection.execute(
                "INSERT OR REPLACE INTO action_approvals(action_id, approver, decision, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (action_id, approver, decision, note, now_text()),
            )
            decisions = {
                row["approver"]: row["decision"]
                for row in connection.execute(
                    "SELECT approver, decision FROM action_approvals WHERE action_id=?",
                    (action_id,),
                )
            }
            if "reject" in decisions.values():
                status = "rejected"
            elif domain == ActionDomain.CROSS_DOMAIN:
                status = (
                    "approved"
                    if decisions.get("CEO") == decisions.get("CTO") == "approve"
                    else "pending"
                )
            else:
                status = "approved"
            connection.execute(
                "UPDATE action_requests SET status=?, resolved_at=? WHERE action_id=?",
                (status, now_text() if status != "pending" else None, action_id),
            )
        return status

    def snapshot(self, *, extended: bool = False) -> dict[str, Any]:
        with self.connect() as connection:
            identities = [
                dict(row)
                for row in connection.execute(
                    """SELECT i.logical_id, i.epoch, s.id AS session_id, s.lifecycle, s.work_state,
                s.last_seen_at, s.tmux_target, s.provider_session_id, s.authority_kind,
                s.terminal_transport, s.terminal_handle, s.parent_session_id
                FROM identities i JOIN sessions s ON s.id=i.active_session_id ORDER BY i.logical_id"""
                )
            ]
            unread = {
                row["recipient"]: row["count"]
                for row in connection.execute(
                    "SELECT recipient, count(*) AS count FROM messages WHERE acked_at IS NULL GROUP BY recipient"
                )
            }
            actions = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM action_requests WHERE status='pending' ORDER BY created_at"
                )
            ]
            work = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM work_queue WHERE status IN ('queued','claimed','running','failed') ORDER BY priority, sequence"
                )
            ]
            capabilities = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM capability_health ORDER BY capability_name"
                )
            ]
        result = {
            "identities": identities,
            "unread": unread,
            "pending_actions": actions,
        }
        if extended:
            result.update({"work_queue": work, "capabilities": capabilities})
        return result

    def reset_indexes(self) -> None:
        """Remove rebuildable runtime state while preserving schema."""
        with self.connect(immediate=True) as connection:
            for table in (
                "checkpoint_index",
                "events",
                "writer_leases",
                "action_approvals",
                "action_requests",
                "work_queue",
                "messages",
                "launch_packets",
                "human_turns",
                "succession_requests",
                "capability_health",
                "sessions",
                "identities",
            ):
                connection.execute(f"DELETE FROM {table}")
