from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import uuid4


class LaneState(StrEnum):
    IDLE = "IDLE"
    WORKING = "WORKING"
    CHECKPOINT = "CHECKPOINT"
    WAITING_FOR_REVIEW = "WAITING_FOR_REVIEW"
    REVIEWING = "REVIEWING"
    CONTINUING = "CONTINUING"
    PAUSED = "PAUSED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    def can_transition(self, target: LaneState) -> bool:
        allowed = {
            LaneState.IDLE: {
                LaneState.WORKING,
                LaneState.PAUSED,
                LaneState.FAILED,
                LaneState.COMPLETED,
                LaneState.HUMAN_REQUIRED,
            },
            LaneState.WORKING: {
                LaneState.CHECKPOINT,
                LaneState.PAUSED,
                LaneState.FAILED,
                LaneState.COMPLETED,
                LaneState.HUMAN_REQUIRED,
            },
            LaneState.CHECKPOINT: {
                LaneState.WAITING_FOR_REVIEW,
                LaneState.CONTINUING,
                LaneState.PAUSED,
                LaneState.FAILED,
            },
            LaneState.WAITING_FOR_REVIEW: {
                LaneState.REVIEWING,
                LaneState.CONTINUING,
                LaneState.FAILED,
                LaneState.HUMAN_REQUIRED,
            },
            LaneState.REVIEWING: {
                LaneState.WAITING_FOR_REVIEW,
                LaneState.CONTINUING,
                LaneState.COMPLETED,
                LaneState.FAILED,
                LaneState.HUMAN_REQUIRED,
            },
            LaneState.CONTINUING: {
                LaneState.WORKING,
                LaneState.PAUSED,
                LaneState.FAILED,
                LaneState.COMPLETED,
            },
            LaneState.PAUSED: {LaneState.WORKING, LaneState.FAILED, LaneState.COMPLETED},
            LaneState.HUMAN_REQUIRED: {
                LaneState.WORKING,
                LaneState.PAUSED,
                LaneState.FAILED,
                LaneState.COMPLETED,
            },
            LaneState.COMPLETED: {LaneState.IDLE},
            LaneState.FAILED: {LaneState.IDLE, LaneState.PAUSED, LaneState.HUMAN_REQUIRED},
        }
        return target in allowed[self]


def now_utc() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


@dataclass(slots=True)
class Lane:
    lane_id: str
    name: str
    project_id: str
    workspace_path: Path
    agent_id: str
    reviewer_id: str
    state: LaneState = LaneState.IDLE
    owner_id: str | None = None
    provider: str = "shell"
    reviewer_provider: str = "deterministic"
    model: str = "phase-1"
    current_objective: str | None = None
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True)
class AgentState:
    lane_id: str
    session_id: str
    state: LaneState
    objective: str
    current_revision: int
    last_checkpoint_id: str | None
    writer_id: str | None = None
    process_status: str = "idle"


@dataclass(slots=True)
class AgentSession:
    session_id: str
    lane_id: str
    agent_id: str
    provider: str
    workspace_path: Path
    provider_session_id: str | None = None
    state: LaneState = LaneState.IDLE
    process_status: str = "idle"
    writer_id: str | None = None
    writer_lease_id: str | None = None
    writer_lease_expires_at: datetime | None = None
    writer_lease_version: int = 0
    current_objective: str | None = None
    current_revision: int = 0
    last_checkpoint_id: str | None = None
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)

    @property
    def status(self) -> str:
        return self.process_status


@dataclass(slots=True)
class SessionLease:
    session_id: str
    lease_id: str
    writer_id: str
    expires_at: datetime
    version: int

    def can_write_by(self, writer_id: str, at: datetime) -> bool:
        return writer_id == self.writer_id and at < self.expires_at


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(slots=True, frozen=True)
class Checkpoint:
    checkpoint_id: str
    lane_id: str
    session_id: str
    objective: str
    git_status: str
    changed_files: list[str]
    build_results: str
    evidence: dict[str, Any]
    revision: int
    created_at: datetime
    created_by: str
    correlation_id: str
    transcript_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "changed_files", tuple(self.changed_files))
        object.__setattr__(self, "evidence", _freeze(self.evidence))


@dataclass(slots=True, frozen=True)
class ReviewerThread:
    thread_id: str
    lane_id: str
    reviewer_id: str
    provider: str
    model: str
    current_context_digest: str
    current_checkpoint_id: str | None
    rollover_count: int = 0
    parent_thread_id: str | None = None
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)


@dataclass(slots=True, frozen=True)
class Review:
    review_id: str
    lane_id: str
    reviewer_thread_id: str
    checkpoint_id: str
    status: str
    verdict: str
    findings: list[str]
    requested_changes: list[str]
    evidence_refs: list[str]
    model: str
    created_at: datetime = field(default_factory=now_utc)
    created_by: str = "reviewer"

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "requested_changes", tuple(self.requested_changes))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))


@dataclass(slots=True, frozen=True)
class ApprovalRequest:
    request_id: str
    lane_id: str
    request_key: str
    action: str
    parameters: dict[str, Any]
    status: str
    requested_by: str
    requested_at: datetime
    approved_by: str | None = None
    approved_at: datetime | None = None
    result: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", _freeze(self.parameters))
        if self.result is not None:
            object.__setattr__(self, "result", _freeze(self.result))


@dataclass(slots=True)
class EventRecord:
    event_id: str
    lane_id: str
    event_type: str
    severity: str
    payload: dict[str, Any]
    occurred_at: datetime
    actor_id: str | None
    correlation_id: str
    acknowledged_at: datetime | None = None


@dataclass(slots=True)
class EventNotification:
    event_id: str
    lane_id: str
    event_type: str
    severity: str
    payload: dict[str, Any]
    occurred_at: datetime
    actor_id: str | None
    correlation_id: str
    unread: bool = True


@dataclass(slots=True)
class SecretRecord:
    secret_id: str
    lane_id: str
    purpose: str
    encrypted_value: bytes
    owner_id: str = ""
    revoked_at: datetime | None = None
    created_at: datetime = field(default_factory=now_utc)
    rotated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class SecretMetadata:
    secret_id: str
    lane_id: str
    owner_id: str
    purpose: str
    created_at: datetime
    rotated_at: datetime | None
    revoked_at: datetime | None


@dataclass(slots=True)
class ConnectionRecord:
    connection_id: str
    lane_id: str
    owner_id: str
    permissions: set[str]
    revoked_at: datetime | None = None
    created_at: datetime = field(default_factory=now_utc)
    expires_at: datetime | None = None
    credential_hash: str | None = None


@dataclass(slots=True)
class LaneStateTransition:
    lane_id: str
    from_state: LaneState
    to_state: LaneState
    actor_id: str
    correlation_id: str
    occurred_at: datetime
    reason: str | None = None


@dataclass(slots=True)
class WorkspacePolicy:
    root: Path

    def resolve(self, requested: Path) -> Path:
        candidate = requested.expanduser().resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ValueError(f"Path is outside workspace: {requested}") from exc
        return candidate

    def resolve_relative(self, requested: str | Path) -> Path:
        import re
        from urllib.parse import unquote

        raw = str(requested)
        decoded = raw
        for _ in range(3):
            decoded = unquote(decoded)
        normalized = decoded.replace("\\", "/")
        if (
            "\x00" in normalized
            or normalized.startswith("/")
            or re.match(r"^[A-Za-z]:", normalized)
            or any(part == ".." for part in normalized.split("/"))
        ):
            raise ValueError(f"Path is outside workspace: {requested}")
        return self.resolve(self.root / normalized)


@dataclass(slots=True)
class AgentCommandResult:
    success: bool
    output: str
    error: str | None = None
    status: str = "completed"
    exit_code: int | None = None
    changed_files: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
