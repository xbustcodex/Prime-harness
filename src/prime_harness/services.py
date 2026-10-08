from __future__ import annotations

import hashlib
import secrets
import subprocess
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from shutil import which
from threading import Lock, RLock
from typing import Any

from prime_harness.agents import CodingAgentProvider, ShellAgentProvider
from prime_harness.config import AgentConfig, ReviewerConfig
from prime_harness.domain import (
    AgentCommandResult,
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
    SessionLease,
    WorkspacePolicy,
    new_id,
    now_utc,
)
from prime_harness.omp import OmpAgentProvider
from prime_harness.reviewers import DeterministicReviewerProvider, ReviewerProvider
from prime_harness.secret_service import EnvironmentSecretKeyProvider, SecretService
from prime_harness.stores import InMemoryStore, Store


class HarnessService:
    def __init__(
        self,
        store: Store | None = None,
        workspace_root: Path | Path = Path.cwd(),
        now=None,
        default_agent_provider: str = "shell",
        omp_executable: str = "omp",
    ) -> None:
        self.store = store or InMemoryStore()
        self.workspace_root = Path(workspace_root).resolve()
        self._now = now or now_utc
        self._agent_providers: dict[str, CodingAgentProvider] = {}
        self._reviewer_providers: dict[str, ReviewerProvider] = {}
        if default_agent_provider not in {"shell", "omp"}:
            raise ValueError("Unsupported default coding-agent provider")
        self.default_agent_provider = default_agent_provider
        self.omp_executable = omp_executable
        self._lane_lock_guard = Lock()
        self._lane_locks: dict[str, RLock] = {}
        self.secret_service = SecretService(
            self.store,
            EnvironmentSecretKeyProvider(),
            self._event,
            now=self._now,
        )

    def _lane_lock(self, lane_id: str) -> RLock:
        with self._lane_lock_guard:
            return self._lane_locks.setdefault(lane_id, RLock())

    @staticmethod
    def _id(prefix: str) -> str:
        return new_id(prefix)

    def _event(
        self,
        lane_id: str,
        event_type: str,
        severity: str,
        payload: dict[str, Any],
        actor_id: str | None,
        correlation_id: str,
    ) -> EventRecord:
        event = EventRecord(
            event_id=self._id("event"),
            lane_id=lane_id,
            event_type=event_type,
            severity=severity,
            payload=payload,
            occurred_at=self._now(),
            actor_id=actor_id,
            correlation_id=correlation_id,
        )
        self.store.create_event(event)
        return event

    def _transition(
        self,
        lane_id: str,
        target: LaneState,
        actor_id: str,
        correlation_id: str,
        reason: str | None = None,
    ) -> None:
        lane = self.store.get_lane(lane_id)
        if lane is None:
            return
        if lane.state == target:
            return
        if not lane.state.can_transition(target):
            raise ValueError(f"Illegal transition {lane.state.value}->{target.value}")
        former = lane.state
        lane.state = target
        lane.updated_at = self._now()
        self.store.create_lane(lane)
        transition = LaneStateTransition(
            lane_id, former, target, actor_id, correlation_id, self._now(), reason
        )
        self.store.create_transition(transition)
        self._event(
            lane_id,
            "lane_state_changed",
            "info",
            {"from": former.value, "to": target.value, "reason": reason},
            actor_id,
            correlation_id,
        )

    def create_lane(self, name: str, project_id: str, workspace_path: Path) -> Lane:
        requested = (
            workspace_path if workspace_path.is_absolute() else self.workspace_root / workspace_path
        )
        resolved = WorkspacePolicy(self.workspace_root).resolve(requested)
        if not resolved.exists():
            resolved.mkdir(parents=True, exist_ok=True)
        lane = Lane(
            lane_id=self._id("lane"),
            name=name,
            project_id=project_id,
            workspace_path=resolved,
            agent_id=f"agent-{name.lower().replace(' ', '-')}-1",
            reviewer_id=f"reviewer-{name.lower().replace(' ', '-')}-1",
            provider=self.default_agent_provider,
        )
        self.store.create_lane(lane)
        self._event(
            lane.lane_id,
            "lane_created",
            "info",
            {"name": name, "project_id": project_id},
            None,
            lane.lane_id,
        )
        return lane

    def create_connection(self, lane_id: str, owner_id: str, permissions: set[str]) -> str:
        if self.store.get_lane(lane_id) is None:
            raise KeyError("Unknown lane")
        connection_id = self._id("connection")
        connection = ConnectionRecord(connection_id, lane_id, owner_id, permissions)
        self.store.create_connection(connection)
        self._event(
            lane_id,
            "connection_created",
            "info",
            {"permissions": sorted(permissions)},
            owner_id,
            connection_id,
        )
        return connection_id

    def issue_api_credential(self, connection_id: str) -> str:
        connection = self.store.get_connection(connection_id)
        if (
            connection is None
            or connection.revoked_at is not None
            or (connection.expires_at is not None and connection.expires_at <= self._now())
        ):
            raise KeyError("Unknown active connection")
        credential = secrets.token_urlsafe(32)
        connection.credential_hash = hashlib.sha256(credential.encode("utf-8")).hexdigest()
        self.store.create_connection(connection)
        self._event(
            connection.lane_id,
            "api_credential_issued",
            "security",
            {"connection_id": connection_id},
            connection.owner_id,
            connection_id,
        )
        return credential

    def authenticate_credential(
        self,
        credential: str,
        action: str,
        lane_id: str,
    ) -> str | None:
        credential_hash = hashlib.sha256(credential.encode("utf-8")).hexdigest()
        connection = self.store.get_connection_by_credential_hash(credential_hash)
        if connection is None or not self.can_execute(connection.connection_id, action, lane_id):
            return None
        return connection.connection_id

    def revoke_connection(self, connection_id: str) -> bool:
        connection = self.store.get_connection(connection_id)
        if connection is None or connection.revoked_at is not None:
            return False
        connection.revoked_at = self._now()
        self.store.create_connection(connection)
        self._event(
            connection.lane_id,
            "connection_revoked",
            "security",
            {"connection_id": connection_id},
            connection.owner_id,
            connection_id,
        )
        return True

    def can_execute(self, connection_id: str, action: str, lane_id: str) -> bool:
        connection = self.store.get_connection(connection_id)
        if connection is None or connection.revoked_at is not None or connection.lane_id != lane_id:
            return False
        if connection.expires_at is not None and connection.expires_at <= self._now():
            return False
        return action in connection.permissions

    def create_session(self, lane_id: str, agent_id: str) -> AgentSession:
        lane = self.store.get_lane(lane_id)
        if lane is None:
            raise KeyError("Unknown lane")
        session = AgentSession(
            session_id=self._id("session"),
            lane_id=lane_id,
            agent_id=agent_id,
            provider=lane.provider,
            workspace_path=lane.workspace_path,
        )
        self.store.create_session(session)
        self._transition(
            lane_id, LaneState.IDLE, "system", session.session_id, "Session initialized"
        )
        return session

    def start_agent_session(self, lane_id: str) -> AgentSession:
        lane = self.store.get_lane(lane_id)
        if lane is None:
            raise KeyError("Unknown lane")
        provider = self._agent_provider(lane.agent_id)
        session = provider.start_session()
        session.lane_id = lane_id
        session.workspace_path = lane.workspace_path
        session.provider_session_id = session.provider_session_id or session.session_id
        session.state = LaneState.IDLE
        self.store.create_session(session)
        return session

    def restore_agent_session(self, lane_id: str, session_id: str) -> AgentSession:
        session = self.store.get_session(session_id)
        if session is None or session.lane_id != lane_id:
            raise KeyError("Unknown session")
        provider = self._agent_provider(session.agent_id)
        restore = getattr(provider, "restore_session", None)
        if restore is None:
            raise RuntimeError("Provider does not support restoring persisted sessions")
        restored = restore(session)
        self.store.create_session(restored)
        return restored

    def acquire_writer_lease(self, session_id: str, writer_id: str, ttl_seconds: int = 300) -> bool:
        session = self.store.get_session(session_id)
        if session is None or ttl_seconds <= 0:
            return False
        now = self._now()
        old_writer = session.writer_id
        old_expiry = session.writer_lease_expires_at
        if old_writer is not None and old_expiry is not None and old_expiry <= now:
            self._event(
                session.lane_id,
                "writer_lease_expired",
                "warning",
                {"session_id": session_id, "writer_id": old_writer},
                "system",
                session_id,
            )
        lease = SessionLease(
            session_id,
            self._id("lease"),
            writer_id,
            now + timedelta(seconds=ttl_seconds),
            session.writer_lease_version + 1,
        )
        acquired = self.store.acquire_lease(lease, now)
        if acquired:
            current = self.store.get_session(session_id)
            if current is None:
                raise RuntimeError("Session disappeared after lease acquisition")
            current.writer_id = writer_id
            current.writer_lease_id = lease.lease_id
            current.writer_lease_expires_at = lease.expires_at
            current.writer_lease_version = lease.version
            current.updated_at = self._now()
            self.store.create_session(current)
            self._event(
                current.lane_id,
                "writer_lease_acquired",
                "info",
                {"session_id": session_id, "writer_id": writer_id, "version": lease.version},
                writer_id,
                session_id,
            )
        return acquired

    def release_writer_lease(self, session_id: str, lease_id: str) -> None:
        session = self.store.get_session(session_id)
        if session is None or session.writer_lease_id != lease_id:
            return
        released_writer = session.writer_id
        if (
            session.writer_lease_expires_at is not None
            and session.writer_lease_expires_at <= self._now()
        ):
            self._event(
                session.lane_id,
                "writer_lease_expired",
                "warning",
                {"session_id": session_id, "writer_id": released_writer},
                "system",
                session_id,
            )
        self.store.release_lease(session_id, lease_id)
        session.writer_id = None
        session.writer_lease_id = None
        session.writer_lease_expires_at = None
        session.updated_at = self._now()
        self.store.create_session(session)
        self._event(
            session.lane_id,
            "writer_lease_released",
            "info",
            {"session_id": session_id, "lease_id": lease_id},
            released_writer,
            session_id,
        )

    def get_session(self, session_id: str) -> AgentSession | None:
        return self.store.get_session(session_id)

    def create_checkpoint(
        self,
        lane_id: str,
        objective: str,
        git_status: str,
        changed_files: list[str],
        build_results: str,
        evidence: dict[str, Any],
        session_id: str | None = None,
        created_by: str = "system",
    ) -> Checkpoint:
        with self._lane_lock(lane_id):
            return self._create_checkpoint(
                lane_id,
                objective,
                git_status,
                changed_files,
                build_results,
                evidence,
                session_id,
                created_by,
            )

    def _create_checkpoint(
        self,
        lane_id: str,
        objective: str,
        git_status: str,
        changed_files: list[str],
        build_results: str,
        evidence: dict[str, Any],
        session_id: str | None,
        created_by: str,
    ) -> Checkpoint:
        lane = self.store.get_lane(lane_id)
        if lane is None:
            raise KeyError("Unknown lane")
        session = self.store.get_session(session_id) if session_id else None
        if session_id is not None and (session is None or session.lane_id != lane_id):
            raise KeyError("Unknown session")
        if session is None:
            sessions = self.store.list_sessions_for_lane(lane_id)
            session = sessions[-1] if sessions else self.create_session(lane_id, lane.agent_id)
        if session.lane_id != lane_id:
            raise PermissionError("Session belongs to another lane")
        checkpoint = Checkpoint(
            checkpoint_id=self._id("checkpoint"),
            lane_id=lane_id,
            session_id=session.session_id,
            objective=objective,
            git_status=git_status,
            changed_files=changed_files,
            build_results=build_results,
            evidence=evidence,
            revision=(session.current_revision + 1),
            created_at=self._now(),
            created_by=created_by,
            correlation_id=self._id("correlation"),
            transcript_id=f"transcript:{session.session_id}",
        )
        self.store.create_checkpoint(checkpoint)
        session.current_revision = checkpoint.revision
        session.last_checkpoint_id = checkpoint.checkpoint_id
        self.store.create_session(session)
        self._event(
            lane_id,
            "checkpoint_created",
            "info",
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "revision": checkpoint.revision,
                "changed_files": changed_files,
            },
            created_by,
            checkpoint.correlation_id,
        )
        current_lane = self.store.get_lane(lane_id)
        if current_lane is None:
            raise RuntimeError("Lane disappeared while checkpoint was being persisted")
        if current_lane.state in {LaneState.IDLE, LaneState.CONTINUING}:
            self._transition(lane_id, LaneState.WORKING, created_by, checkpoint.correlation_id)
        current_lane = self.store.get_lane(lane_id)
        if current_lane is not None and current_lane.state is LaneState.WORKING:
            self._transition(lane_id, LaneState.CHECKPOINT, created_by, checkpoint.correlation_id)
        current_lane = self.store.get_lane(lane_id)
        if current_lane is not None and current_lane.state is LaneState.CHECKPOINT:
            self._transition(
                lane_id, LaneState.WAITING_FOR_REVIEW, created_by, checkpoint.correlation_id
            )
        return checkpoint

    def _sessions_for_lane(self, lane_id: str) -> list[str]:
        sessions = self.store.list_sessions_for_lane(lane_id)
        return [session.session_id for session in sessions]

    def get_checkpoint(self, lane_id: str, checkpoint_id: str) -> Checkpoint | None:
        checkpoint = self.store.get_checkpoint(checkpoint_id)
        return checkpoint if checkpoint and checkpoint.lane_id == lane_id else None

    def get_review_context(self, lane_id: str, checkpoint_id: str) -> dict[str, Any] | None:
        checkpoint = self.get_checkpoint(lane_id, checkpoint_id)
        if checkpoint is None:
            return None
        return {
            "objective": checkpoint.objective,
            "git_status": checkpoint.git_status,
            "changed_files": checkpoint.changed_files,
            "build_results": checkpoint.build_results,
            "evidence": checkpoint.evidence,
        }

    def get_transcript(self, lane_id: str, checkpoint_id: str) -> dict[str, Any] | None:
        checkpoint = self.get_checkpoint(lane_id, checkpoint_id)
        if checkpoint is None:
            return None
        lane = self.store.get_lane(lane_id)
        return {
            "checkpoint_id": checkpoint.checkpoint_id,
            "lane_id": lane_id,
            "project_id": lane.project_id if lane else None,
            "transcript_id": checkpoint.transcript_id,
            "transcript": checkpoint.evidence.get(
                "transcript",
                f"Operational transcript for {checkpoint.objective}",
            ),
            "evidence_refs": checkpoint.evidence.get("evidence_refs", ()),
        }

    def create_review_thread(
        self, lane_id: str, reviewer_id: str, provider: ReviewerProvider
    ) -> ReviewerThread:
        thread = replace(provider.create_thread(lane_id), reviewer_id=reviewer_id)
        self.store.create_thread(thread)
        return thread

    def create_review_thread_authenticated(
        self, connection_id: str, lane_id: str
    ) -> ReviewerThread:
        self._require_permission(connection_id, "review", lane_id)
        lane = self.store.get_lane(lane_id)
        if lane is None:
            raise KeyError("Unknown lane")
        return self.create_review_thread(
            lane_id,
            lane.reviewer_id,
            self._reviewer_provider_for_lane(lane_id),
        )

    def submit_review(
        self,
        lane_id: str,
        thread_id: str,
        checkpoint_id: str,
        reviewer_id: str,
        provider: ReviewerProvider,
    ) -> Review:
        checkpoint = self.get_checkpoint(lane_id, checkpoint_id)
        thread = self.store.get_thread(thread_id)
        if (
            checkpoint is None
            or thread is None
            or thread.lane_id != lane_id
            or thread.reviewer_id != reviewer_id
        ):
            raise KeyError("Unknown reviewer context")
        review = provider.review(checkpoint, thread)
        self.store.create_review(review)
        self.store.create_thread(
            replace(thread, current_checkpoint_id=checkpoint_id, updated_at=self._now())
        )
        self._event(
            lane_id,
            "review_submitted",
            "info",
            {"review_id": review.review_id, "status": review.status},
            reviewer_id,
            review.review_id,
        )
        lane = self.store.get_lane(lane_id)
        if lane is not None:
            if lane.state is LaneState.WAITING_FOR_REVIEW:
                self._transition(lane_id, LaneState.REVIEWING, reviewer_id, review.review_id)
            if review.status in {"accepted", "pass"}:
                self._transition(lane_id, LaneState.COMPLETED, reviewer_id, review.review_id)
            elif review.status in {"rejected", "needs_correction"}:
                self._transition(lane_id, LaneState.CONTINUING, reviewer_id, review.review_id)
        return review

    def rollover_review_thread(self, lane_id: str, checkpoint_id: str) -> ReviewerThread | None:
        checkpoint = self.get_checkpoint(lane_id, checkpoint_id)
        if checkpoint is None:
            return None
        threads = self.store.list_threads_for_lane(lane_id)
        successor = next(
            (
                item
                for item in threads
                if item.parent_thread_id and item.current_checkpoint_id == checkpoint_id
            ),
            None,
        )
        if successor is not None:
            return successor
        provider = self._reviewer_provider_for_lane(lane_id)
        source = next(
            (item for item in reversed(threads) if item.current_checkpoint_id == checkpoint_id),
            threads[-1] if threads else None,
        )
        if source is None:
            source = provider.create_thread(lane_id)
            self.store.create_thread(source)
        rolled = provider.rollover(checkpoint, source)
        self.store.create_thread(rolled)
        self._event(
            lane_id,
            "review_thread_rolled",
            "info",
            {
                "checkpoint_id": checkpoint_id,
                "thread_id": rolled.thread_id,
                "parent_thread_id": source.thread_id,
                "transcript_id": checkpoint.transcript_id,
            },
            "system",
            checkpoint.correlation_id,
        )
        return rolled

    def _reviewer_provider_for_lane(self, lane_id: str) -> ReviewerProvider:
        lane = self.store.get_lane(lane_id)
        if lane is None:
            raise KeyError("Unknown lane")
        if lane.reviewer_id not in self._reviewer_providers:
            config = ReviewerConfig(lane.reviewer_id, lane.reviewer_provider, lane.model)
            self._reviewer_providers[lane.reviewer_id] = DeterministicReviewerProvider(config)
        return self._reviewer_providers[lane.reviewer_id]

    def register_agent_provider(self, lane_id: str, provider: CodingAgentProvider) -> None:
        lane = self.store.get_lane(lane_id)
        if lane is None:
            raise KeyError("Unknown lane")
        self._agent_providers[lane.agent_id] = provider

    def get_agent_state(self, lane_id: str) -> dict[str, Any] | None:
        lane = self.store.get_lane(lane_id)
        if lane is None:
            return None
        session = self.store.get_session(next(iter(self._sessions_for_lane(lane_id)), ""))
        if session is None:
            return {"lane_id": lane_id, "state": lane.state.value, "session_id": None}
        return {
            "lane_id": lane_id,
            "state": session.state.value,
            "session_id": session.session_id,
            "objective": session.current_objective,
        }

    def get_git_status(self, lane_id: str, session_id: str) -> dict[str, Any]:
        return self._run_git(lane_id, session_id, ["status", "--short", "--branch"])

    def get_diff(self, lane_id: str, session_id: str) -> str:
        result = self._run_git(lane_id, session_id, ["diff", "--stat"])
        if result["status"] != "ok":
            raise RuntimeError(f"Git diff failed: {result['stderr']}")
        return str(result["stdout"])

    def get_test_results(self, lane_id: str, session_id: str) -> dict[str, Any]:
        return {
            "status": "not-run",
            "command": "pytest",
            "lane_id": lane_id,
            "session_id": session_id,
        }

    def read_project_file(
        self, lane_id: str, relative_path: str, max_bytes: int = 64 * 1024
    ) -> str:
        lane = self.store.get_lane(lane_id)
        if lane is None:
            raise KeyError("Unknown lane")
        policy = WorkspacePolicy(lane.workspace_path)
        path = policy.resolve_relative(relative_path)
        if not path.is_file():
            raise ValueError("Requested workspace path is not a regular file")
        if path.stat().st_size > max_bytes:
            raise ValueError("File exceeds maximum size")
        return path.read_text(encoding="utf-8")

    def _run_git(self, lane_id: str, session_id: str, args: list[str]) -> dict[str, Any]:
        lane = self.store.get_lane(lane_id)
        if lane is None:
            raise KeyError("Unknown lane")
        session = self.store.get_session(session_id)
        if session is None or session.lane_id != lane_id:
            raise KeyError("Unknown session")
        if tuple(args) not in {
            ("status", "--short", "--branch"),
            ("diff", "--stat"),
        }:
            raise ValueError("Unsupported Git operation")
        git_executable = which("git")
        if git_executable is None:
            raise FileNotFoundError("Git executable is not available")
        result = subprocess.run(  # noqa: S603 - argv uses a fixed operation template.
            [git_executable, *args],
            cwd=lane.workspace_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        if result.returncode != 0:
            return {"status": "failed", "stdout": result.stdout, "stderr": result.stderr}
        return {"status": "ok", "stdout": result.stdout.strip(), "stderr": ""}

    def _require_permission(self, connection_id: str, action: str, lane_id: str) -> None:
        if not self.can_execute(connection_id, action, lane_id):
            raise PermissionError(f"Connection lacks active {action} permission for this lane")

    def read_file_authenticated(self, connection_id: str, lane_id: str, relative_path: str) -> str:
        self._require_permission(connection_id, "read", lane_id)
        return self.read_project_file(lane_id, relative_path)

    def read_checkpoint_authenticated(
        self, connection_id: str, lane_id: str, checkpoint_id: str
    ) -> Checkpoint:
        self._require_permission(connection_id, "read", lane_id)
        checkpoint = self.get_checkpoint(lane_id, checkpoint_id)
        if checkpoint is None:
            raise KeyError("Unknown checkpoint")
        return checkpoint

    def read_session_authenticated(
        self,
        connection_id: str,
        lane_id: str,
        session_id: str,
    ) -> AgentSession:
        self._require_permission(connection_id, "read", lane_id)
        session = self.store.get_session(session_id)
        if session is None or session.lane_id != lane_id:
            raise KeyError("Unknown session")
        return session

    def read_transcript_authenticated(
        self, connection_id: str, lane_id: str, checkpoint_id: str
    ) -> dict[str, Any]:
        self._require_permission(connection_id, "read", lane_id)
        transcript = self.get_transcript(lane_id, checkpoint_id)
        if transcript is None:
            raise KeyError("Unknown transcript")
        return transcript

    def read_events_authenticated(self, connection_id: str, lane_id: str) -> list[EventRecord]:
        self._require_permission(connection_id, "read", lane_id)
        return self.get_events(lane_id)

    def read_reviews_authenticated(self, connection_id: str, lane_id: str) -> list[Review]:
        self._require_permission(connection_id, "read", lane_id)
        return self.store.list_reviews(lane_id)

    def read_reviewer_thread_authenticated(
        self,
        connection_id: str,
        lane_id: str,
        thread_id: str,
    ) -> list[Review]:
        self._require_permission(connection_id, "review", lane_id)
        thread = self.store.get_thread(thread_id)
        if thread is None or thread.lane_id != lane_id:
            raise KeyError("Unknown reviewer thread")
        return [
            review
            for review in self.store.list_reviews(lane_id)
            if review.reviewer_thread_id == thread_id
        ]

    def review_checkpoint_authenticated(
        self,
        connection_id: str,
        lane_id: str,
        thread_id: str,
        checkpoint_id: str,
    ) -> Review:
        self._require_permission(connection_id, "review", lane_id)
        lane = self.store.get_lane(lane_id)
        if lane is None:
            raise KeyError("Unknown lane")
        return self.submit_review(
            lane_id,
            thread_id,
            checkpoint_id,
            lane.reviewer_id,
            self._reviewer_provider_for_lane(lane_id),
        )

    def send_instruction(
        self, lane_id: str, session_id: str, connection_id: str, instruction: str
    ) -> AgentCommandResult:
        self._require_permission(connection_id, "send_instruction", lane_id)
        session = self.store.get_session(session_id)
        if session is None or session.lane_id != lane_id:
            raise KeyError("Unknown session")
        provider = self._agent_provider(session.agent_id)
        if not self.acquire_writer_lease(
            session_id, connection_id, self._provider_lease_ttl(provider)
        ):
            raise PermissionError("Session is owned by another writer")
        session = self.store.get_session(session_id)
        if session is None:
            raise RuntimeError("Session disappeared after lease acquisition")
        lease_id = session.writer_lease_id
        try:
            result = provider.send_instruction(session, instruction)
            session.state = (
                LaneState.WORKING
                if result.success
                else LaneState.HUMAN_REQUIRED
                if result.status == "approval_required"
                else LaneState.IDLE
                if result.status == "aborted"
                else LaneState.FAILED
            )
            if result.success:
                session.current_objective = instruction
            session.updated_at = self._now()
            self.store.create_session(session)
            if result.success:
                lane = self.store.get_lane(lane_id)
                if lane is not None:
                    lane.current_objective = instruction
                    self.store.create_lane(lane)
                self._transition(lane_id, LaneState.WORKING, connection_id, session_id)
            else:
                target_state = (
                    LaneState.HUMAN_REQUIRED
                    if result.status == "approval_required"
                    else LaneState.PAUSED
                    if result.status == "aborted"
                    else LaneState.FAILED
                )
                self._transition(
                    lane_id,
                    target_state,
                    connection_id,
                    session_id,
                    "OMP operation requires human approval"
                    if result.status == "approval_required"
                    else "OMP operation cancelled"
                    if result.status == "aborted"
                    else self._safe_diagnostic(result.error),
                )
            self._event(
                lane_id,
                "agent_instruction_sent",
                "info"
                if result.success
                else "warning"
                if result.status == "approval_required"
                else "error",
                {
                    "session_id": session_id,
                    "exit_code": result.exit_code,
                    "status": "succeeded" if result.success else result.status,
                    "diagnostic": self._safe_diagnostic(result.error)
                    if not result.success
                    else None,
                },
                connection_id,
                session_id,
            )
            if result.success:
                self.create_checkpoint(
                    lane_id=lane_id,
                    objective=instruction,
                    git_status="not-collected",
                    changed_files=result.changed_files,
                    build_results="not-run",
                    evidence={
                        "action_status": "succeeded",
                        **result.evidence,
                        "evidence_refs": [
                            f"agent-session:{session_id}",
                            f"provider-session:{session.provider_session_id or session.session_id}",
                            *(f"file:{path}" for path in result.changed_files),
                        ],
                        "transcript": (
                            "Agent operation succeeded; "
                            "build and test evidence has not been collected."
                        ),
                    },
                    session_id=session_id,
                    created_by=connection_id,
                )
            return result
        finally:
            if lease_id:
                self.release_writer_lease(session_id, lease_id)

    def continue_agent(
        self, lane_id: str, session_id: str, connection_id: str, instruction: str
    ) -> AgentCommandResult:
        self._require_permission(connection_id, "continue", lane_id)
        session = self.store.get_session(session_id)
        if session is None or session.lane_id != lane_id:
            raise KeyError("Unknown session")
        provider = self._agent_provider(session.agent_id)
        if not self.acquire_writer_lease(
            session_id, connection_id, self._provider_lease_ttl(provider)
        ):
            raise PermissionError("Session is owned by another writer")
        session = self.store.get_session(session_id)
        if session is None:
            raise RuntimeError("Session disappeared after lease acquisition")
        lease_id = session.writer_lease_id
        try:
            result = provider.send_instruction(session, instruction)
            session.state = (
                LaneState.WORKING
                if result.success
                else LaneState.HUMAN_REQUIRED
                if result.status == "approval_required"
                else LaneState.IDLE
                if result.status == "aborted"
                else LaneState.FAILED
            )
            session.updated_at = self._now()
            self.store.create_session(session)
            lane = self.store.get_lane(lane_id)
            if lane is not None:
                if lane.state in {LaneState.COMPLETED, LaneState.FAILED}:
                    self._transition(
                        lane_id,
                        LaneState.IDLE,
                        connection_id,
                        session_id,
                        "Starting a persisted session",
                    )
                self._transition(
                    lane_id,
                    LaneState.WORKING
                    if result.success
                    else LaneState.HUMAN_REQUIRED
                    if result.status == "approval_required"
                    else LaneState.PAUSED
                    if result.status == "aborted"
                    else LaneState.FAILED,
                    connection_id,
                    session_id,
                    None
                    if result.success
                    else "OMP operation requires human approval"
                    if result.status == "approval_required"
                    else "OMP operation cancelled"
                    if result.status == "aborted"
                    else self._safe_diagnostic(result.error),
                )
            self._event(
                lane_id,
                "agent_continued",
                "info"
                if result.success
                else "warning"
                if result.status == "approval_required"
                else "error",
                {
                    "session_id": session_id,
                    "status": "succeeded" if result.success else result.status,
                    "exit_code": result.exit_code,
                    "diagnostic": self._safe_diagnostic(result.error)
                    if not result.success
                    else None,
                },
                connection_id,
                session_id,
            )
            if result.success:
                self.create_checkpoint(
                    lane_id=lane_id,
                    objective=instruction,
                    git_status="not-collected",
                    changed_files=result.changed_files,
                    build_results="not-run",
                    evidence={
                        "action_status": "succeeded",
                        **result.evidence,
                        "evidence_refs": [
                            f"agent-session:{session_id}",
                            f"provider-session:{session.provider_session_id or session.session_id}",
                            *(f"file:{path}" for path in result.changed_files),
                        ],
                        "transcript": (
                            "Agent continuation succeeded; "
                            "build and test evidence has not been collected."
                        ),
                    },
                    session_id=session_id,
                    created_by=connection_id,
                )
            return result
        finally:
            if lease_id:
                self.release_writer_lease(session_id, lease_id)

    @staticmethod
    def _safe_diagnostic(message: str | None) -> str | None:
        if not message:
            return None
        import re

        bounded = message[:500]
        bounded = re.sub(
            r"(?i)(token|secret|password|api[_-]?key)(\s*[:=]\s*)[^\s,;]+",
            r"\1\2[REDACTED]",
            bounded,
        )
        return bounded

    def stop_agent(self, lane_id: str, session_id: str, connection_id: str) -> bool:
        self._require_permission(connection_id, "stop", lane_id)
        session = self.store.get_session(session_id)
        if session is None or session.lane_id != lane_id:
            raise KeyError("Unknown session")
        existing_lease_owned = (
            session.writer_id == connection_id
            and session.writer_lease_expires_at is not None
            and session.writer_lease_expires_at > self._now()
        )
        acquired_lease = False
        if not existing_lease_owned:
            if not self.acquire_writer_lease(session_id, connection_id):
                raise PermissionError("Session is owned by another writer")
            acquired_lease = True
        session = self.store.get_session(session_id)
        lease_id = session.writer_lease_id if session else None
        stopped = False
        try:
            if session is None:
                raise RuntimeError("Session disappeared after lease acquisition")
            provider = self._agent_provider(session.agent_id)
            stopped = provider.stop_session(session)
            if not stopped:
                self._event(
                    lane_id,
                    "agent_stop_failed",
                    "error",
                    {"session_id": session_id, "status": "failed"},
                    connection_id,
                    session_id,
                )
                return False
            session.state = LaneState.IDLE
            self.store.create_session(session)
            self._transition(
                lane_id, LaneState.IDLE, connection_id, session_id, "Agent stopped"
            )
            stopped = True
            self._event(
                lane_id,
                "agent_stopped",
                "info",
                {"session_id": session_id, "status": "succeeded"},
                connection_id,
                session_id,
            )
            return True
        finally:
            if lease_id and (acquired_lease or stopped):
                self.release_writer_lease(session_id, lease_id)

    def _agent_provider(self, agent_id: str) -> CodingAgentProvider:
        if agent_id not in self._agent_providers:
            raise KeyError("No coding-agent provider registered")
        return self._agent_providers[agent_id]

    def request_human(
        self, lane_id: str, connection_id: str, reason: str, requested_action: str
    ) -> dict[str, Any]:
        self._require_permission(connection_id, "request_human", lane_id)
        request_id = self._id("human-request")
        self._transition(lane_id, LaneState.HUMAN_REQUIRED, connection_id, request_id, reason)
        self._event(
            lane_id,
            "human_required",
            "warning",
            {"request_id": request_id, "reason": reason, "action": requested_action},
            connection_id,
            request_id,
        )
        return {"request_id": request_id, "status": "pending", "reason": reason}

    def request_dangerous_action(
        self,
        lane_id: str,
        connection_id: str,
        request_key: str,
        action: str,
        parameters: dict[str, Any],
    ) -> dict[str, str]:
        self._require_permission(connection_id, "dangerous_action", lane_id)
        if action != "create_file":
            raise ValueError("Unsupported dangerous action")
        lane = self.store.get_lane(lane_id)
        if lane is None:
            raise KeyError("Unknown lane")
        path = WorkspacePolicy(lane.workspace_path).resolve_relative(
            str(parameters.get("path", ""))
        )
        content = parameters.get("content")
        if not isinstance(content, str) or len(content.encode("utf-8")) > 64 * 1024:
            raise ValueError("Dangerous action content must be text no larger than 64 KiB")
        existing = self.store.find_approval_request(lane_id, request_key)
        if existing is not None:
            return {
                "request_id": existing.request_id,
                "status": existing.status,
                "action": existing.action,
            }
        stored = self.store.create_approval_request(
            ApprovalRequest(
                request_id=self._id("approval"),
                lane_id=lane_id,
                request_key=request_key,
                action=action,
                parameters={"path": str(path.relative_to(lane.workspace_path)), "content": content},
                status="pending",
                requested_by=connection_id,
                requested_at=self._now(),
            )
        )
        self._event(
            lane_id,
            "dangerous_action_pending",
            "warning",
            {"request_id": stored.request_id, "action": stored.action, "status": stored.status},
            connection_id,
            stored.request_id,
        )
        return {"request_id": stored.request_id, "status": stored.status, "action": stored.action}

    def approve_dangerous_action(
        self, lane_id: str, connection_id: str, request_id: str, approver_id: str
    ) -> bool:
        if not self.can_execute(connection_id, "dangerous_action", lane_id):
            return False
        request = self.store.get_approval_request(request_id)
        if request is None or request.lane_id != lane_id:
            return False
        if not self.store.claim_approval(lane_id, request_id, approver_id, self._now()):
            return False
        claimed = self.store.get_approval_request(request_id)
        if claimed is None:
            raise RuntimeError("Approval request disappeared after claim")
        lane = self.store.get_lane(lane_id)
        if lane is None:
            self.store.finish_approval(
                request_id, "failed", {"diagnostic": "Lane no longer exists"}
            )
            return False
        try:
            if claimed.action != "create_file":
                raise ValueError("Unsupported dangerous action")
            path = WorkspacePolicy(lane.workspace_path).resolve_relative(
                str(claimed.parameters["path"])
            )
            content = str(claimed.parameters["content"])
            import os

            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            result = {"status": "succeeded", "path": str(path.relative_to(lane.workspace_path))}
            self.store.finish_approval(request_id, "executed", result)
            self._event(
                lane_id,
                "dangerous_action_executed",
                "info",
                {"request_id": request_id, "status": "succeeded"},
                approver_id,
                request_id,
            )
            return True
        except (OSError, ValueError, KeyError) as exc:
            diagnostic = self._safe_diagnostic(str(exc)) or "Operation failed"
            self.store.finish_approval(
                request_id, "failed", {"status": "failed", "diagnostic": diagnostic}
            )
            self._event(
                lane_id,
                "dangerous_action_failed",
                "error",
                {"request_id": request_id, "status": "failed", "diagnostic": diagnostic},
                approver_id,
                request_id,
            )
            return False

    def create_agent_provider(
        self,
        lane_id: str,
        workspace_path: Path,
        command: str = "bash",
        provider_name: str | None = None,
    ) -> CodingAgentProvider:
        lane = self.store.get_lane(lane_id)
        if lane is None:
            raise KeyError("Unknown lane")
        policy = WorkspacePolicy(self.workspace_root)
        resolved = policy.resolve(workspace_path)
        selected_provider = provider_name or lane.provider
        if selected_provider == "shell":
            provider: CodingAgentProvider = ShellAgentProvider(
                AgentConfig(lane.agent_id, "shell", command, resolved, 120)
            )
        elif selected_provider == "omp":
            provider = OmpAgentProvider(
                agent_id=lane.agent_id,
                workspace_path=resolved,
                executable=self.omp_executable,
            )
        else:
            raise ValueError("Unsupported coding-agent provider")
        self.register_agent_provider(lane_id, provider)
        return provider

    @staticmethod
    def _provider_lease_ttl(provider: CodingAgentProvider) -> int:
        timeout = getattr(provider, "timeout_seconds", 300)
        return max(300, int(timeout) + 30)

    def create_temporary_lane(self, name: str, project_id: str) -> Lane:
        return self.create_lane(name, project_id, self.workspace_root / project_id)

    def get_events(self, lane_id: str) -> list[EventRecord]:
        return self.store.list_events(lane_id)

    def get_state(self, lane_id: str) -> dict[str, Any]:
        lane = self.store.get_lane(lane_id)
        if lane is None:
            raise KeyError("Unknown lane")
        return {
            "lane_id": lane.lane_id,
            "state": lane.state.value,
            "objective": lane.current_objective,
            "project_id": lane.project_id,
        }

    def get_history(self, lane_id: str) -> dict[str, Any]:
        return {
            "checkpoints": [item.checkpoint_id for item in self.store.list_checkpoints(lane_id)],
            "reviews": [item.review_id for item in self.store.list_reviews(lane_id)],
            "events": [item.event_id for item in self.store.list_events(lane_id)],
        }

    def compact_history(self, lane_id: str) -> dict[str, Any]:
        checkpoints = self.store.list_checkpoints(lane_id)
        return {
            "lane_id": lane_id,
            "preserved_checkpoints": [item.checkpoint_id for item in checkpoints],
            "preserved_transcripts": [
                item.transcript_id for item in checkpoints if item.transcript_id
            ],
            "preserved_evidence_refs": [
                ref for item in checkpoints for ref in item.evidence.get("evidence_refs", ())
            ],
            "preserved_reviews": [item.review_id for item in self.store.list_reviews(lane_id)],
            "status": "complete",
        }
