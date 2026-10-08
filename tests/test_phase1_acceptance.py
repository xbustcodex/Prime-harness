from __future__ import annotations

import base64
import hashlib
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from prime_harness.agents import ShellAgentProvider
from prime_harness.api import create_app
from prime_harness.config import AgentConfig
from prime_harness.domain import WorkspacePolicy
from prime_harness.secret_service import SecretOperationError
from prime_harness.services import HarnessService
from prime_harness.stores import SqliteStore

PERMISSIONS = {"read", "review", "send_instruction", "continue", "stop", "dangerous_action"}


def make_service(tmp_path: Path, clock=None) -> tuple[HarnessService, Path, Path]:
    database = tmp_path / "state.sqlite3"
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir(exist_ok=True)
    service = HarnessService(
        store=SqliteStore(database),
        workspace_root=workspace_root,
        now=clock or (lambda: datetime.now(UTC)),
    )
    return service, database, workspace_root


def make_lane(service: HarnessService, name: str, project: str) -> tuple[str, str]:
    lane = service.create_lane(name, project, Path(project))
    connection = service.create_connection(lane.lane_id, f"owner-{name}", PERMISSIONS)
    return lane.lane_id, connection


def make_checkpoint(
    service: HarnessService,
    lane_id: str,
    *,
    objective: str = "Review evidence",
    changed_files: list[str] | None = None,
    build_results: str = "passed",
    evidence: dict | None = None,
):
    return service.create_checkpoint(
        lane_id=lane_id,
        objective=objective,
        git_status="modified",
        changed_files=changed_files or [],
        build_results=build_results,
        evidence=evidence or {"evidence_refs": ["diff:1"], "transcript": "checkpoint transcript"},
    )


def test_authorized_service_and_api_deny_cross_lane_data_both_directions(tmp_path: Path):
    service, _, workspace_root = make_service(tmp_path)
    lane_a, connection_a = make_lane(service, "A1", "project-a")
    lane_b, connection_b = make_lane(service, "A2", "project-b")
    (workspace_root / "project-a").joinpath("private.txt").write_text(
        "A1 private", encoding="utf-8"
    )
    (workspace_root / "project-b").joinpath("private.txt").write_text(
        "A2 private", encoding="utf-8"
    )
    checkpoint_a = make_checkpoint(service, lane_a, evidence={"evidence_refs": ["a:evidence"]})
    checkpoint_b = make_checkpoint(service, lane_b, evidence={"evidence_refs": ["b:evidence"]})
    thread_a = service.create_review_thread_authenticated(connection_a, lane_a)
    thread_b = service.create_review_thread_authenticated(connection_b, lane_b)
    service.review_checkpoint_authenticated(
        connection_a, lane_a, thread_a.thread_id, checkpoint_a.checkpoint_id
    )
    service.review_checkpoint_authenticated(
        connection_b, lane_b, thread_b.thread_id, checkpoint_b.checkpoint_id
    )
    lane_b_record = service.store.get_lane(lane_b)
    assert lane_b_record is not None
    with pytest.raises(KeyError):
        service.submit_review(
            lane_a,
            thread_b.thread_id,
            checkpoint_a.checkpoint_id,
            lane_b_record.reviewer_id,
            service._reviewer_provider_for_lane(lane_b),
        )
    lane_a_record = service.store.get_lane(lane_a)
    assert lane_a_record is not None
    with pytest.raises(KeyError):
        service.submit_review(
            lane_b,
            thread_a.thread_id,
            checkpoint_b.checkpoint_id,
            lane_a_record.reviewer_id,
            service._reviewer_provider_for_lane(lane_a),
        )

    for own_lane, own_connection, own_checkpoint, other_lane, other_checkpoint, other_thread in (
        (lane_a, connection_a, checkpoint_a, lane_b, checkpoint_b, thread_b),
        (lane_b, connection_b, checkpoint_b, lane_a, checkpoint_a, thread_a),
    ):
        expected_contents = "A1 private" if own_lane == lane_a else "A2 private"
        assert (
            service.read_file_authenticated(own_connection, own_lane, "private.txt")
            == expected_contents
        )
        assert (
            service.read_checkpoint_authenticated(
                own_connection, own_lane, own_checkpoint.checkpoint_id
            ).lane_id
            == own_lane
        )
        assert (
            service.read_transcript_authenticated(
                own_connection, own_lane, own_checkpoint.checkpoint_id
            )["lane_id"]
            == own_lane
        )
        assert service.read_events_authenticated(own_connection, own_lane)
        assert service.read_reviewer_thread_authenticated(
            own_connection,
            own_lane,
            thread_a.thread_id if own_lane == lane_a else thread_b.thread_id,
        )
        with pytest.raises(PermissionError):
            service.read_file_authenticated(own_connection, other_lane, "private.txt")
        with pytest.raises(PermissionError):
            service.read_checkpoint_authenticated(
                own_connection, other_lane, other_checkpoint.checkpoint_id
            )
        with pytest.raises(PermissionError):
            service.read_transcript_authenticated(
                own_connection, other_lane, other_checkpoint.checkpoint_id
            )
        with pytest.raises(PermissionError):
            service.read_events_authenticated(own_connection, other_lane)
        with pytest.raises(PermissionError):
            service.read_reviewer_thread_authenticated(
                own_connection, other_lane, other_thread.thread_id
            )

    connection_a = service.issue_api_credential(connection_a)
    connection_b = service.issue_api_credential(connection_b)
    client = TestClient(create_app(service))
    assert client.get(f"/api/lanes/{lane_a}/state").status_code == 401
    assert (
        client.get(
            f"/api/lanes/{lane_b}/checkpoints/{checkpoint_b.checkpoint_id}",
            headers={"Authorization": f"Bearer {connection_a}"},
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/lanes/{lane_a}/checkpoints/{checkpoint_a.checkpoint_id}",
            headers={"Authorization": f"Bearer {connection_b}"},
        ).status_code
        == 403
    )


def test_sqlite_lease_race_has_one_winner_and_expiry_is_reacquired_auditably(tmp_path: Path):
    base = datetime.now(UTC)
    instant = [base]
    service, database, workspace_root = make_service(tmp_path, lambda: instant[0])
    lane_id, _ = make_lane(service, "A1", "project-a")
    session = service.create_session(lane_id, "agent-A1-1")
    controllers = [
        HarnessService(SqliteStore(database), workspace_root, lambda: instant[0]) for _ in range(2)
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda pair: pair[0].acquire_writer_lease(session.session_id, pair[1], 1),
                zip(controllers, ("controller-1", "controller-2"), strict=True),
            )
        )
    assert sorted(results) == [False, True]
    owned = SqliteStore(database).get_session(session.session_id)
    assert owned is not None
    first_writer = owned.writer_id
    first_lease = owned.writer_lease_id
    assert first_writer in {"controller-1", "controller-2"}

    instant[0] = base + timedelta(seconds=2)
    expired_before_reacquisition = SqliteStore(database).get_session(session.session_id)
    assert expired_before_reacquisition is not None
    assert expired_before_reacquisition.writer_id == first_writer
    expires_at = expired_before_reacquisition.writer_lease_expires_at
    assert expires_at is not None
    assert expires_at < instant[0]
    reclaimer = HarnessService(SqliteStore(database), workspace_root, lambda: instant[0])
    reclaimer.release_writer_lease(session.session_id, first_lease or "")
    assert reclaimer.acquire_writer_lease(session.session_id, "controller-3", 10)
    reclaimed = SqliteStore(database).get_session(session.session_id)
    assert reclaimed is not None
    assert reclaimed.writer_id == "controller-3"
    assert reclaimed.writer_lease_version == 2
    reclaimer.release_writer_lease(session.session_id, first_lease or "")
    still_owned = SqliteStore(database).get_session(session.session_id)
    assert still_owned is not None and still_owned.writer_id == "controller-3"
    events = reclaimer.get_events(lane_id)
    assert sum(event.event_type == "writer_lease_acquired" for event in events) == 2
    assert sum(event.event_type == "writer_lease_expired" for event in events) == 1


def test_parallel_lane_checkpoints_have_independent_reviewers_and_stable_local_order(
    tmp_path: Path,
):
    service, _, _ = make_service(tmp_path)
    lanes = [make_lane(service, f"A{index}", f"project-{index}") for index in range(8)]

    def checkpoint_and_review(item: tuple[str, str, int]):
        lane_id, connection_id, index = item
        checkpoint = make_checkpoint(
            service,
            lane_id,
            objective=f"objective-{index}",
            evidence={"evidence_refs": [f"lane-{index}:ref"], "lane_marker": index},
        )
        thread = service.create_review_thread_authenticated(connection_id, lane_id)
        review = service.review_checkpoint_authenticated(
            connection_id,
            lane_id,
            thread.thread_id,
            checkpoint.checkpoint_id,
        )
        return lane_id, checkpoint, thread, review

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                checkpoint_and_review, [(lane, conn, i) for i, (lane, conn) in enumerate(lanes)]
            )
        )

    for index, (lane_id, checkpoint, thread, review) in enumerate(results):
        assert checkpoint.lane_id == lane_id
        assert checkpoint.evidence["lane_marker"] == index
        assert review.lane_id == lane_id
        assert review.reviewer_thread_id == thread.thread_id
        stored_thread = service.store.get_thread(thread.thread_id)
        assert stored_thread is not None
        assert stored_thread.lane_id == lane_id

    concurrent_appends = [
        (lane_id, lane_index, append_index)
        for lane_index, (lane_id, _) in enumerate(lanes)
        for append_index in range(3)
    ]
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(
            pool.map(
                lambda item: make_checkpoint(
                    service,
                    item[0],
                    objective=f"parallel-{item[1]}-{item[2]}",
                    evidence={"lane_marker": item[1], "append_marker": item[2]},
                ),
                concurrent_appends,
            )
        )
    for lane_index, (lane_id, _) in enumerate(lanes):
        lane_checkpoints = service.store.list_checkpoints(lane_id)
        assert [item.revision for item in lane_checkpoints] == [1, 2, 3, 4]
        assert all(item.evidence["lane_marker"] == lane_index for item in lane_checkpoints)

    lane_id = results[0][0]
    extra = [
        make_checkpoint(service, lane_id, objective=f"ordered-{index}", evidence={"ordinal": index})
        for index in range(3)
    ]
    listed = service.store.list_checkpoints(lane_id)
    assert [item.revision for item in listed] == sorted(item.revision for item in listed)
    assert [item.checkpoint_id for item in listed[-3:]] == [item.checkpoint_id for item in extra]


def test_reviewer_rollover_creates_successor_and_preserves_durable_evidence(tmp_path: Path):
    service, database, workspace_root = make_service(tmp_path)
    lane_id, connection_id = make_lane(service, "A1", "project-a")
    checkpoint = make_checkpoint(
        service,
        lane_id,
        changed_files=["marker.txt"],
        build_results="not-run",
        evidence={
            "evidence_refs": ["diff:marker", "test:passed"],
            "transcript": "agent created marker",
        },
    )
    original_thread = service.create_review_thread_authenticated(connection_id, lane_id)
    review = service.review_checkpoint_authenticated(
        connection_id,
        lane_id,
        original_thread.thread_id,
        checkpoint.checkpoint_id,
    )
    assert review.status == "rejected"

    successor = service.rollover_review_thread(lane_id, checkpoint.checkpoint_id)
    assert successor is not None
    assert successor.thread_id != original_thread.thread_id
    assert successor.parent_thread_id == original_thread.thread_id
    assert successor.lane_id == lane_id
    assert successor.current_checkpoint_id == checkpoint.checkpoint_id
    restarted = HarnessService(SqliteStore(database), workspace_root)
    persisted_checkpoint = restarted.get_checkpoint(lane_id, checkpoint.checkpoint_id)
    assert persisted_checkpoint is not None
    assert persisted_checkpoint.evidence["evidence_refs"] == ("diff:marker", "test:passed")
    transcript = restarted.get_transcript(lane_id, checkpoint.checkpoint_id)
    assert transcript is not None
    assert transcript["transcript"] == "agent created marker"
    assert restarted.store.get_thread(original_thread.thread_id) is not None
    restored_successor = restarted.store.get_thread(successor.thread_id)
    assert restored_successor is not None
    assert restored_successor.parent_thread_id == original_thread.thread_id
    idempotent_successor = restarted.rollover_review_thread(lane_id, checkpoint.checkpoint_id)
    assert idempotent_successor is not None
    assert idempotent_successor.thread_id == successor.thread_id


def test_history_compaction_preserves_checkpoint_decisions_and_evidence_refs(tmp_path: Path):
    service, database, workspace_root = make_service(tmp_path)
    lane_id, connection_id = make_lane(service, "A1", "long-lived-project")
    checkpoints = [
        make_checkpoint(
            service,
            lane_id,
            objective=f"decision-{index}",
            evidence={
                "evidence_refs": [f"evidence:{index}"],
                "decision": f"decision-{index}",
                "transcript": f"transcript-{index}",
            },
        )
        for index in range(30)
    ]
    compacted = service.compact_history(lane_id)
    assert compacted["status"] == "complete"
    assert compacted["preserved_checkpoints"] == [item.checkpoint_id for item in checkpoints]
    assert len(compacted["preserved_transcripts"]) == 30
    assert set(compacted["preserved_evidence_refs"]) == {f"evidence:{index}" for index in range(30)}
    restarted = HarnessService(SqliteStore(database), workspace_root)
    assert restarted.get_state(lane_id)["project_id"] == "long-lived-project"
    assert all(restarted.get_checkpoint(lane_id, item.checkpoint_id) for item in checkpoints)
    assert (
        restarted.read_transcript_authenticated(
            connection_id, lane_id, checkpoints[-1].checkpoint_id
        )["transcript"]
        == "transcript-29"
    )


def test_approval_is_pending_then_exactly_once_across_races_and_restart(tmp_path: Path):
    service, database, workspace_root = make_service(tmp_path)
    lane_id, connection_id = make_lane(service, "A1", "project-a")
    pending = service.request_dangerous_action(
        lane_id,
        connection_id,
        "create-marker-1",
        "create_file",
        {"path": "approved.txt", "content": "approved exactly once"},
    )
    assert pending["status"] == "pending"
    assert not (workspace_root / "project-a" / "approved.txt").exists()
    duplicate = service.request_dangerous_action(
        lane_id,
        connection_id,
        "create-marker-1",
        "create_file",
        {"path": "approved.txt", "content": "must not replace original"},
    )
    assert duplicate["request_id"] == pending["request_id"]

    controllers = [HarnessService(SqliteStore(database), workspace_root) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda pair: pair[0].approve_dangerous_action(
                    lane_id,
                    connection_id,
                    pending["request_id"],
                    f"approver-{pair[1]}",
                ),
                [(controller, index) for index, controller in enumerate(controllers)],
            )
        )
    assert sorted(outcomes) == [False, True]
    target = workspace_root / "project-a" / "approved.txt"
    assert target.read_text(encoding="utf-8") == "approved exactly once"

    restarted = HarnessService(SqliteStore(database), workspace_root)
    assert not restarted.approve_dangerous_action(
        lane_id, connection_id, pending["request_id"], "approver-replay"
    )
    request = restarted.store.get_approval_request(pending["request_id"])
    assert request is not None and request.status == "executed"
    events = restarted.get_events(lane_id)
    assert sum(event.event_type == "dangerous_action_executed" for event in events) == 1
    assert sum(event.event_type == "dangerous_action_pending" for event in events) == 1

    existing_file = workspace_root / "project-a" / "existing.txt"
    existing_file.write_text("original", encoding="utf-8")
    failed_request = restarted.request_dangerous_action(
        lane_id,
        connection_id,
        "cannot-overwrite-existing",
        "create_file",
        {"path": "existing.txt", "content": "replacement"},
    )
    assert not restarted.approve_dangerous_action(
        lane_id,
        connection_id,
        failed_request["request_id"],
        "approver-failure",
    )
    failed_record = restarted.store.get_approval_request(failed_request["request_id"])
    assert failed_record is not None and failed_record.status == "failed"
    assert failed_record.result is not None
    assert failed_record.result["status"] == "failed"
    assert len(failed_record.result["diagnostic"]) <= 500
    assert existing_file.read_text(encoding="utf-8") == "original"
    assert (
        sum(
            event.event_type == "dangerous_action_executed"
            for event in restarted.get_events(lane_id)
        )
        == 1
    )


def test_failed_external_action_is_persisted_failed_without_secrets(tmp_path: Path, caplog):
    service, database, workspace_root = make_service(tmp_path)
    lane_id, connection_id = make_lane(service, "A1", "project-a")
    credential_marker = "credential-sentinel-not-for-storage"
    lane = service.store.get_lane(lane_id)
    assert lane is not None
    provider = ShellAgentProvider(
        AgentConfig(
            agent_id=lane.agent_id,
            provider="shell",
            command="bash",
            workspace_path=workspace_root / "project-a",
            environment={"API_TOKEN": credential_marker},
        )
    )
    service.register_agent_provider(lane_id, provider)
    session = service.start_agent_session(lane_id)
    result = service.send_instruction(
        lane_id,
        session.session_id,
        connection_id,
        f"Unsupported operation with token={credential_marker}",
    )
    assert result.success is False
    assert result.status == "failed"
    assert "Unsupported instruction" in (result.error or "")
    assert len(result.error or "") <= 500
    events = service.get_events(lane_id)
    failure = next(event for event in events if event.event_type == "agent_instruction_sent")
    assert failure.payload["status"] == "failed"
    assert failure.severity == "error"
    stored = SqliteStore(database).get_session(session.session_id)
    assert stored is not None and credential_marker not in (stored.current_objective or "")

    api_credential = service.issue_api_credential(connection_id)
    persisted_connection = service.store.get_connection(connection_id)
    assert persisted_connection is not None
    assert persisted_connection.credential_hash == hashlib.sha256(
        api_credential.encode("utf-8")
    ).hexdigest()
    assert api_credential != connection_id
    visible = "\n".join(
        [
            repr(events),
            repr(failure.payload),
            repr(result),
            repr(service.get_history(lane_id)),
            caplog.text,
            database.read_bytes().decode("latin-1"),
        ]
    )
    assert credential_marker not in visible
    assert api_credential not in visible
    assert service.read_reviews_authenticated(connection_id, lane_id) == []
    client = TestClient(create_app(service))
    response = client.post(
        f"/api/lanes/{lane_id}/agents/{session.session_id}/instructions",
        headers={"Authorization": f"Bearer {api_credential}"},
        json={"instruction": f"Unsupported operation token={credential_marker}"},
    )
    api_status, api_body = response.status_code, response.text
    assert api_status == 200 and credential_marker not in api_body
    assert api_credential not in api_body
    post_request_surfaces = "\n".join(
        [
            repr(service.get_events(lane_id)),
            repr(service.get_history(lane_id)),
            caplog.text,
            database.read_bytes().decode("latin-1"),
        ]
    )
    assert api_credential not in post_request_surfaces
    assert (
        client.get(
            "/api/secrets/secret-test",
            headers={"Authorization": f"Bearer {connection_id}"},
        ).status_code
        == 404
    )


def test_secret_service_encrypts_audits_and_only_provides_authorized_internal_use(
    tmp_path: Path, monkeypatch, caplog
):
    service, database, _ = make_service(tmp_path)
    lane_id, api_connection_id = make_lane(service, "A1", "project-a")
    secret_connection_id = service.create_connection(
        lane_id,
        "owner-A1",
        PERMISSIONS | {"manage_secrets", "use_secret"},
    )
    plaintext = "provider-token-never-persist-this"
    monkeypatch.setenv("PRIME_HARNESS_SECRET_KEY", base64.b64encode(b"k" * 32).decode("ascii"))

    metadata = service.secret_service.create_secret(
        secret_connection_id, "agent.provider", plaintext
    )
    stored = service.store.get_secret(metadata.secret_id)
    assert stored is not None
    assert stored.encrypted_value != plaintext.encode()
    assert stored.encrypted_value.startswith(b"PHSE1")
    assert stored.encrypted_value[-16:] != plaintext.encode()
    reopened_service = HarnessService(SqliteStore(database), service.workspace_root)

    provider_received: list[str] = []

    def invoke_provider(token: str) -> str:
        provider_received.append(token)
        return "provider-accepted"

    assert (
        reopened_service.secret_service.use_secret(
            secret_connection_id, metadata.secret_id, invoke_provider
        )
        == "provider-accepted"
    )
    assert provider_received == [plaintext]

    credential = service.issue_api_credential(api_connection_id)
    client = TestClient(create_app(service))
    response = client.get(
        f"/api/lanes/{lane_id}/secrets",
        headers={"Authorization": f"Bearer {credential}"},
    )
    assert response.status_code == 200
    assert metadata.secret_id in response.text
    assert plaintext not in response.text
    assert client.get(
        f"/api/secrets/{metadata.secret_id}",
        headers={"Authorization": f"Bearer {credential}"},
    ).status_code == 404

    events = service.get_events(lane_id)
    history = service.get_history(lane_id)
    database_surfaces = b"".join(
        path.read_bytes() for path in database.parent.glob("state.sqlite3*")
    )
    exposed = "\n".join(
        [
            repr(response.json()),
            repr(events),
            repr(history),
            caplog.text,
            database_surfaces.decode("latin-1"),
        ]
    )
    assert plaintext not in exposed
    assert b"k" * 32 not in database_surfaces
    assert all(event.payload.get("secret_id") != plaintext for event in events)
    assert {event.event_type for event in events} >= {"secret_created", "secret_used"}
    events_response = client.get(
        f"/api/lanes/{lane_id}/events",
        headers={"Authorization": f"Bearer {credential}"},
    )
    assert plaintext not in events_response.text

    foreign_connection = service.create_connection(
        lane_id, "different-owner", PERMISSIONS | {"use_secret"}
    )
    with pytest.raises(SecretOperationError, match="access denied"):
        service.secret_service.use_secret(foreign_connection, metadata.secret_id, invoke_provider)
    assert provider_received == [plaintext]


def test_secret_service_fails_closed_for_missing_invalid_and_wrong_keys(
    tmp_path: Path, monkeypatch, caplog
):
    service, database, _ = make_service(tmp_path)
    lane_id, _ = make_lane(service, "A1", "project-a")
    connection_id = service.create_connection(
        lane_id, "owner-A1", PERMISSIONS | {"manage_secrets", "use_secret"}
    )
    plaintext = "key-failure-secret-sentinel"
    monkeypatch.delenv("PRIME_HARNESS_SECRET_KEY", raising=False)
    with pytest.raises(SecretOperationError) as missing_key:
        service.secret_service.create_secret(connection_id, "agent.provider", plaintext)
    assert plaintext not in str(missing_key.value)
    assert service.store.list_secrets(lane_id) == []

    monkeypatch.setenv("PRIME_HARNESS_SECRET_KEY", "invalid-base64!")
    with pytest.raises(SecretOperationError) as invalid_key:
        service.secret_service.create_secret(connection_id, "agent.provider", plaintext)
    assert plaintext not in str(invalid_key.value)
    assert service.store.list_secrets(lane_id) == []

    monkeypatch.setenv("PRIME_HARNESS_SECRET_KEY", base64.b64encode(b"a" * 32).decode("ascii"))
    created = service.secret_service.create_secret(connection_id, "agent.provider", plaintext)
    monkeypatch.setenv("PRIME_HARNESS_SECRET_KEY", base64.b64encode(b"b" * 32).decode("ascii"))
    with pytest.raises(SecretOperationError) as wrong_key:
        service.secret_service.use_secret(connection_id, created.secret_id, lambda token: token)
    assert plaintext not in str(wrong_key.value)
    assert service.store.get_secret(created.secret_id) is not None

    events = service.get_events(lane_id)
    recorded = "\n".join([repr(events), repr(service.get_history(lane_id))])
    sqlite_bytes = b"".join(path.read_bytes() for path in database.parent.glob("state.sqlite3*"))
    assert plaintext not in recorded
    assert plaintext.encode() not in sqlite_bytes
    secret_create_statuses = {
        event.payload["status"] for event in events if event.event_type == "secret_created"
    }
    assert secret_create_statuses == {"failed", "succeeded"}
    failed_use = next(event for event in events if event.event_type == "secret_used")
    assert failed_use.payload["status"] == "failed"

    def fail_provider(token: str) -> None:
        raise RuntimeError(f"provider failed for token {token}")

    with pytest.raises(SecretOperationError) as provider_failure:
        service.secret_service.use_secret(connection_id, created.secret_id, fail_provider)
    assert plaintext not in str(provider_failure.value)
    assert plaintext not in caplog.text
    failed_events = repr(service.get_events(lane_id))
    assert plaintext not in failed_events


def test_secret_update_rotation_and_deletion_never_expose_values(
    tmp_path: Path, monkeypatch, caplog
):
    service, database, _ = make_service(tmp_path)
    lane_id, api_connection_id = make_lane(service, "A1", "project-a")
    manager_id = service.create_connection(
        lane_id, "owner-A1", PERMISSIONS | {"manage_secrets", "use_secret"}
    )
    old_value = "old-rotation-secret-sentinel"
    new_value = "new-rotation-secret-sentinel"
    monkeypatch.setenv("PRIME_HARNESS_SECRET_KEY", base64.b64encode(b"r" * 32).decode("ascii"))

    created = service.secret_service.create_secret(manager_id, "agent.provider", old_value)
    rotated = service.secret_service.update_secret(manager_id, created.secret_id, new_value)
    assert rotated.rotated_at is not None
    assert rotated.secret_id == created.secret_id
    assert old_value not in repr(created)
    assert new_value not in repr(rotated)
    api_credential = service.issue_api_credential(api_connection_id)
    client = TestClient(create_app(service))
    metadata_response = client.get(
        f"/api/lanes/{lane_id}/secrets",
        headers={"Authorization": f"Bearer {api_credential}"},
    )
    assert created.secret_id in metadata_response.text
    assert old_value not in metadata_response.text
    assert new_value not in metadata_response.text
    persisted_bytes = b"".join(
        path.read_bytes() for path in database.parent.glob("state.sqlite3*")
    )
    assert old_value.encode() not in persisted_bytes
    assert new_value.encode() not in persisted_bytes

    provider_values: list[str] = []
    service.secret_service.use_secret(
        manager_id, created.secret_id, lambda value: provider_values.append(value)
    )
    assert provider_values == [new_value]
    assert service.secret_service.delete_secret(manager_id, created.secret_id) is True
    assert service.store.get_secret(created.secret_id) is None

    response = client.get(
        f"/api/lanes/{lane_id}/secrets",
        headers={"Authorization": f"Bearer {api_credential}"},
    )
    sqlite_bytes = b"".join(path.read_bytes() for path in database.parent.glob("state.sqlite3*"))
    surfaces = "\n".join(
        [
            response.text,
            repr(service.get_events(lane_id)),
            repr(service.get_history(lane_id)),
            caplog.text,
            sqlite_bytes.decode("latin-1"),
        ]
    )
    assert old_value not in surfaces
    assert new_value not in surfaces
    assert {event.event_type for event in service.get_events(lane_id)} >= {
        "secret_created",
        "secret_updated",
        "secret_used",
        "secret_deleted",
    }


def test_path_validation_blocks_api_escape_and_allows_in_workspace_file(tmp_path: Path):
    service, _, workspace_root = make_service(tmp_path)
    lane_id, connection_id = make_lane(service, "A1", "project-a")
    root = workspace_root / "project-a"
    sibling = workspace_root / "project-a-evil"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("sibling", encoding="utf-8")
    (root / "legitimate.txt").write_text("safe", encoding="utf-8")
    symlink = root / "link-out"
    symlink.symlink_to(sibling, target_is_directory=True)
    policy = WorkspacePolicy(root)
    attacks = [
        "../project-a-evil/secret.txt",
        str(sibling / "secret.txt"),
        r"..\project-a-evil\secret.txt",
        "%2e%2e/project-a-evil/secret.txt",
        "%252e%252e%252fproject-a-evil/secret.txt",
        "link-out/secret.txt",
        "C:\\project-a-evil\\secret.txt",
    ]
    for path in attacks:
        with pytest.raises(ValueError):
            policy.resolve_relative(path)
        with pytest.raises(ValueError):
            service.read_file_authenticated(connection_id, lane_id, path)
    assert service.read_file_authenticated(connection_id, lane_id, "legitimate.txt") == "safe"

    connection_id = service.issue_api_credential(connection_id)
    app_client = TestClient(create_app(service))
    response = app_client.get(
        f"/api/lanes/{lane_id}/files/%2e%2e/project-a-evil/secret.txt",
        headers={"Authorization": f"Bearer {connection_id}"},
    )
    assert response.status_code in {400, 404}
    assert (
        app_client.get(
            f"/api/lanes/{lane_id}/files/legitimate.txt",
            headers={"Authorization": f"Bearer {connection_id}"},
        ).json()["content"]
        == "safe"
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http(port: int, method: str, path: str, token: str | None = None, body: dict | None = None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    response = httpx.request(
        method,
        f"http://127.0.0.1:{port}{path}",
        headers=headers,
        json=body,
        timeout=2,
    )
    return response.status_code, response.json()


def _start_web(database: Path, workspace: Path) -> tuple[subprocess.Popen, int]:
    port = _free_port()
    environment = os.environ.copy()
    source_root = str(Path(__file__).parents[1] / "src")
    environment["PYTHONPATH"] = source_root + os.pathsep + environment.get("PYTHONPATH", "")
    environment["PRIME_HARNESS_DB"] = str(database)
    environment["PRIME_HARNESS_WORKSPACE"] = str(workspace)
    environment["PRIME_HARNESS_PORT"] = str(port)
    process = subprocess.Popen(
        [sys.executable, "-m", "prime_harness.server"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.communicate()[0]
            raise AssertionError(f"web process exited during startup: {output}")
        try:
            _http(port, "GET", "/api/lanes/not-created/state")
            break
        except httpx.RequestError:
            time.sleep(0.05)
    else:
        process.terminate()
        output = process.communicate(timeout=5)[0]
        raise AssertionError(f"web process did not start: {output}")
    return process, port


def _stop_web(process: subprocess.Popen | None) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def test_http_vertical_workflow_restart_recovery_and_revocation(tmp_path: Path):
    service, database, workspace_root = make_service(tmp_path)
    process: subprocess.Popen | None
    process, port = _start_web(database, workspace_root)
    try:
        lane_id, connection_record_id = make_lane(service, "A1", "project-a")
        connection_id = service.issue_api_credential(connection_record_id)
        lane_b, connection_b_id = make_lane(service, "A2", "project-b")
        connection_b = service.issue_api_credential(connection_b_id)
        (workspace_root / "project-b" / "private.txt").write_text("A2-only", encoding="utf-8")
        assert _http(port, "GET", f"/api/lanes/{lane_id}/state")[0] == 401
        assert (
            _http(port, "GET", f"/api/lanes/{lane_id}/state", connection_record_id)[0] == 403
        )
        assert _http(port, "GET", f"/api/lanes/{lane_b}/files/private.txt", connection_id)[0] == 403
        assert _http(port, "GET", f"/api/lanes/{lane_id}/files/private.txt", connection_b)[0] == 403
        status, session = _http(port, "POST", f"/api/lanes/{lane_id}/sessions", connection_id)
        assert status == 200
        session_id = session["session_id"]
        provider_session_id = session["provider_session_id"]
        assert provider_session_id

        status, initial = _http(
            port,
            "POST",
            f"/api/lanes/{lane_id}/agents/{session_id}/instructions",
            connection_id,
            {"instruction": "Create marker.txt containing 'first-pass' using a Python command."},
        )
        assert status == 200 and initial["success"]
        assert (workspace_root / "project-a" / "marker.txt").read_text(
            encoding="utf-8"
        ) == "first-pass"
        status, generated = _http(
            port,
            "GET",
            f"/api/lanes/{lane_id}/checkpoints",
            connection_id,
        )
        assert status == 200 and generated
        checkpoint1 = generated[-1]
        assert checkpoint1["build_results"] == "not-run"
        assert checkpoint1["changed_files"] == ["marker.txt"]
        assert checkpoint1["evidence"]["action_status"] == "succeeded"
        assert (
            "build and test evidence has not been collected"
            in checkpoint1["evidence"]["transcript"]
        )
        assert (
            _http(port, "GET", f"/api/lanes/{lane_id}/state", connection_id)[1]["state"]
            == "WAITING_FOR_REVIEW"
        )
        status, thread = _http(port, "POST", f"/api/lanes/{lane_id}/review-threads", connection_id)
        assert status == 200
        status, first_review = _http(
            port,
            "POST",
            f"/api/lanes/{lane_id}/review-threads/{thread['thread_id']}/checkpoints/{checkpoint1['checkpoint_id']}/review",
            connection_id,
        )
        assert status == 200 and first_review["status"] == "rejected"
        assert (
            _http(port, "GET", f"/api/lanes/{lane_id}/state", connection_id)[1]["state"]
            == "CONTINUING"
        )

        successor = service.rollover_review_thread(lane_id, checkpoint1["checkpoint_id"])
        assert successor is not None and successor.parent_thread_id == thread["thread_id"]
        status, correction = _http(
            port,
            "POST",
            f"/api/lanes/{lane_id}/agents/{session_id}/continue",
            connection_id,
            {"instruction": "Create correction.txt containing 'reviewed' using a Python command."},
        )
        assert status == 200 and correction["success"]
        assert (workspace_root / "project-a" / "correction.txt").read_text(
            encoding="utf-8"
        ) == "reviewed"
        status, generated_after_correction = _http(
            port,
            "GET",
            f"/api/lanes/{lane_id}/checkpoints",
            connection_id,
        )
        assert status == 200
        assert generated_after_correction[-1]["changed_files"] == ["correction.txt"]
        # This verified record adds test evidence to the continuation checkpoint.
        status, checkpoint2 = _http(
            port,
            "POST",
            f"/api/lanes/{lane_id}/checkpoints",
            connection_id,
            {
                "session_id": session_id,
                "objective": "Apply reviewer correction",
                "git_status": "clean",
                "changed_files": ["correction.txt"],
                "build_results": "passed",
                "evidence": {
                    "evidence_refs": [
                        "file:correction.txt",
                        "test:correction-file-content",
                        "review:corrected",
                    ],
                    "transcript": "applied reviewer correction",
                },
            },
        )
        assert status == 200
        status, final_review = _http(
            port,
            "POST",
            f"/api/lanes/{lane_id}/review-threads/{successor.thread_id}/checkpoints/{checkpoint2['checkpoint_id']}/review",
            connection_id,
        )
        assert status == 200 and final_review["status"] == "accepted"
        assert (
            _http(port, "GET", f"/api/lanes/{lane_id}/state", connection_id)[1]["state"]
            == "COMPLETED"
        )
        transition_targets = [
            item.to_state.value for item in service.store.list_transitions(lane_id)
        ]
        assert transition_targets[:10] == [
            "WORKING",
            "CHECKPOINT",
            "WAITING_FOR_REVIEW",
            "REVIEWING",
            "CONTINUING",
            "WORKING",
            "CHECKPOINT",
            "WAITING_FOR_REVIEW",
            "REVIEWING",
            "COMPLETED",
        ]
        event_types = [event.event_type for event in service.get_events(lane_id)]
        assert "writer_lease_acquired" in event_types
        assert "writer_lease_released" in event_types
        assert "checkpoint_created" in event_types
        _stop_web(process)
        process = None

        process, port = _start_web(database, workspace_root)
        state_status, state = _http(port, "GET", f"/api/lanes/{lane_id}/state", connection_id)
        assert state_status == 200
        assert state == {
            "lane_id": lane_id,
            "state": "COMPLETED",
            "objective": "Create marker.txt containing 'first-pass' using a Python command.",
            "project_id": "project-a",
        }
        first_status, persisted_checkpoint = _http(
            port,
            "GET",
            f"/api/lanes/{lane_id}/checkpoints/{checkpoint1['checkpoint_id']}",
            connection_id,
        )
        assert first_status == 200
        assert persisted_checkpoint["evidence"]["action_status"] == "succeeded"
        transcript_status, transcript = _http(
            port,
            "GET",
            f"/api/lanes/{lane_id}/checkpoints/{checkpoint1['checkpoint_id']}/transcript",
            connection_id,
        )
        assert transcript_status == 200
        assert "Agent operation succeeded" in transcript["transcript"]
        assert transcript["project_id"] == "project-a"
        session_status, persisted_session = _http(
            port,
            "GET",
            f"/api/lanes/{lane_id}/sessions/{session_id}",
            connection_id,
        )
        assert session_status == 200
        assert persisted_session["provider_session_id"] == provider_session_id
        assert persisted_session["session_id"] == session_id
        reviews_status, reviews = _http(port, "GET", f"/api/lanes/{lane_id}/reviews", connection_id)
        assert reviews_status == 200
        assert [item["status"] for item in reviews] == ["rejected", "accepted"]
        assert reviews[0]["reviewer_thread_id"] == thread["thread_id"]
        assert reviews[1]["reviewer_thread_id"] == successor.thread_id
        assert service.get_checkpoint(lane_id, checkpoint1["checkpoint_id"]) is not None
        restored_thread = service.store.get_thread(successor.thread_id)
        assert restored_thread is not None
        assert restored_thread.parent_thread_id == thread["thread_id"]

        # A restored provider/session identity remains usable after the web process restart.
        status, after_restart = _http(
            port,
            "POST",
            f"/api/lanes/{lane_id}/agents/{session_id}/continue",
            connection_id,
            {
                "instruction": (
                    "Create after-restart.txt containing 'same-session' using a Python command."
                )
            },
        )
        assert status == 200 and after_restart["success"]
        assert (workspace_root / "project-a" / "after-restart.txt").read_text(
            encoding="utf-8"
        ) == "same-session"

        pending_status, pending = _http(
            port,
            "POST",
            f"/api/lanes/{lane_id}/dangerous-actions",
            connection_id,
            {
                "request_key": "blocked-until-approved",
                "action": "create_file",
                "parameters": {"path": "blocked.txt", "content": "only after approval"},
            },
        )
        assert pending_status == 200 and pending["status"] == "pending"
        assert not (workspace_root / "project-a" / "blocked.txt").exists()
        _stop_web(process)
        process = None
        process, port = _start_web(database, workspace_root)
        approval_status, approval = _http(
            port,
            "POST",
            f"/api/lanes/{lane_id}/dangerous-actions/{pending['request_id']}/approve",
            connection_id,
        )
        assert approval_status == 200 and approval["executed"]
        assert (workspace_root / "project-a" / "blocked.txt").read_text(
            encoding="utf-8"
        ) == "only after approval"
        approval_events = service.get_events(lane_id)
        assert (
            sum(event.event_type == "dangerous_action_executed" for event in approval_events) == 1
        )
        replay_status, replay = _http(
            port,
            "POST",
            f"/api/lanes/{lane_id}/dangerous-actions/{pending['request_id']}/approve",
            connection_id,
        )
        assert replay_status == 200 and not replay["executed"]

        assert service.revoke_connection(connection_record_id)
        assert _http(port, "GET", f"/api/lanes/{lane_id}/state", connection_id)[0] == 403
        revoked_calls = [
            ("GET", f"/api/lanes/{lane_id}/checkpoints", None),
            ("GET", f"/api/lanes/{lane_id}/events", None),
            ("GET", f"/api/lanes/{lane_id}/reviews", None),
            ("GET", f"/api/lanes/{lane_id}/files/marker.txt", None),
            (
                "GET",
                f"/api/lanes/{lane_id}/checkpoints/{checkpoint1['checkpoint_id']}/transcript",
                None,
            ),
            (
                "POST",
                f"/api/lanes/{lane_id}/agents/{session_id}/instructions",
                {"instruction": "Create forbidden.txt containing 'no' using a Python command."},
            ),
            (
                "POST",
                f"/api/lanes/{lane_id}/agents/{session_id}/continue",
                {"instruction": "Create forbidden.txt containing 'no' using a Python command."},
            ),
            (
                "POST",
                f"/api/lanes/{lane_id}/dangerous-actions/{pending['request_id']}/approve",
                None,
            ),
        ]
        for method, path, body in revoked_calls:
            assert _http(port, method, path, connection_id, body)[0] == 403
        assert not (workspace_root / "project-a" / "forbidden.txt").exists()
        assert _http(port, "GET", f"/api/lanes/{lane_b}/files/private.txt", connection_b)[0] == 200
    finally:
        if process is not None:
            _stop_web(process)
