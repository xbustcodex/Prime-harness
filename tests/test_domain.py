import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from prime_harness.domain import (
    AgentState,
    Checkpoint,
    Lane,
    LaneState,
    LaneStateTransition,
    Review,
    SecretMetadata,
    SessionLease,
    WorkspacePolicy,
)
from prime_harness.stores import InMemoryStore, SqliteStore


def test_lane_state_machine_enforces_allowed_transitions():
    expected = {
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
    for state in LaneState:
        for target in LaneState:
            assert state.can_transition(target) is (target in expected[state])


def test_lane_identity_is_separate_from_secret_identity():
    lane = Lane(
        lane_id="lane-a1",
        name="Agent 1",
        project_id="project-a",
        workspace_path=Path("/tmp/project-a"),
        agent_id="agent-shell-1",
        reviewer_id="reviewer-deterministic-1",
    )
    secret = SecretMetadata(
        secret_id="secret-connection-1",
        lane_id=lane.lane_id,
        owner_id="owner-a1",
        purpose="shell-agent",
        created_at=datetime.now(UTC),
        rotated_at=None,
        revoked_at=None,
    )

    assert secret.lane_id == lane.lane_id
    assert secret.secret_id != lane.lane_id
    assert secret.revoked_at is None


def test_writer_lease_is_single_writer_and_expiry_is_explicit():
    now = datetime.now(UTC)
    lease = SessionLease(
        session_id="session-1",
        lease_id="lease-1",
        writer_id="connection-1",
        expires_at=now,
        version=1,
    )

    assert not lease.can_write_by("connection-1", now)
    assert not lease.can_write_by("connection-2", now)
    assert not lease.can_write_by("connection-1", now.replace(year=now.year + 1))


def test_workspace_policy_rejects_path_escape():
    policy = WorkspacePolicy(root=Path("/srv/harness/projects"))

    with pytest.raises(ValueError, match="outside workspace"):
        policy.resolve(Path("/srv/harness/projects/../other"))


@pytest.mark.parametrize(
    "path",
    [
        "../sibling/file",
        "/etc/passwd",
        r"..\\sibling\\file",
        "folder/../../outside",
        "%2e%2e/sibling/file",
        "%252e%252e%252fsibling/file",
        "C:\\outside\\file",
        "//server/share/file",
    ],
)
def test_workspace_policy_rejects_encoded_and_mixed_path_escapes(path: str):
    policy = WorkspacePolicy(root=Path("/srv/harness/projects/A1"))
    with pytest.raises(ValueError, match="outside workspace"):
        policy.resolve_relative(path)


def test_checkpoint_and_review_records_are_immutable(tmp_path: Path):
    checkpoint = Checkpoint(
        checkpoint_id="checkpoint-1",
        lane_id="lane-a1",
        session_id="session-1",
        objective="Keep evidence durable",
        git_status="clean",
        changed_files=["src/a.py"],
        build_results="passed",
        evidence={"evidence_refs": ["diff:1"]},
        revision=1,
        created_at=datetime.now(UTC),
        created_by="test",
        correlation_id="corr-1",
    )
    review = Review(
        review_id="review-1",
        lane_id="lane-a1",
        reviewer_thread_id="thread-1",
        checkpoint_id=checkpoint.checkpoint_id,
        status="accepted",
        verdict="pass",
        findings=[],
        requested_changes=[],
        evidence_refs=["checkpoint:checkpoint-1"],
        model="deterministic",
    )

    objective_field = "objective"
    status_field = "status"
    with pytest.raises(AttributeError):
        setattr(checkpoint, objective_field, "mutated")
    with pytest.raises(AttributeError):
        setattr(review, status_field, "rejected")
    with pytest.raises(TypeError):
        checkpoint.evidence["new"] = "not allowed"
    store = InMemoryStore()
    store.create_checkpoint(checkpoint)
    store.create_review(review)
    with pytest.raises(ValueError, match="immutable"):
        store.create_checkpoint(checkpoint)
    with pytest.raises(ValueError, match="immutable"):
        store.create_review(review)
    sqlite_store = SqliteStore(tmp_path / "records.sqlite3")
    sqlite_store.create_lane(
        Lane(
            lane_id="lane-a1",
            name="A1",
            project_id="project-a",
            workspace_path=tmp_path,
            agent_id="agent-a1",
            reviewer_id="reviewer-a1",
        )
    )
    sqlite_store.create_checkpoint(checkpoint)
    sqlite_store.create_review(review)
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_store.create_checkpoint(checkpoint)
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_store.create_review(review)
    assert checkpoint.changed_files == ("src/a.py",)
    assert checkpoint.evidence["evidence_refs"] == ("diff:1",)


def test_agent_state_is_a_lane_scoped_snapshot():
    snapshot = AgentState(
        lane_id="lane-a1",
        session_id="session-1",
        state=LaneState.WORKING,
        objective="Implement validation",
        current_revision=3,
        last_checkpoint_id="checkpoint-3",
    )

    assert snapshot.lane_id == "lane-a1"
    assert snapshot.current_revision == 3


def test_transition_audit_preserves_actor_and_correlation():
    transition = LaneStateTransition(
        lane_id="lane-a1",
        from_state=LaneState.IDLE,
        to_state=LaneState.WORKING,
        actor_id="connection-human",
        correlation_id="corr-1",
        occurred_at=datetime.now(UTC),
    )

    assert transition.actor_id == "connection-human"
    assert transition.correlation_id == "corr-1"
    assert transition.from_state is LaneState.IDLE
