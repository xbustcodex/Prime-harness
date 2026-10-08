from __future__ import annotations

import base64
import json
import queue
from pathlib import Path
from typing import Any

import pytest

from prime_harness.domain import LaneState
from prime_harness.omp import OmpAgentProvider, OmpRpcError, OmpRpcProcess
from prime_harness.services import HarnessService
from prime_harness.stores import SqliteStore


class _FakeStdout:
    def __init__(self) -> None:
        self.frames: queue.Queue[str | None] = queue.Queue()

    def readline(self, _limit: int = -1) -> str:
        frame = self.frames.get()
        return "" if frame is None else frame

    def emit(self, frame: dict[str, Any]) -> None:
        self.frames.put(json.dumps(frame) + "\n")


class _FakeStdin:
    def __init__(self, process: _FakeOmpProcess) -> None:
        self.process = process
        self.buffer = ""

    def write(self, value: str) -> int:
        self.buffer += value
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.process.handle(json.loads(line))
        return len(value)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.process.stdout.frames.put(None)


class _FakeOmpProcess:
    def __init__(self) -> None:
        self.stdout = _FakeStdout()
        self.stdin = _FakeStdin(self)
        self.returncode: int | None = None
        self.prompt_count = 0
        self.stdout.emit(
            {
                "type": "ready",
                "protocolVersion": 1,
                "supportedProtocolVersions": [1, 2],
            }
        )

    def handle(self, request: dict[str, Any]) -> None:
        command = request["type"]
        request_id = request["id"]
        data: dict[str, Any]
        if command == "negotiate_protocol":
            data = {"protocolVersion": 2}
        elif command == "open_session":
            data = {"sessionId": "omp-session-1"}
        elif command == "prompt":
            self.prompt_count += 1
            data = {"agentInvoked": True}
        else:
            data = {}
        self.stdout.emit(
            {
                "type": "response",
                "id": request_id,
                "command": command,
                "success": True,
                "data": data,
            }
        )
        if command == "prompt":
            self.stdout.emit(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "text_delta",
                        "delta": f"iteration-{self.prompt_count}",
                    },
                }
            )
            self.stdout.emit(
                {
                    "type": "tool_execution_start",
                    "toolCallId": "call-1",
                    "toolName": "write",
                    "args": {"path": "src/result.py", "content": "private tool argument"},
                }
            )
            self.stdout.emit(
                {
                    "type": "tool_execution_end",
                    "toolCallId": "call-1",
                    "toolName": "write",
                    "result": {"text": "private tool result"},
                    "isError": False,
                }
            )
            self.stdout.emit(
                {
                    "type": "prompt_result",
                    "id": request_id,
                    "status": "completed",
                }
            )

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15
        self.stdout.frames.put(None)

    def kill(self) -> None:
        self.returncode = -9
        self.stdout.frames.put(None)


@pytest.fixture
def fake_omp(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> list[_FakeOmpProcess]:
    processes: list[_FakeOmpProcess] = []

    def start(*_args: Any, **_kwargs: Any) -> _FakeOmpProcess:
        process = _FakeOmpProcess()
        processes.append(process)
        return process

    monkeypatch.setattr("prime_harness.omp.shutil.which", lambda _command: "/fake/omp")
    monkeypatch.setattr("prime_harness.omp.subprocess.Popen", start)

    def close_processes() -> None:
        for process in processes:
            process.stdin.close()

    request.addfinalizer(close_processes)
    return processes


def test_omp_rpc_protocol_projection_and_session_restore(
    tmp_path: Path, fake_omp: list[_FakeOmpProcess]
) -> None:
    provider = OmpAgentProvider("agent-1", tmp_path, executable="omp")
    session = provider.start_session()
    result = provider.send_instruction(session, "Do the first pass")

    assert session.provider_session_id == "omp-session-1"
    assert result.success is True
    assert result.output == "iteration-1"
    assert result.changed_files == ["src/result.py"]
    assert fake_omp[0].prompt_count == 1
    projected = provider.recent_activity(session.session_id)
    assert all("args" not in event and "result" not in event for event in projected)
    assert "private tool argument" not in repr(projected)
    assert "private tool result" not in repr(projected)

    provider._processes[session.session_id].close()
    provider._processes.pop(session.session_id)
    restored = OmpAgentProvider("agent-1", tmp_path, executable="omp")
    restored_session = restored.restore_session(session)
    continued = restored.send_instruction(restored_session, "Apply reviewer correction")

    assert continued.success is True
    assert continued.output == "iteration-1"
    assert fake_omp[1].prompt_count == 1
    assert restored_session.provider_session_id == session.provider_session_id


def test_omp_ownership_marker_and_explicit_restore_identity(
    tmp_path: Path, fake_omp: list[_FakeOmpProcess]
) -> None:
    provider = OmpAgentProvider("agent-1", tmp_path, executable="omp")
    session = provider.start_session()
    session_dir = provider._session_directory(session.session_id)
    marker = session_dir / ".prime-harness-session"
    marker.write_text("another-owner", encoding="utf-8")

    with pytest.raises(OmpRpcError, match="not owned"):
        OmpAgentProvider("agent-1", tmp_path, executable="omp").restore_session(session)
    assert len(fake_omp) == 1


def test_omp_permission_request_handles_non_serializable_results() -> None:
    rejection = [{
        "type": "tool_execution_end",
        "isError": True,
        "result": {"message": "Approval required", "payload": {"bad": {1, 2}}},
    }]
    non_serializable = [{
        "type": "tool_execution_end",
        "isError": True,
        "result": object(),
    }]

    assert OmpAgentProvider._permission_request(rejection) is True
    assert OmpAgentProvider._permission_request(non_serializable) is False


def test_omp_rpc_v2_reassembles_chunks_and_rejects_interleaving() -> None:
    decoder = object.__new__(OmpRpcProcess)
    decoder.protocol_version = 2
    decoder._chunk_id = None
    decoder._chunk_count = 0
    decoder._chunk_byte_length = 0
    decoder._chunk_parts = []
    decoder._chunk_size = 0
    payload = json.dumps({"type": "response", "id": "request-1"}).encode()
    split = len(payload) // 2
    first = {
        "type": "rpc_chunk",
        "chunkId": "chunk-1",
        "index": 0,
        "count": 2,
        "byteLength": len(payload),
        "data": base64.b64encode(payload[:split]).decode(),
    }
    second = {**first, "index": 1, "data": base64.b64encode(payload[split:]).decode()}

    assert decoder._decode_frame(first) is None
    assert decoder._decode_frame(second) == {"type": "response", "id": "request-1"}
    assert decoder._decode_frame(first) is None
    with pytest.raises(OmpRpcError, match="interrupted"):
        decoder._decode_frame({"type": "response", "id": "other"})


def test_omp_workflow_reviewer_correction_and_sqlite_restore(
    tmp_path: Path, fake_omp: list[_FakeOmpProcess]
) -> None:
    database = tmp_path / "state.sqlite3"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SqliteStore(database)
    service = HarnessService(
        store=store,
        workspace_root=workspace,
        default_agent_provider="omp",
        omp_executable="omp",
    )
    lane = service.create_lane("OMP", "omp-project", Path("omp-project"))
    connection_id = service.create_connection(
        lane.lane_id,
        "owner",
        {"read", "review", "send_instruction", "continue", "stop"},
    )
    provider = OmpAgentProvider(lane.agent_id, lane.workspace_path, executable="omp")
    service.register_agent_provider(lane.lane_id, provider)
    session = service.start_agent_session(lane.lane_id)

    first = service.send_instruction(
        lane.lane_id, session.session_id, connection_id, "Initial pass"
    )
    assert first.success is True
    checkpoint = service.store.list_checkpoints(lane.lane_id)[0]
    thread = service.create_review_thread_authenticated(connection_id, lane.lane_id)
    review = service.review_checkpoint_authenticated(
        connection_id, lane.lane_id, thread.thread_id, checkpoint.checkpoint_id
    )
    assert review.status == "rejected"
    assert review.requested_changes
    continued = service.continue_agent(
        lane.lane_id,
        session.session_id,
        connection_id,
        review.requested_changes[0],
    )
    assert continued.success is True
    checkpoints = service.store.list_checkpoints(lane.lane_id)
    assert len(checkpoints) == 2
    assert fake_omp[0].prompt_count == 2
    assert checkpoints[1].session_id == session.session_id
    assert checkpoints[1].evidence["provider_session_id"] == session.provider_session_id
    updated_session = service.get_session(session.session_id)
    assert updated_session is not None
    assert updated_session.state is LaneState.WORKING

    provider._processes[session.session_id].close()
    provider._processes.pop(session.session_id)
    restarted = HarnessService(
        store=SqliteStore(database),
        workspace_root=workspace,
        default_agent_provider="omp",
        omp_executable="omp",
    )
    restarted_provider = OmpAgentProvider(lane.agent_id, lane.workspace_path, executable="omp")
    restarted.register_agent_provider(lane.lane_id, restarted_provider)
    restarted_session = restarted.restore_agent_session(lane.lane_id, session.session_id)
    assert restarted_session.provider_session_id == session.provider_session_id
    assert restarted_session.process_status == "restored"
    assert len(fake_omp) == 2
    restarted_provider._processes[session.session_id].close()
