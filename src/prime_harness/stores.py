from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from prime_harness.domain import (
    AgentSession,
    ApprovalRequest,
    Checkpoint,
    ConnectionRecord,
    EventRecord,
    Lane,
    LaneState,
    LaneStateTransition,
    Review,
    ReviewerThread,
    SecretMetadata,
    SecretRecord,
    SessionLease,
    new_id,
)


class Store(Protocol):
    def create_lane(self, lane: Lane) -> None: ...
    def get_lane(self, lane_id: str) -> Lane | None: ...
    def create_session(self, session: AgentSession) -> None: ...
    def get_session(self, session_id: str) -> AgentSession | None: ...
    def list_sessions_for_lane(self, lane_id: str) -> list[AgentSession]: ...
    def acquire_lease(self, lease: SessionLease, now: datetime | None = None) -> bool: ...
    def release_lease(self, session_id: str, lease_id: str) -> None: ...
    def create_checkpoint(self, checkpoint: Checkpoint) -> None: ...
    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None: ...
    def create_review(self, review: Review) -> None: ...
    def create_thread(self, thread: ReviewerThread) -> None: ...
    def get_thread(self, thread_id: str) -> ReviewerThread | None: ...
    def list_threads_for_lane(self, lane_id: str) -> list[ReviewerThread]: ...
    def create_event(self, event: EventRecord) -> None: ...
    def list_events(self, lane_id: str) -> list[EventRecord]: ...
    def create_connection(self, connection: ConnectionRecord) -> None: ...
    def get_connection(self, connection_id: str) -> ConnectionRecord | None: ...
    def get_connection_by_credential_hash(
        self, credential_hash: str
    ) -> ConnectionRecord | None: ...
    def create_secret(self, secret: SecretRecord) -> None: ...
    def get_secret(self, secret_id: str) -> SecretRecord | None: ...
    def list_secrets(self, lane_id: str) -> list[SecretMetadata]: ...
    def delete_secret(self, secret_id: str) -> bool: ...
    def create_transition(self, transition: LaneStateTransition) -> None: ...
    def list_transitions(self, lane_id: str) -> list[LaneStateTransition]: ...
    def list_checkpoints(self, lane_id: str) -> list[Checkpoint]: ...
    def list_reviews(self, lane_id: str) -> list[Review]: ...
    def create_approval_request(self, request: ApprovalRequest) -> ApprovalRequest: ...
    def get_approval_request(self, request_id: str) -> ApprovalRequest | None: ...
    def find_approval_request(self, lane_id: str, request_key: str) -> ApprovalRequest | None: ...
    def claim_approval(
        self, lane_id: str, request_id: str, approver_id: str, at: datetime
    ) -> bool: ...
    def finish_approval(self, request_id: str, status: str, result: dict[str, Any]) -> None: ...


class InMemoryStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._lanes: dict[str, Lane] = {}
        self._sessions: dict[str, AgentSession] = {}
        self._leases: dict[str, SessionLease] = {}
        self._checkpoints: dict[str, Checkpoint] = {}
        self._reviews: dict[str, Review] = {}
        self._threads: dict[str, ReviewerThread] = {}
        self._events: dict[str, EventRecord] = {}
        self._connections: dict[str, ConnectionRecord] = {}
        self._secrets: dict[str, SecretRecord] = {}
        self._transitions: dict[str, list[LaneStateTransition]] = {}
        self._approvals: dict[str, ApprovalRequest] = {}

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, sort_keys=True)

    def create_lane(self, lane: Lane) -> None:
        with self._lock:
            self._lanes[lane.lane_id] = lane

    def get_lane(self, lane_id: str) -> Lane | None:
        with self._lock:
            return self._lanes.get(lane_id)

    def create_session(self, session: AgentSession) -> None:
        with self._lock:
            self._sessions[session.session_id] = session

    def get_session(self, session_id: str) -> AgentSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def list_sessions_for_lane(self, lane_id: str) -> list[AgentSession]:
        with self._lock:
            return [session for session in self._sessions.values() if session.lane_id == lane_id]

    def acquire_lease(self, lease: SessionLease, now: datetime | None = None) -> bool:
        with self._lock:
            current = self._leases.get(lease.session_id)
            if current is not None and current.can_write_by(
                current.writer_id, now or datetime.now(UTC)
            ):
                return False
            self._leases[lease.session_id] = lease
            return True

    def release_lease(self, session_id: str, lease_id: str) -> None:
        with self._lock:
            lease = self._leases.get(session_id)
            if lease is not None and lease.lease_id == lease_id:
                self._leases.pop(session_id, None)

    def create_checkpoint(self, checkpoint: Checkpoint) -> None:
        with self._lock:
            if checkpoint.checkpoint_id in self._checkpoints:
                raise ValueError("Checkpoint records are immutable after creation")
            self._checkpoints[checkpoint.checkpoint_id] = checkpoint

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        with self._lock:
            return self._checkpoints.get(checkpoint_id)

    def create_review(self, review: Review) -> None:
        with self._lock:
            if review.review_id in self._reviews:
                raise ValueError("Review records are immutable after creation")
            self._reviews[review.review_id] = review

    def create_thread(self, thread: ReviewerThread) -> None:
        with self._lock:
            self._threads[thread.thread_id] = thread

    def get_thread(self, thread_id: str) -> ReviewerThread | None:
        with self._lock:
            return self._threads.get(thread_id)

    def list_threads_for_lane(self, lane_id: str) -> list[ReviewerThread]:
        with self._lock:
            return [thread for thread in self._threads.values() if thread.lane_id == lane_id]

    def create_event(self, event: EventRecord) -> None:
        with self._lock:
            self._events[event.event_id] = event

    def list_events(self, lane_id: str) -> list[EventRecord]:
        with self._lock:
            return sorted(
                (event for event in self._events.values() if event.lane_id == lane_id),
                key=lambda item: (item.occurred_at, item.event_id),
            )

    def create_connection(self, connection: ConnectionRecord) -> None:
        with self._lock:
            self._connections[connection.connection_id] = connection

    def get_connection(self, connection_id: str) -> ConnectionRecord | None:
        with self._lock:
            return self._connections.get(connection_id)

    def get_connection_by_credential_hash(self, credential_hash: str) -> ConnectionRecord | None:
        with self._lock:
            return next(
                (
                    item
                    for item in self._connections.values()
                    if item.credential_hash == credential_hash
                ),
                None,
            )

    def create_secret(self, secret: SecretRecord) -> None:
        if not secret.encrypted_value.startswith(b"PHSE1") or len(secret.encrypted_value) < 33:
            raise ValueError("Secret storage requires an authenticated ciphertext envelope")
        with self._lock:
            self._secrets[secret.secret_id] = secret

    def get_secret(self, secret_id: str) -> SecretRecord | None:
        with self._lock:
            return self._secrets.get(secret_id)

    def list_secrets(self, lane_id: str) -> list[SecretMetadata]:
        with self._lock:
            records = sorted(
                (item for item in self._secrets.values() if item.lane_id == lane_id),
                key=lambda item: (item.created_at, item.secret_id),
            )
            return [
                SecretMetadata(
                    secret_id=item.secret_id,
                    lane_id=item.lane_id,
                    owner_id=item.owner_id,
                    purpose=item.purpose,
                    created_at=item.created_at,
                    rotated_at=item.rotated_at,
                    revoked_at=item.revoked_at,
                )
                for item in records
            ]

    def delete_secret(self, secret_id: str) -> bool:
        with self._lock:
            return self._secrets.pop(secret_id, None) is not None

    def create_transition(self, transition: LaneStateTransition) -> None:
        with self._lock:
            self._transitions.setdefault(transition.lane_id, []).append(transition)

    def list_transitions(self, lane_id: str) -> list[LaneStateTransition]:
        with self._lock:
            return list(self._transitions.get(lane_id, []))

    def list_checkpoints(self, lane_id: str) -> list[Checkpoint]:
        with self._lock:
            return sorted(
                (item for item in self._checkpoints.values() if item.lane_id == lane_id),
                key=lambda item: (item.created_at, item.revision, item.checkpoint_id),
            )

    def list_reviews(self, lane_id: str) -> list[Review]:
        with self._lock:
            return sorted(
                (item for item in self._reviews.values() if item.lane_id == lane_id),
                key=lambda item: (item.created_at, item.review_id),
            )

    def create_approval_request(self, request: ApprovalRequest) -> ApprovalRequest:
        with self._lock:
            existing = self.find_approval_request(request.lane_id, request.request_key)
            if existing is not None:
                return existing
            self._approvals[request.request_id] = request
            return request

    def get_approval_request(self, request_id: str) -> ApprovalRequest | None:
        with self._lock:
            return self._approvals.get(request_id)

    def find_approval_request(self, lane_id: str, request_key: str) -> ApprovalRequest | None:
        with self._lock:
            return next(
                (
                    item
                    for item in self._approvals.values()
                    if item.lane_id == lane_id and item.request_key == request_key
                ),
                None,
            )

    def claim_approval(self, lane_id: str, request_id: str, approver_id: str, at: datetime) -> bool:
        with self._lock:
            request = self._approvals.get(request_id)
            if request is None or request.lane_id != lane_id or request.status != "pending":
                return False
            self._approvals[request_id] = ApprovalRequest(
                request_id=request.request_id,
                lane_id=request.lane_id,
                request_key=request.request_key,
                action=request.action,
                parameters=dict(request.parameters),
                status="executing",
                requested_by=request.requested_by,
                requested_at=request.requested_at,
                approved_by=approver_id,
                approved_at=at,
            )
            return True

    def finish_approval(self, request_id: str, status: str, result: dict[str, Any]) -> None:
        with self._lock:
            request = self._approvals[request_id]
            self._approvals[request_id] = ApprovalRequest(
                request_id=request.request_id,
                lane_id=request.lane_id,
                request_key=request.request_key,
                action=request.action,
                parameters=dict(request.parameters),
                status=status,
                requested_by=request.requested_by,
                requested_at=request.requested_at,
                approved_by=request.approved_by,
                approved_at=request.approved_at,
                result=result,
            )


class SqliteStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            self._initialize(connection)

    @staticmethod
    def _dt(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _json(value: Any) -> str:
        def thaw(item: Any) -> Any:
            if hasattr(item, "items"):
                return {key: thaw(child) for key, child in item.items()}
            if isinstance(item, (tuple, set, frozenset)):
                return [thaw(child) for child in item]
            if isinstance(item, list):
                return [thaw(child) for child in item]
            return item

        return json.dumps(thaw(value), sort_keys=True)

    @staticmethod
    def _json_list(value: str | None) -> list[str]:
        if not value:
            return []
        if isinstance(value, list):
            return value
        return json.loads(value)

    @staticmethod
    def _row_to_lane(row: sqlite3.Row) -> Lane:
        return Lane(
            lane_id=row["lane_id"],
            name=row["name"],
            project_id=row["project_id"],
            workspace_path=Path(row["workspace_path"]),
            agent_id=row["agent_id"],
            reviewer_id=row["reviewer_id"],
            state=LaneState(row["state"]),
            owner_id=row["owner_id"],
            provider=row["provider"],
            reviewer_provider=row["reviewer_provider"],
            model=row["model"],
            current_objective=row["current_objective"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection = sqlite3.connect(
            self.path, timeout=30, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        yield connection
        connection.close()

    def _initialize(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS lanes (
                lane_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                project_id TEXT NOT NULL,
                workspace_path TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                reviewer_id TEXT NOT NULL,
                state TEXT NOT NULL,
                owner_id TEXT,
                provider TEXT NOT NULL,
                reviewer_provider TEXT NOT NULL,
                model TEXT NOT NULL,
                current_objective TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                lane_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                provider_session_id TEXT,
                workspace_path TEXT NOT NULL,
                state TEXT NOT NULL,
                process_status TEXT NOT NULL,
                writer_id TEXT,
                writer_lease_id TEXT,
                writer_lease_expires_at TEXT,
                writer_lease_version INTEGER,
                current_objective TEXT,
                current_revision INTEGER,
                last_checkpoint_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(lane_id) REFERENCES lanes(lane_id)
            );
            CREATE TABLE IF NOT EXISTS leases (
                session_id TEXT PRIMARY KEY,
                lease_id TEXT NOT NULL,
                writer_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                checkpoint_id TEXT PRIMARY KEY,
                lane_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                objective TEXT NOT NULL,
                git_status TEXT NOT NULL,
                changed_files TEXT NOT NULL,
                build_results TEXT NOT NULL,
                evidence TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                transcript_id TEXT,
                FOREIGN KEY(lane_id) REFERENCES lanes(lane_id)
            );
            CREATE TABLE IF NOT EXISTS reviews (
                review_id TEXT PRIMARY KEY,
                lane_id TEXT NOT NULL,
                reviewer_thread_id TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                status TEXT NOT NULL,
                verdict TEXT NOT NULL,
                findings TEXT NOT NULL,
                requested_changes TEXT NOT NULL,
                evidence_refs TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                FOREIGN KEY(lane_id) REFERENCES lanes(lane_id)
            );
            CREATE TABLE IF NOT EXISTS reviewer_threads (
                thread_id TEXT PRIMARY KEY,
                lane_id TEXT NOT NULL,
                reviewer_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                current_context_digest TEXT NOT NULL,
                current_checkpoint_id TEXT,
                rollover_count INTEGER NOT NULL,
                parent_thread_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(lane_id) REFERENCES lanes(lane_id)
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                lane_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                payload TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                actor_id TEXT,
                correlation_id TEXT NOT NULL,
                acknowledged_at TEXT,
                FOREIGN KEY(lane_id) REFERENCES lanes(lane_id)
            );
            CREATE TABLE IF NOT EXISTS connections (
                connection_id TEXT PRIMARY KEY,
                lane_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                permissions TEXT NOT NULL,
                revoked_at TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                credential_hash TEXT UNIQUE,
                FOREIGN KEY(lane_id) REFERENCES lanes(lane_id)
            );
            CREATE TABLE IF NOT EXISTS secrets (
                secret_id TEXT PRIMARY KEY,
                lane_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                purpose TEXT NOT NULL,
                encrypted_value BLOB NOT NULL,
                revoked_at TEXT,
                created_at TEXT NOT NULL,
                rotated_at TEXT,
                FOREIGN KEY(lane_id) REFERENCES lanes(lane_id)
            );
            CREATE TABLE IF NOT EXISTS transitions (
                transition_id TEXT PRIMARY KEY,
                lane_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                reason TEXT,
                FOREIGN KEY(lane_id) REFERENCES lanes(lane_id)
            );
            CREATE TABLE IF NOT EXISTS approval_requests (
                request_id TEXT PRIMARY KEY,
                lane_id TEXT NOT NULL,
                request_key TEXT NOT NULL,
                action TEXT NOT NULL,
                parameters TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                approved_by TEXT,
                approved_at TEXT,
                result TEXT,
                UNIQUE(lane_id, request_key),
                FOREIGN KEY(lane_id) REFERENCES lanes(lane_id)
            );
            CREATE INDEX IF NOT EXISTS idx_events_lane ON events(lane_id, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_checkpoints_lane ON checkpoints(lane_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_threads_lane ON reviewer_threads(lane_id, updated_at);
            """
        )
        thread_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(reviewer_threads)").fetchall()
        }
        if "parent_thread_id" not in thread_columns:
            connection.execute("ALTER TABLE reviewer_threads ADD COLUMN parent_thread_id TEXT")
        connection_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(connections)").fetchall()
        }
        if "credential_hash" not in connection_columns:
            connection.execute("ALTER TABLE connections ADD COLUMN credential_hash TEXT")
        secret_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(secrets)").fetchall()
        }
        if "owner_id" not in secret_columns:
            connection.execute(
                "ALTER TABLE secrets ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''"
            )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_connections_credential_hash "
            "ON connections(credential_hash)"
        )

    def create_lane(self, lane: Lane) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO lanes (
                    lane_id, name, project_id, workspace_path, agent_id, reviewer_id, state,
                    owner_id, provider, reviewer_provider, model, current_objective,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lane.lane_id,
                    lane.name,
                    lane.project_id,
                    str(lane.workspace_path),
                    lane.agent_id,
                    lane.reviewer_id,
                    lane.state.value,
                    lane.owner_id,
                    lane.provider,
                    lane.reviewer_provider,
                    lane.model,
                    lane.current_objective,
                    self._dt(lane.created_at),
                    self._dt(lane.updated_at),
                ),
            )

    def get_lane(self, lane_id: str) -> Lane | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM lanes WHERE lane_id = ?", (lane_id,)).fetchone()
        return self._row_to_lane(row) if row else None

    def create_session(self, session: AgentSession) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO sessions (
                    session_id, lane_id, agent_id, provider, provider_session_id, workspace_path,
                    state, process_status, writer_id, writer_lease_id, writer_lease_expires_at,
                    writer_lease_version, current_objective, current_revision, last_checkpoint_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.lane_id,
                    session.agent_id,
                    session.provider,
                    session.provider_session_id,
                    str(session.workspace_path),
                    session.state.value,
                    session.process_status,
                    session.writer_id,
                    session.writer_lease_id,
                    self._dt(session.writer_lease_expires_at),
                    session.writer_lease_version,
                    session.current_objective,
                    session.current_revision,
                    session.last_checkpoint_id,
                    self._dt(session.created_at),
                    self._dt(session.updated_at),
                ),
            )

    def get_session(self, session_id: str) -> AgentSession | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if not row:
            return None
        return AgentSession(
            session_id=row["session_id"],
            lane_id=row["lane_id"],
            agent_id=row["agent_id"],
            provider=row["provider"],
            workspace_path=Path(row["workspace_path"]),
            provider_session_id=row["provider_session_id"],
            state=LaneState(row["state"]),
            process_status=row["process_status"],
            writer_id=row["writer_id"],
            writer_lease_id=row["writer_lease_id"],
            writer_lease_expires_at=datetime.fromisoformat(row["writer_lease_expires_at"])
            if row["writer_lease_expires_at"]
            else None,
            writer_lease_version=row["writer_lease_version"] or 0,
            current_objective=row["current_objective"],
            current_revision=row["current_revision"] or 0,
            last_checkpoint_id=row["last_checkpoint_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def list_sessions_for_lane(self, lane_id: str) -> list[AgentSession]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sessions WHERE lane_id = ? ORDER BY created_at", (lane_id,)
            ).fetchall()
        sessions: list[AgentSession] = []
        for row in rows:
            session = self.get_session(row["session_id"])
            if session is not None:
                sessions.append(session)
        return sessions

    def acquire_lease(self, lease: SessionLease, now: datetime | None = None) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM leases WHERE session_id = ?",
                (lease.session_id,),
            ).fetchone()
            if current is not None and datetime.fromisoformat(current["expires_at"]) > (
                now or datetime.now(UTC)
            ):
                connection.execute("COMMIT")
                return False
            connection.execute(
                """
                INSERT OR REPLACE INTO leases
                    (session_id, lease_id, writer_id, expires_at, version)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    lease.session_id,
                    lease.lease_id,
                    lease.writer_id,
                    self._dt(lease.expires_at),
                    lease.version,
                ),
            )
            connection.execute(
                """
                UPDATE sessions
                SET writer_id = ?, writer_lease_id = ?, writer_lease_expires_at = ?,
                    writer_lease_version = ?
                WHERE session_id = ?
                """,
                (
                    lease.writer_id,
                    lease.lease_id,
                    self._dt(lease.expires_at),
                    lease.version,
                    lease.session_id,
                ),
            )
            connection.execute("COMMIT")
            return True

    def release_lease(self, session_id: str, lease_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM leases WHERE session_id = ? AND lease_id = ?",
                (session_id, lease_id),
            )
            connection.execute(
                """
                UPDATE sessions
                SET writer_id = NULL, writer_lease_id = NULL,
                    writer_lease_expires_at = NULL, writer_lease_version = 0
                WHERE session_id = ? AND writer_lease_id = ?
                """,
                (session_id, lease_id),
            )
            connection.execute("COMMIT")

    def create_checkpoint(self, checkpoint: Checkpoint) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO checkpoints (
                    checkpoint_id, lane_id, session_id, objective, git_status, changed_files,
                    build_results, evidence, revision, created_at, created_by,
                    correlation_id, transcript_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.checkpoint_id,
                    checkpoint.lane_id,
                    checkpoint.session_id,
                    checkpoint.objective,
                    checkpoint.git_status,
                    self._json(checkpoint.changed_files),
                    checkpoint.build_results,
                    self._json(checkpoint.evidence),
                    checkpoint.revision,
                    self._dt(checkpoint.created_at),
                    checkpoint.created_by,
                    checkpoint.correlation_id,
                    checkpoint.transcript_id,
                ),
            )

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
            ).fetchone()
        if not row:
            return None
        return Checkpoint(
            checkpoint_id=row["checkpoint_id"],
            lane_id=row["lane_id"],
            session_id=row["session_id"],
            objective=row["objective"],
            git_status=row["git_status"],
            changed_files=self._json_list(row["changed_files"]),
            build_results=row["build_results"],
            evidence=json.loads(row["evidence"]),
            revision=row["revision"],
            created_at=datetime.fromisoformat(row["created_at"]),
            created_by=row["created_by"],
            correlation_id=row["correlation_id"],
            transcript_id=row["transcript_id"],
        )

    def create_review(self, review: Review) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO reviews (
                    review_id, lane_id, reviewer_thread_id, checkpoint_id, status, verdict,
                    findings, requested_changes, evidence_refs, model, created_at, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review.review_id,
                    review.lane_id,
                    review.reviewer_thread_id,
                    review.checkpoint_id,
                    review.status,
                    review.verdict,
                    self._json(review.findings),
                    self._json(review.requested_changes),
                    self._json(review.evidence_refs),
                    review.model,
                    self._dt(review.created_at),
                    review.created_by,
                ),
            )

    def create_thread(self, thread: ReviewerThread) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO reviewer_threads (
                    thread_id, lane_id, reviewer_id, provider, model, current_context_digest,
                    current_checkpoint_id, rollover_count, parent_thread_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread.thread_id,
                    thread.lane_id,
                    thread.reviewer_id,
                    thread.provider,
                    thread.model,
                    thread.current_context_digest,
                    thread.current_checkpoint_id,
                    thread.rollover_count,
                    thread.parent_thread_id,
                    self._dt(thread.created_at),
                    self._dt(thread.updated_at),
                ),
            )

    def get_thread(self, thread_id: str) -> ReviewerThread | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM reviewer_threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        if not row:
            return None
        return ReviewerThread(
            thread_id=row["thread_id"],
            lane_id=row["lane_id"],
            reviewer_id=row["reviewer_id"],
            provider=row["provider"],
            model=row["model"],
            current_context_digest=row["current_context_digest"],
            current_checkpoint_id=row["current_checkpoint_id"],
            rollover_count=row["rollover_count"],
            parent_thread_id=row["parent_thread_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def list_threads_for_lane(self, lane_id: str) -> list[ReviewerThread]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reviewer_threads WHERE lane_id = ? ORDER BY created_at", (lane_id,)
            ).fetchall()
        threads: list[ReviewerThread] = []
        for row in rows:
            thread = self.get_thread(row["thread_id"])
            if thread is not None:
                threads.append(thread)
        return threads

    def create_event(self, event: EventRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO events (
                    event_id, lane_id, event_type, severity, payload, occurred_at, actor_id,
                    correlation_id, acknowledged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.lane_id,
                    event.event_type,
                    event.severity,
                    self._json(event.payload),
                    self._dt(event.occurred_at),
                    event.actor_id,
                    event.correlation_id,
                    self._dt(event.acknowledged_at),
                ),
            )

    def list_events(self, lane_id: str) -> list[EventRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE lane_id = ? ORDER BY occurred_at, event_id", (lane_id,)
            ).fetchall()
        return [
            EventRecord(
                event_id=row["event_id"],
                lane_id=row["lane_id"],
                event_type=row["event_type"],
                severity=row["severity"],
                payload=json.loads(row["payload"]),
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                actor_id=row["actor_id"],
                correlation_id=row["correlation_id"],
                acknowledged_at=datetime.fromisoformat(row["acknowledged_at"])
                if row["acknowledged_at"]
                else None,
            )
            for row in rows
        ]

    @staticmethod
    def _row_to_approval(row: sqlite3.Row) -> ApprovalRequest:
        return ApprovalRequest(
            request_id=row["request_id"],
            lane_id=row["lane_id"],
            request_key=row["request_key"],
            action=row["action"],
            parameters=json.loads(row["parameters"]),
            status=row["status"],
            requested_by=row["requested_by"],
            requested_at=datetime.fromisoformat(row["requested_at"]),
            approved_by=row["approved_by"],
            approved_at=datetime.fromisoformat(row["approved_at"]) if row["approved_at"] else None,
            result=json.loads(row["result"]) if row["result"] else None,
        )

    def create_approval_request(self, request: ApprovalRequest) -> ApprovalRequest:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO approval_requests (
                    request_id, lane_id, request_key, action, parameters, status,
                    requested_by, requested_at, approved_by, approved_at, result
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.lane_id,
                    request.request_key,
                    request.action,
                    self._json(request.parameters),
                    request.status,
                    request.requested_by,
                    self._dt(request.requested_at),
                    request.approved_by,
                    self._dt(request.approved_at),
                    self._json(request.result) if request.result is not None else None,
                ),
            )
            row = connection.execute(
                "SELECT * FROM approval_requests WHERE lane_id = ? AND request_key = ?",
                (request.lane_id, request.request_key),
            ).fetchone()
        return self._row_to_approval(row)

    def get_approval_request(self, request_id: str) -> ApprovalRequest | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM approval_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return self._row_to_approval(row) if row else None

    def find_approval_request(self, lane_id: str, request_key: str) -> ApprovalRequest | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM approval_requests WHERE lane_id = ? AND request_key = ?",
                (lane_id, request_key),
            ).fetchone()
        return self._row_to_approval(row) if row else None

    def claim_approval(self, lane_id: str, request_id: str, approver_id: str, at: datetime) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE approval_requests
                SET status = 'executing', approved_by = ?, approved_at = ?
                WHERE request_id = ? AND lane_id = ? AND status = 'pending'
                """,
                (approver_id, self._dt(at), request_id, lane_id),
            )
            connection.execute("COMMIT")
            return cursor.rowcount == 1

    def finish_approval(self, request_id: str, status: str, result: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE approval_requests SET status = ?, result = ?
                WHERE request_id = ? AND status = 'executing'
                """,
                (status, self._json(result), request_id),
            )

    def create_connection(self, connection: ConnectionRecord) -> None:
        with self._connect() as connection_db:
            connection_db.execute(
                """
                INSERT OR REPLACE INTO connections (
                    connection_id, lane_id, owner_id, permissions, revoked_at,
                    created_at, expires_at, credential_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    connection.connection_id,
                    connection.lane_id,
                    connection.owner_id,
                    self._json(sorted(connection.permissions)),
                    self._dt(connection.revoked_at),
                    self._dt(connection.created_at),
                    self._dt(connection.expires_at),
                    connection.credential_hash,
                ),
            )

    def get_connection(self, connection_id: str) -> ConnectionRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM connections WHERE connection_id = ?", (connection_id,)
            ).fetchone()
        if not row:
            return None
        return ConnectionRecord(
            connection_id=row["connection_id"],
            lane_id=row["lane_id"],
            owner_id=row["owner_id"],
            permissions=set(json.loads(row["permissions"])),
            revoked_at=datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            credential_hash=row["credential_hash"],
        )

    def get_connection_by_credential_hash(self, credential_hash: str) -> ConnectionRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM connections WHERE credential_hash = ?",
                (credential_hash,),
            ).fetchone()
        if not row:
            return None
        return self.get_connection(row["connection_id"])

    def create_secret(self, secret: SecretRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO secrets (
                    secret_id, lane_id, owner_id, purpose, encrypted_value, revoked_at,
                    created_at, rotated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    secret.secret_id,
                    secret.lane_id,
                    secret.owner_id,
                    secret.purpose,
                    secret.encrypted_value,
                    self._dt(secret.revoked_at),
                    self._dt(secret.created_at),
                    self._dt(secret.rotated_at),
                ),
            )

    def get_secret(self, secret_id: str) -> SecretRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM secrets WHERE secret_id = ?", (secret_id,)
            ).fetchone()
        if not row:
            return None
        return SecretRecord(
            secret_id=row["secret_id"],
            lane_id=row["lane_id"],
            purpose=row["purpose"],
            encrypted_value=row["encrypted_value"],
            revoked_at=datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            rotated_at=datetime.fromisoformat(row["rotated_at"]) if row["rotated_at"] else None,
            owner_id=row["owner_id"],
        )

    def list_secrets(self, lane_id: str) -> list[SecretMetadata]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT secret_id, lane_id, owner_id, purpose, created_at, rotated_at, revoked_at
                FROM secrets WHERE lane_id = ? ORDER BY created_at, secret_id
                """,
                (lane_id,),
            ).fetchall()
        return [
            SecretMetadata(
                secret_id=row["secret_id"],
                lane_id=row["lane_id"],
                owner_id=row["owner_id"],
                purpose=row["purpose"],
                created_at=datetime.fromisoformat(row["created_at"]),
                rotated_at=datetime.fromisoformat(row["rotated_at"])
                if row["rotated_at"]
                else None,
                revoked_at=datetime.fromisoformat(row["revoked_at"])
                if row["revoked_at"]
                else None,
            )
            for row in rows
        ]

    def delete_secret(self, secret_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM secrets WHERE secret_id = ?", (secret_id,))
            return cursor.rowcount > 0

    def create_transition(self, transition: LaneStateTransition) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transitions (
                    transition_id, lane_id, from_state, to_state, actor_id, correlation_id,
                    occurred_at, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("transition"),
                    transition.lane_id,
                    transition.from_state.value,
                    transition.to_state.value,
                    transition.actor_id,
                    transition.correlation_id,
                    self._dt(transition.occurred_at),
                    transition.reason,
                ),
            )

    def list_transitions(self, lane_id: str) -> list[LaneStateTransition]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM transitions WHERE lane_id = ? ORDER BY occurred_at", (lane_id,)
            ).fetchall()
        return [
            LaneStateTransition(
                lane_id=row["lane_id"],
                from_state=LaneState(row["from_state"]),
                to_state=LaneState(row["to_state"]),
                actor_id=row["actor_id"],
                correlation_id=row["correlation_id"],
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                reason=row["reason"],
            )
            for row in rows
        ]

    def list_checkpoints(self, lane_id: str) -> list[Checkpoint]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM checkpoints
                WHERE lane_id = ?
                ORDER BY created_at, revision, checkpoint_id
                """,
                (lane_id,),
            ).fetchall()
        checkpoints: list[Checkpoint] = []
        for row in rows:
            checkpoint = self.get_checkpoint(row["checkpoint_id"])
            if checkpoint is not None:
                checkpoints.append(checkpoint)
        return checkpoints

    def list_reviews(self, lane_id: str) -> list[Review]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reviews WHERE lane_id = ? ORDER BY created_at, review_id", (lane_id,)
            ).fetchall()
        return [
            Review(
                review_id=row["review_id"],
                lane_id=row["lane_id"],
                reviewer_thread_id=row["reviewer_thread_id"],
                checkpoint_id=row["checkpoint_id"],
                status=row["status"],
                verdict=row["verdict"],
                findings=json.loads(row["findings"]),
                requested_changes=json.loads(row["requested_changes"]),
                evidence_refs=json.loads(row["evidence_refs"]),
                model=row["model"],
                created_at=datetime.fromisoformat(row["created_at"]),
                created_by=row["created_by"],
            )
            for row in rows
        ]
