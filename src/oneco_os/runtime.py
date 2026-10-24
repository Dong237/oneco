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
from .models import ActionDomain, AuthorityKind, IdentityKind, SessionLifecycle, WorkState

SCHEMA_VERSION = 3


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
  created_at TEXT NOT NULL, acked_at TEXT, acked_by TEXT, subject TEXT NOT NULL DEFAULT '',
  thread_key TEXT, object_id TEXT, artifact_digest TEXT, sender_session_id TEXT,
  sender_epoch INTEGER, stale_version INTEGER NOT NULL DEFAULT 0, sequence INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS messages_recipient_idx ON messages(recipient, acked_at, created_at);
CREATE TABLE IF NOT EXISTS message_acknowledgements (
  message_id TEXT NOT NULL, actor TEXT NOT NULL, acked_at TEXT NOT NULL,
  PRIMARY KEY(message_id, actor), FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
);
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
CREATE TABLE IF NOT EXISTS durable_records (
  record_id TEXT PRIMARY KEY, record_type TEXT NOT NULL, object_id TEXT NOT NULL,
  file_path TEXT NOT NULL UNIQUE, digest TEXT NOT NULL, state TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS durable_records_object_idx
  ON durable_records(object_id, record_type, created_at);
CREATE TABLE IF NOT EXISTS role_work_queue (
  id TEXT PRIMARY KEY, recipient TEXT NOT NULL, kind TEXT NOT NULL, object_id TEXT,
  payload TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 100, status TEXT NOT NULL,
  sequence INTEGER NOT NULL, idempotency_key TEXT UNIQUE, claim_token TEXT,
  claimed_at TEXT, completed_at TEXT, error TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS role_work_recipient_idx
  ON role_work_queue(recipient, status, priority, sequence);
CREATE TABLE IF NOT EXISTS role_turn_leases (
  role TEXT PRIMARY KEY, session_id TEXT NOT NULL, epoch INTEGER NOT NULL,
  lease_token TEXT NOT NULL, acquired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supervision (
  project_id TEXT PRIMARY KEY, last_review_at TEXT, next_review_at TEXT,
  last_verdict TEXT, progress_requested_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS release_index (
  release_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, release_digest TEXT NOT NULL,
  state TEXT NOT NULL, file_path TEXT NOT NULL UNIQUE, updated_at TEXT NOT NULL
);
"""

SESSION_V2_COLUMNS = {
    "authority_kind": "TEXT NOT NULL DEFAULT 'canonical'",
    "terminal_transport": "TEXT",
    "terminal_handle": "TEXT",
    "launch_nonce": "TEXT",
    "parent_session_id": "TEXT",
}

SESSION_V3_COLUMNS = {
    "identity_kind": "TEXT NOT NULL DEFAULT 'persistent_role'",
}

MESSAGE_V3_COLUMNS = {
    "subject": "TEXT NOT NULL DEFAULT ''",
    "thread_key": "TEXT",
    "object_id": "TEXT",
    "artifact_digest": "TEXT",
    "sender_session_id": "TEXT",
    "sender_epoch": "INTEGER",
    "stale_version": "INTEGER NOT NULL DEFAULT 0",
    "sequence": "INTEGER NOT NULL DEFAULT 0",
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
            for name, declaration in SESSION_V3_COLUMNS.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE sessions ADD COLUMN {name} {declaration}")
            connection.execute(
                "UPDATE sessions SET identity_kind=? WHERE logical_id LIKE '%:owner'",
                (IdentityKind.OWNER,),
            )
            connection.execute(
                "UPDATE sessions SET identity_kind=? WHERE logical_id LIKE '%:secretary:%'",
                (IdentityKind.DELEGATE,),
            )
            connection.execute(
                "UPDATE sessions SET identity_kind=? WHERE logical_id LIKE '%:candidate:%'",
                (IdentityKind.CANDIDATE,),
            )
            connection.execute(
                "UPDATE sessions SET authority_kind=? WHERE logical_id='CPO'",
                (AuthorityKind.ADVISORY,),
            )
            message_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(messages)")
            }
            for name, declaration in MESSAGE_V3_COLUMNS.items():
                if name not in message_columns:
                    connection.execute(f"ALTER TABLE messages ADD COLUMN {name} {declaration}")
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
        identity_kind: IdentityKind | str | None = None,
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
                    connection.execute("DELETE FROM role_turn_leases WHERE session_id=?", (active,))
                connection.execute(
                    "UPDATE identities SET active_session_id=?, epoch=? WHERE logical_id=?",
                    (session_id, epoch, logical_id),
                )
            kind = str(authority_kind or self._infer_authority(logical_id))
            identity = str(identity_kind or self._infer_identity(logical_id))
            connection.execute(
                """INSERT INTO sessions(
                  id, logical_id, epoch, provider, provider_session_id, cwd, tmux_target, pid,
                  lifecycle, work_state, last_seen_at, authority_kind, terminal_transport,
                  terminal_handle, launch_nonce, parent_session_id, identity_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    identity,
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
        if logical_id == "CPO":
            return AuthorityKind.ADVISORY
        return AuthorityKind.CANONICAL

    @staticmethod
    def _infer_identity(logical_id: str) -> IdentityKind:
        if ":secretary:" in logical_id:
            return IdentityKind.DELEGATE
        if ":candidate:" in logical_id:
            return IdentityKind.CANDIDATE
        if logical_id.endswith(":owner"):
            return IdentityKind.OWNER
        return IdentityKind.PERSISTENT_ROLE

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

    def prepare_process_launch(self, session_id: str, epoch: int, launch_nonce: str) -> None:
        """Fence a new native-process attempt and clear observations from the prior attempt."""

        self.assert_current(session_id, epoch)
        with self.connect(immediate=True) as connection:
            connection.execute(
                """UPDATE sessions SET launch_nonce=?, pid=NULL, terminal_handle=NULL,
                last_seen_at=? WHERE id=?""",
                (launch_nonce, now_text(), session_id),
            )

    def bind_process(
        self,
        session_id: str,
        epoch: int,
        pid: int,
        *,
        launch_nonce: str | None = None,
    ) -> None:
        """Bind the running wrapper only if it still owns the current launch attempt."""

        with self.connect(immediate=True) as connection:
            row = connection.execute(
                """SELECT s.*, i.active_session_id, i.epoch AS current_epoch
                FROM sessions s JOIN identities i ON i.logical_id=s.logical_id WHERE s.id=?""",
                (session_id,),
            ).fetchone()
            if row is None:
                raise SessionError(f"unknown session: {session_id}")
            if (
                row["active_session_id"] != session_id
                or int(row["current_epoch"]) != int(epoch)
                or row["lifecycle"] != SessionLifecycle.LIVE
            ):
                raise StaleSessionError(f"stale session {session_id}")
            if launch_nonce is not None and row["launch_nonce"] != launch_nonce:
                raise StaleSessionError(f"stale launch attempt for session {session_id}")
            connection.execute(
                "UPDATE sessions SET pid=?, last_seen_at=? WHERE id=?",
                (pid, now_text(), session_id),
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
        with self.connect(immediate=True) as connection:
            connection.execute(
                """UPDATE sessions SET lifecycle='lost', work_state=?, ended_at=?,
                last_seen_at=? WHERE id=?""",
                (str(WorkState.IDLE), now_text(), now_text(), session_id),
            )
            connection.execute("DELETE FROM writer_leases WHERE session_id=?", (session_id,))
            connection.execute("DELETE FROM role_turn_leases WHERE session_id=?", (session_id,))

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
                "UPDATE sessions SET lifecycle=?, work_state=?, ended_at=? WHERE id=?",
                (state, str(WorkState.IDLE), now_text(), session_id),
            )
            connection.execute(
                "UPDATE identities SET active_session_id=NULL WHERE logical_id=? AND active_session_id=?",
                (row["logical_id"], session_id),
            )
            connection.execute("DELETE FROM writer_leases WHERE session_id=?", (session_id,))
            connection.execute("DELETE FROM role_turn_leases WHERE session_id=?", (session_id,))

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
        subject: str = "",
        thread_key: str | None = None,
        object_id: str | None = None,
        artifact_digest: str | None = None,
        sender_session_id: str | None = None,
        sender_epoch: int | None = None,
        stale_version: bool = False,
    ) -> str:
        message_id = f"msg_{uuid.uuid4().hex}"
        payload = (
            sender,
            recipient,
            project_id,
            kind,
            body,
            int(requires_ack),
            subject,
            thread_key,
            object_id,
            artifact_digest,
            sender_session_id,
            sender_epoch,
            int(stale_version),
        )
        idempotent_payload = (*payload[:10], payload[12])
        with self.connect(immediate=True) as connection:
            if (sender_session_id is None) != (sender_epoch is None):
                raise StaleSessionError("message sender session and epoch must be supplied together")
            if sender_session_id is not None and sender_epoch is not None:
                current_sender = connection.execute(
                    """SELECT s.logical_id, s.epoch, s.lifecycle, i.active_session_id,
                    i.epoch AS current_epoch FROM sessions s
                    JOIN identities i ON i.logical_id=s.logical_id WHERE s.id=?""",
                    (sender_session_id,),
                ).fetchone()
                if (
                    current_sender is None
                    or current_sender["logical_id"] != sender
                    or current_sender["active_session_id"] != sender_session_id
                    or int(current_sender["epoch"]) != int(sender_epoch)
                    or int(current_sender["current_epoch"]) != int(sender_epoch)
                    or current_sender["lifecycle"] != SessionLifecycle.LIVE
                ):
                    raise StaleSessionError(
                        f"stale message sender session {sender_session_id} at epoch {sender_epoch}"
                    )
            if idempotency_key:
                existing = connection.execute(
                    """SELECT id, sender, recipient, project_id, kind, body, requires_ack,
                    subject, thread_key, object_id, artifact_digest, stale_version
                    FROM messages WHERE idempotency_key=?""",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    existing_payload = tuple(existing[key] for key in existing.keys() if key != "id")
                    if existing_payload != idempotent_payload:
                        raise ConflictError(
                            f"idempotency key {idempotency_key} was already used for a different message"
                        )
                    return str(existing["id"])
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages WHERE thread_key IS ?",
                    (thread_key,),
                ).fetchone()[0]
            )
            connection.execute(
                """INSERT INTO messages(
                  id, idempotency_key, sender, recipient, project_id, kind, body, requires_ack,
                  created_at, subject, thread_key, object_id, artifact_digest, sender_session_id,
                  sender_epoch, stale_version, sequence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (message_id, idempotency_key, *payload[:6], now_text(), *payload[6:], sequence),
            )
        self.event(
            "message.sent", sender, {"message_id": message_id, "recipient": recipient}, project_id
        )
        return message_id

    def mark_object_messages_stale(self, object_id: str, current_digest: str) -> int:
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE messages SET stale_version=1
                WHERE object_id=? AND artifact_digest IS NOT NULL AND artifact_digest<>?""",
                (object_id, current_digest),
            ).rowcount
        return int(changed)

    def index_record(
        self,
        record_id: str,
        record_type: str,
        object_id: str,
        path: Path,
        digest: str,
        state: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO durable_records(
                record_id, record_type, object_id, file_path, digest, state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (record_id, record_type, object_id, str(path), digest, state, now_text()),
            )

    def records(self, *, record_type: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM durable_records"
        params: tuple[Any, ...] = ()
        if record_type:
            query += " WHERE record_type=?"
            params = (record_type,)
        query += " ORDER BY created_at, record_id"
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, params)]

    def enqueue_role_work(
        self,
        recipient: str,
        kind: str,
        payload: dict[str, Any],
        *,
        object_id: str | None = None,
        priority: int = 100,
        idempotency_key: str | None = None,
    ) -> str:
        work_id = f"rolework_{uuid.uuid4().hex}"
        with self.connect(immediate=True) as connection:
            if idempotency_key:
                existing = connection.execute(
                    """SELECT id, recipient, kind, object_id, payload, priority
                    FROM role_work_queue WHERE idempotency_key=?""",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    existing_payload = json.loads(str(existing["payload"]))
                    if (
                        existing["recipient"] != recipient
                        or existing["kind"] != kind
                        or existing["object_id"] != object_id
                        or existing_payload != payload
                        or int(existing["priority"]) != priority
                    ):
                        raise ConflictError(
                            f"idempotency key {idempotency_key} was already used for different role work"
                        )
                    return str(existing["id"])
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM role_work_queue WHERE recipient=?",
                    (recipient,),
                ).fetchone()[0]
            )
            connection.execute(
                """INSERT INTO role_work_queue(
                id, recipient, kind, object_id, payload, priority, status, sequence,
                idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)""",
                (
                    work_id,
                    recipient,
                    kind,
                    object_id,
                    json.dumps(payload),
                    priority,
                    sequence,
                    idempotency_key,
                    now_text(),
                ),
            )
        return work_id

    def claim_role_work(self, recipient: str) -> dict[str, Any] | None:
        with self.connect(immediate=True) as connection:
            current = connection.execute(
                "SELECT id FROM role_work_queue WHERE recipient=? AND status IN ('claimed','running')",
                (recipient,),
            ).fetchone()
            if current:
                return None
            row = connection.execute(
                """SELECT * FROM role_work_queue WHERE recipient=? AND status='queued'
                ORDER BY priority, sequence LIMIT 1""",
                (recipient,),
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_hex(16)
            connection.execute(
                """UPDATE role_work_queue SET status='claimed', claim_token=?, claimed_at=?
                WHERE id=? AND status='queued'""",
                (token, now_text(), row["id"]),
            )
            result = dict(row)
            result.update({"status": "claimed", "claim_token": token})
            result["payload"] = json.loads(str(result["payload"]))
            return result

    def finish_role_work(self, work_id: str, claim_token: str, *, error: str | None = None) -> None:
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE role_work_queue SET status=?, completed_at=?, error=?
                WHERE id=? AND status IN ('claimed','running') AND claim_token=?""",
                ("failed" if error else "done", now_text(), error, work_id, claim_token),
            ).rowcount
        if not changed:
            raise ConflictError(f"role work claim is no longer current: {work_id}")

    def resolve_messages(
        self,
        actor: str,
        *,
        message_id: str | None = None,
        kind: str | None = None,
        object_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, int]:
        """Acknowledge completed inputs and retire their matching queue rows.

        This is deliberately idempotent and may reconcile queued, running, failed, or
        already-completed work after a durable operation succeeds.
        """
        clauses = ["recipient=?", "stale_version=0"]
        parameters: list[Any] = [actor]
        for column, value in (
            ("id", message_id),
            ("kind", kind),
            ("object_id", object_id),
            ("project_id", project_id),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                parameters.append(value)
        with self.connect(immediate=True) as connection:
            rows = connection.execute(
                f"SELECT id, kind, object_id FROM messages WHERE {' AND '.join(clauses)}",
                parameters,
            ).fetchall()
            message_ids = [str(row["id"]) for row in rows]
            role_kind_for_message = {
                "signal_assignment": "signal_assignment",
                "bet_decision": "bet_decision",
                "technical_shaping_request": "technical_shaping_request",
                "review_request": "gate_review",
                "technical_review": "review_discussion",
                "release_candidate": "release_candidate",
                "supervision_due": "scheduled_review",
            }
            expected_role_kinds = {
                role_kind_for_message[str(row["kind"])]
                for row in rows
                if str(row["kind"]) in role_kind_for_message
            }
            acknowledged = 0
            owner_work = 0
            role_work = 0
            for current_message_id in message_ids:
                acknowledged += connection.execute(
                    """UPDATE messages SET acked_at=COALESCE(acked_at, ?),
                    acked_by=COALESCE(acked_by, ?) WHERE id=?""",
                    (now_text(), actor, current_message_id),
                ).rowcount
                owner_work += connection.execute(
                    """UPDATE work_queue SET status='done', completed_at=COALESCE(completed_at, ?),
                    claim_token=NULL, claimed_at=NULL, error=NULL
                    WHERE message_id=? AND recipient=? AND status<>'done'""",
                    (now_text(), current_message_id, actor),
                ).rowcount
            role_rows = connection.execute(
                """SELECT id, kind, object_id, payload FROM role_work_queue
                WHERE recipient=? AND status<>'done'""",
                (actor,),
            ).fetchall()
            wanted = set(message_ids)
            for row in role_rows:
                payload = json.loads(str(row["payload"]))
                payload_matches = str(payload.get("message_id") or "") in wanted
                object_matches = (
                    object_id is not None
                    and str(row["object_id"] or "") == object_id
                    and str(row["kind"]) in expected_role_kinds
                )
                if not payload_matches and not object_matches:
                    continue
                role_work += connection.execute(
                    """UPDATE role_work_queue SET status='done', completed_at=COALESCE(completed_at, ?),
                    claim_token=NULL, claimed_at=NULL, error=NULL WHERE id=?""",
                    (now_text(), row["id"]),
                ).rowcount
        return {
            "messages": acknowledged,
            "owner_work": owner_work,
            "role_work": role_work,
        }

    def queued_role_work(self, recipient: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM role_work_queue"
        params: tuple[Any, ...] = ()
        if recipient:
            query += " WHERE recipient=?"
            params = (recipient,)
        query += " ORDER BY priority, sequence"
        with self.connect() as connection:
            rows = [dict(row) for row in connection.execute(query, params)]
        for row in rows:
            row["payload"] = json.loads(str(row["payload"]))
        return rows

    def acquire_role_turn(self, role: str, session_id: str, epoch: int) -> str:
        token = secrets.token_hex(16)
        with self.connect(immediate=True) as connection:
            current = connection.execute(
                """SELECT s.logical_id, s.lifecycle, i.active_session_id,
                i.epoch AS current_epoch FROM sessions s
                JOIN identities i ON i.logical_id=s.logical_id WHERE s.id=?""",
                (session_id,),
            ).fetchone()
            if (
                current is None
                or current["active_session_id"] != session_id
                or int(current["current_epoch"]) != int(epoch)
                or current["lifecycle"] != SessionLifecycle.LIVE
            ):
                raise StaleSessionError(f"stale session {session_id}")
            lease = connection.execute(
                "SELECT session_id, epoch FROM role_turn_leases WHERE role=?", (role,)
            ).fetchone()
            if lease and (lease["session_id"], int(lease["epoch"])) != (session_id, int(epoch)):
                holder = connection.execute(
                    """SELECT s.lifecycle, i.active_session_id, i.epoch AS current_epoch
                    FROM sessions s LEFT JOIN identities i ON i.logical_id=s.logical_id
                    WHERE s.id=?""",
                    (lease["session_id"],),
                ).fetchone()
                holder_is_current = bool(
                    holder
                    and holder["active_session_id"] == lease["session_id"]
                    and int(holder["current_epoch"]) == int(lease["epoch"])
                    and holder["lifecycle"] == SessionLifecycle.LIVE
                )
                if holder_is_current:
                    raise ConflictError(f"{role} already has an active turn")
                connection.execute("DELETE FROM role_turn_leases WHERE role=?", (role,))
            connection.execute(
                """INSERT OR REPLACE INTO role_turn_leases(
                role, session_id, epoch, lease_token, acquired_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (role, session_id, epoch, token, now_text()),
            )
        return token

    def release_role_turn(self, role: str, token: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM role_turn_leases WHERE role=? AND lease_token=?", (role, token)
            )

    def set_supervision_due(
        self,
        project_id: str,
        *,
        next_review_at: str,
        last_review_at: str | None = None,
        verdict: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO supervision(
                project_id, last_review_at, next_review_at, last_verdict, updated_at
                ) VALUES (?, ?, ?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET
                last_review_at=COALESCE(excluded.last_review_at, supervision.last_review_at),
                next_review_at=excluded.next_review_at,
                last_verdict=COALESCE(excluded.last_verdict, supervision.last_verdict),
                updated_at=excluded.updated_at""",
                (project_id, last_review_at, next_review_at, verdict, now_text()),
            )

    def due_supervision(self, at: str | None = None) -> list[dict[str, Any]]:
        cutoff = at or now_text()
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM supervision WHERE next_review_at<=? ORDER BY next_review_at",
                    (cutoff,),
                )
            ]

    def request_progress(self, project_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO supervision(project_id, progress_requested_at, updated_at)
                VALUES (?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET
                progress_requested_at=excluded.progress_requested_at, updated_at=excluded.updated_at""",
                (project_id, now_text(), now_text()),
            )

    def index_release(
        self, release_id: str, project_id: str, digest: str, state: str, path: Path
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO release_index(
                release_id, project_id, release_digest, state, file_path, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (release_id, project_id, digest, state, str(path), now_text()),
            )

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

    def retry_work(
        self, work_id: str, claim_token: str, error: str, *, max_attempts: int = 3
    ) -> bool:
        """Requeue a transient provider failure until the bounded retry budget is exhausted."""
        with self.connect(immediate=True) as connection:
            row = connection.execute(
                """SELECT attempt_count FROM work_queue
                WHERE id=? AND status IN ('claimed','running') AND claim_token=?""",
                (work_id, claim_token),
            ).fetchone()
            if row is None:
                raise ConflictError(f"work claim is no longer current: {work_id}")
            retry = int(row["attempt_count"]) < max_attempts
            connection.execute(
                """UPDATE work_queue SET status=?, claim_token=NULL, claimed_at=NULL,
                completed_at=?, error=? WHERE id=? AND claim_token=?""",
                (
                    "queued" if retry else "failed",
                    None if retry else now_text(),
                    error,
                    work_id,
                    claim_token,
                ),
            )
        return retry

    def requeue_failed_work(self, work_id: str) -> dict[str, Any]:
        """Explicitly retry a failed Owner item without replacing its durable identity."""
        with self.connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM work_queue WHERE id=?", (work_id,)
            ).fetchone()
            if row is None:
                raise ConflictError(f"work item not found: {work_id}")
            if row["status"] != "failed":
                raise ConflictError(f"work item {work_id} is {row['status']}, not failed")
            connection.execute(
                """UPDATE work_queue SET status='queued', claim_token=NULL, claimed_at=NULL,
                completed_at=NULL, attempt_count=0, provider_turn_id=NULL, error=NULL WHERE id=?""",
                (work_id,),
            )
        result = dict(row)
        result.update({"status": "queued", "attempt_count": 0, "error": None})
        return result

    def work_status(self, work_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM work_queue WHERE id=?", (work_id,)
            ).fetchone()
        return str(row["status"]) if row else None

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
            packet = connection.execute(
                "SELECT * FROM launch_packets WHERE project_id=?", (project_id,)
            ).fetchone()
            if packet is None:
                return None
            digest = packet["revision_digest"]
            endorsement_count = sum(
                value == digest
                for value in (
                    packet["ceo_endorsed_digest"],
                    packet["cto_endorsed_digest"],
                )
            )
            if endorsement_count != 1:
                return None
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
        if role not in {"CPO", "CEO", "CTO"}:
            raise AuthorizationError("succession is only defined for persistent company roles")
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
            candidate = connection.execute(
                """SELECT s.*, i.active_session_id, i.epoch AS current_epoch
                FROM sessions s JOIN identities i ON i.logical_id=s.logical_id
                WHERE s.id=?""",
                (candidate_session_id,),
            ).fetchone()
            if (
                candidate is None
                or candidate["active_session_id"] != candidate_session_id
                or int(candidate["current_epoch"]) != int(candidate["epoch"])
                or candidate["lifecycle"] != SessionLifecycle.LIVE
                or candidate["authority_kind"] != AuthorityKind.CANDIDATE
                or candidate["identity_kind"] != IdentityKind.CANDIDATE
                or not str(candidate["logical_id"]).startswith(f"{role}:candidate:")
            ):
                raise StaleSessionError(f"stale succession candidate {candidate_session_id}")
            identity = connection.execute(
                "SELECT active_session_id, epoch FROM identities WHERE logical_id=?", (role,)
            ).fetchone()
            current_role_session = identity["active_session_id"] if identity else None
            if current_role_session != request["old_session_id"]:
                raise StaleSessionError(f"{role} changed after succession was requested")
            next_epoch = (int(identity["epoch"]) if identity else 0) + 1
            if identity and identity["active_session_id"]:
                connection.execute(
                    "UPDATE sessions SET lifecycle='superseded', ended_at=? WHERE id=?",
                    (now_text(), identity["active_session_id"]),
                )
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
                """UPDATE sessions SET logical_id=?, epoch=?, authority_kind=?, identity_kind=?,
                lifecycle='live', ended_at=NULL, last_seen_at=? WHERE id=?""",
                (
                    role,
                    next_epoch,
                    AuthorityKind.ADVISORY if role == "CPO" else AuthorityKind.CANONICAL,
                    IdentityKind.PERSISTENT_ROLE,
                    now_text(),
                    candidate_session_id,
                ),
            )
            if current_role_session:
                connection.execute(
                    "DELETE FROM role_turn_leases WHERE session_id=?", (current_role_session,)
                )
            connection.execute(
                "DELETE FROM role_turn_leases WHERE session_id=?", (candidate_session_id,)
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
        query = """SELECT m.*, a.acked_at AS recipient_acked_at,
        a.actor AS recipient_acked_by FROM messages m
        LEFT JOIN message_acknowledgements a ON a.message_id=m.id AND a.actor=?
        WHERE m.recipient IN (?, ?) AND m.stale_version=0"""
        parameters: list[Any] = [recipient, recipient, "ALL"]
        if unread_only:
            query += " AND m.requires_ack=1 AND ((m.recipient=? AND m.acked_at IS NULL) OR (m.recipient='ALL' AND a.acked_at IS NULL))"
            parameters.append(recipient)
        query += " ORDER BY COALESCE(m.thread_key, m.id), m.sequence, m.created_at, m.id"
        with self.connect() as connection:
            rows = [dict(row) for row in connection.execute(query, parameters)]
        for row in rows:
            if row["recipient"] == "ALL":
                row["acked_at"] = row.pop("recipient_acked_at")
                row["acked_by"] = row.pop("recipient_acked_by")
            else:
                row.pop("recipient_acked_at", None)
                row.pop("recipient_acked_by", None)
        return rows

    def acknowledge(self, message_id: str, actor: str) -> None:
        with self.connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT recipient, acked_by FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if row is None:
                raise ConflictError(f"message not found: {message_id}")
            if row["recipient"] not in {actor, "ALL"}:
                raise AuthorizationError(
                    f"{actor} cannot acknowledge a message for {row['recipient']}"
                )
            if row["recipient"] == "ALL":
                connection.execute(
                    """INSERT OR IGNORE INTO message_acknowledgements(message_id, actor, acked_at)
                    VALUES (?, ?, ?)""",
                    (message_id, actor, now_text()),
                )
                return
            if row["acked_by"] == actor:
                return
            if row["acked_by"] is not None:
                raise ConflictError(f"message {message_id} was already acknowledged by another role")
            connection.execute(
                "UPDATE messages SET acked_at=?, acked_by=? WHERE id=?",
                (now_text(), actor, message_id),
            )
        self.resolve_messages(actor, message_id=message_id)

    def acquire_writer(self, project_id: str, session_id: str, epoch: int) -> None:
        row = self.assert_current(session_id, epoch)
        expected = f"{project_id}:owner"
        if (
            row["logical_id"] != expected
            or row["authority_kind"] != AuthorityKind.OWNER
            or row["identity_kind"] != IdentityKind.OWNER
        ):
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
        self.validate_action_decision(action_id, approver, decision)
        approver = approver.upper()
        with self.connect(immediate=True) as connection:
            request = connection.execute(
                "SELECT domain, status FROM action_requests WHERE action_id=?", (action_id,)
            ).fetchone()
            if request is None:
                raise ConflictError(f"action not found: {action_id}")
            domain = ActionDomain(request["domain"])
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

    def validate_action_decision(
        self, action_id: str, approver: str, decision: str
    ) -> dict[str, Any]:
        approver = approver.upper()
        if approver not in {"CEO", "CTO"}:
            raise AuthorizationError("only CEO or CTO can approve external actions")
        if decision not in {"approve", "reject"}:
            raise ConflictError("decision must be approve or reject")
        with self.connect() as connection:
            request = connection.execute(
                "SELECT * FROM action_requests WHERE action_id=?", (action_id,)
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
        return dict(request)

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
            unread_recipients = {
                str(row["logical_id"])
                for row in connection.execute("SELECT logical_id FROM identities")
            }
            unread_recipients.update(
                str(row["recipient"])
                for row in connection.execute(
                    "SELECT DISTINCT recipient FROM messages WHERE recipient<>'ALL'"
                )
            )
            unread_recipients.update({"BOARD", "CPO", "CEO", "CTO"})
            unread = {}
            for recipient in sorted(unread_recipients):
                count = int(
                    connection.execute(
                        """SELECT count(*) FROM messages m
                        WHERE m.stale_version=0 AND m.requires_ack=1 AND (
                          (m.recipient=? AND m.acked_at IS NULL)
                          OR (m.recipient='ALL' AND NOT EXISTS (
                            SELECT 1 FROM message_acknowledgements a
                            WHERE a.message_id=m.id AND a.actor=?
                          ))
                        )""",
                        (recipient, recipient),
                    ).fetchone()[0]
                )
                if count:
                    unread[recipient] = count
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
            role_work = [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM role_work_queue
                    WHERE status IN ('queued','claimed','running','failed')
                    ORDER BY priority, sequence"""
                )
            ]
            supervision = [
                dict(row) for row in connection.execute("SELECT * FROM supervision ORDER BY project_id")
            ]
            releases = [
                dict(row) for row in connection.execute("SELECT * FROM release_index ORDER BY updated_at")
            ]
        result = {
            "identities": identities,
            "unread": unread,
            "pending_actions": actions,
        }
        if extended:
            result.update(
                {
                    "work_queue": work,
                    "role_work_queue": role_work,
                    "supervision": supervision,
                    "releases": releases,
                    "capabilities": capabilities,
                }
            )
        return result

    def reset_indexes(self) -> None:
        """Remove rebuildable runtime state while preserving schema."""
        with self.connect(immediate=True) as connection:
            for table in (
                "release_index",
                "supervision",
                "role_turn_leases",
                "role_work_queue",
                "durable_records",
                "checkpoint_index",
                "events",
                "writer_leases",
                "action_approvals",
                "action_requests",
                "work_queue",
                "message_acknowledgements",
                "messages",
                "launch_packets",
                "human_turns",
                "succession_requests",
                "capability_health",
                "sessions",
                "identities",
            ):
                connection.execute(f"DELETE FROM {table}")
