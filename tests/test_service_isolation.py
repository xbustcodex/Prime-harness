from datetime import UTC, datetime
from pathlib import Path

from prime_harness.services import HarnessService
from prime_harness.stores import InMemoryStore


def make_service(tmp_path: Path) -> HarnessService:
    store = InMemoryStore()
    return HarnessService(store=store, workspace_root=tmp_path, now=lambda: datetime.now(UTC))


def test_lanes_are_isolated_and_reviewer_context_is_lane_specific(tmp_path: Path):
    service = make_service(tmp_path)
    lane1 = service.create_lane("A1", project_id="project-1", workspace_path=tmp_path / "project-1")
    lane2 = service.create_lane("A2", project_id="project-2", workspace_path=tmp_path / "project-2")

    checkpoint1 = service.create_checkpoint(
        lane_id=lane1.lane_id,
        objective="Fix validation",
        git_status="modified: src/validation.py",
        changed_files=["src/validation.py"],
        build_results="passed",
        evidence={"selected_files": ["src/validation.py"]},
    )

    assert service.get_checkpoint(lane2.lane_id, checkpoint1.checkpoint_id) is None
    assert service.get_review_context(lane2.lane_id, checkpoint1.checkpoint_id) is None


def test_two_writers_cannot_own_one_session(tmp_path: Path):
    service = make_service(tmp_path)
    lane = service.create_lane("A1", project_id="project-1", workspace_path=tmp_path / "project-1")
    session = service.create_session(lane.lane_id, agent_id="agent-shell-1")

    first = service.acquire_writer_lease(session.session_id, "connection-1")
    second = service.acquire_writer_lease(session.session_id, "connection-2")
    owned = service.get_session(session.session_id)

    assert first is True
    assert second is False
    assert owned is not None
    assert owned.writer_id == "connection-1"


def test_reviewer_rollover_preserves_history(tmp_path: Path):
    service = make_service(tmp_path)
    lane = service.create_lane("A1", project_id="project-1", workspace_path=tmp_path / "project-1")
    checkpoint = service.create_checkpoint(
        lane_id=lane.lane_id,
        objective="Fix parser",
        git_status="clean",
        changed_files=[],
        build_results="passed",
        evidence={},
    )
    rolled = service.rollover_review_thread(lane.lane_id, checkpoint.checkpoint_id)

    assert rolled is not None
    assert service.get_checkpoint(lane.lane_id, checkpoint.checkpoint_id) is not None
    assert service.get_transcript(lane.lane_id, checkpoint.checkpoint_id) is not None


def test_revoked_connection_cannot_control_agent(tmp_path: Path):
    service = make_service(tmp_path)
    lane = service.create_lane("A1", project_id="project-1", workspace_path=tmp_path / "project-1")
    session = service.create_session(lane.lane_id, agent_id="agent-shell-1")
    connection_id = service.create_connection(
        lane.lane_id, "connection-1", permissions={"read", "send_instruction"}
    )
    assert service.revoke_connection(connection_id) is True

    assert service.can_execute(connection_id, "send_instruction", lane.lane_id) is False
    revoked_session = service.get_session(session.session_id)
    assert revoked_session is not None
    assert revoked_session.writer_id is None
