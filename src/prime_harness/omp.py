from __future__ import annotations

import base64
import binascii
import json
import os
import queue
import shutil
import subprocess
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prime_harness.domain import (
    AgentCommandResult,
    AgentSession,
    LaneState,
    WorkspacePolicy,
    new_id,
    now_utc,
)

_MAX_RPC_LINE = 1024 * 1024
_MAX_RPC_LOGICAL_FRAME = 64 * 1024 * 1024
_MAX_RPC_CHUNKS = 4096
_MARKER_NAME = ".prime-harness-session"
_TERMINAL_PROMPT_STATUSES = {"completed", "aborted", "error"}
_MUTATING_FILE_TOOLS = {"write", "edit", "ast_edit"}


def _public_activity(event: dict[str, Any]) -> dict[str, Any]:
    event_type = event.get("type")
    safe: dict[str, Any] = {"type": event_type} if isinstance(event_type, str) else {}
    if isinstance(event.get("messageId"), str):
        safe["messageId"] = event["messageId"]
    tool_name = event.get("toolName")
    if isinstance(tool_name, str):
        safe["tool"] = tool_name[:100]
    else:
        call = event.get("toolCall")
        if isinstance(call, dict) and isinstance(call.get("name"), str):
            safe["tool"] = call["name"][:100]
    result = event.get("result")
    is_error = event.get("isError") is True or (
        isinstance(result, dict) and result.get("isError") is True
    )
    if event_type == "tool_execution_end":
        safe["status"] = "failed" if is_error else "completed"
    elif event_type == "prompt_result" and event.get("status") in _TERMINAL_PROMPT_STATUSES:
        safe["status"] = event["status"]
    return safe


class OmpRpcError(RuntimeError):
    pass


class OmpRpcProcess:
    """Small, process-backed client for OMP's documented JSONL RPC transport."""

    def __init__(
        self,
        command: str,
        workspace: Path,
        timeout_seconds: int,
        environment: dict[str, str] | None = None,
    ) -> None:
        executable = shutil.which(command) or (
            command if Path(command).is_file() else None
        )
        if executable is None:
            raise OmpRpcError("Configured OMP executable is unavailable")
        try:
            self.process = subprocess.Popen(  # noqa: S603 - executable is operator-configured.
                [executable, "--mode", "rpc", "--no-ui", "--approval-mode", "write"],
                cwd=workspace,
                env={**os.environ, **(environment or {})},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError:
            raise OmpRpcError("Configured OMP executable could not be started") from None
        self.timeout_seconds = timeout_seconds
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._responses: dict[str, queue.Queue[dict[str, Any] | BaseException]] = {}
        self._prompt_results: dict[str, queue.Queue[dict[str, Any] | BaseException]] = {}
        self._ready: queue.Queue[dict[str, Any] | BaseException] = queue.Queue(maxsize=1)
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._internal_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=1000)
        self._closed = threading.Event()
        self._chunk_id: str | None = None
        self._chunk_count = 0
        self._chunk_byte_length = 0
        self._chunk_parts: list[bytes] = []
        self._chunk_size = 0
        self._reader_failed = threading.Event()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        try:
            ready = self._ready.get(timeout=timeout_seconds)
        except queue.Empty:
            self.close()
            raise OmpRpcError("OMP RPC startup timed out") from None
        if isinstance(ready, BaseException):
            self.close()
            raise OmpRpcError("OMP RPC failed before becoming ready") from None
        if ready.get("type") != "ready":
            self.close()
            raise OmpRpcError("OMP RPC sent an invalid startup frame")
        version = ready.get("protocolVersion")
        supported_versions = ready.get("supportedProtocolVersions")
        self.protocol_version = 1
        if isinstance(supported_versions, list) and 2 in supported_versions:
            try:
                negotiated = self.request("negotiate_protocol", protocolVersion=2)
            except OmpRpcError:
                self.close()
                raise
            if negotiated.get("protocolVersion") != 2:
                self.close()
                raise OmpRpcError("OMP RPC protocol v2 negotiation failed")
            self.protocol_version = 2
        elif version != 1:
            self.close()
            raise OmpRpcError("OMP RPC advertised no supported protocol version")

    def subscribe(self, listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        with self._pending_lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._pending_lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def recent_events(self) -> list[dict[str, Any]]:
        with self._pending_lock:
            return list(self._recent_events)

    def _subscribe_internal(
        self, listener: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        with self._pending_lock:
            self._internal_listeners.append(listener)

        def unsubscribe() -> None:
            with self._pending_lock:
                if listener in self._internal_listeners:
                    self._internal_listeners.remove(listener)

        return unsubscribe

    def request(self, command_type: str, **payload: Any) -> dict[str, Any]:
        request_id = new_id("omp-rpc")
        response_queue: queue.Queue[dict[str, Any] | BaseException] = queue.Queue(maxsize=1)
        with self._pending_lock:
            if self._reader_failed.is_set():
                raise OmpRpcError("OMP RPC output stream failed")
            self._responses[request_id] = response_queue
        frame = {"id": request_id, "type": command_type, **payload}
        try:
            self._write_frame(frame)
            try:
                response = response_queue.get(timeout=self.timeout_seconds)
            except queue.Empty:
                raise OmpRpcError(f"OMP RPC command timed out: {command_type}") from None
            if isinstance(response, BaseException):
                raise OmpRpcError(f"OMP RPC command failed: {command_type}") from None
            if response.get("success") is not True:
                raise OmpRpcError(f"OMP RPC command was rejected: {command_type}")
            response_command = response.get("command")
            if response_command is not None and response_command != command_type:
                raise OmpRpcError("OMP RPC response command did not match its request")
            data = response.get("data")
            return data if isinstance(data, dict) else {}
        finally:
            with self._pending_lock:
                self._responses.pop(request_id, None)

    def open_session(self, session_dir: Path) -> dict[str, Any]:
        return self.request("open_session", sessionDir=str(session_dir))

    def prompt_and_wait(
        self,
        message: str,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        seen: list[dict[str, Any]] = []

        def collect(event: dict[str, Any]) -> None:
            seen.append(event)
            if on_event is not None:
                on_event(event)

        unsubscribe = self._subscribe_internal(collect)
        prompt_id: str | None = None
        prompt_queue: queue.Queue[dict[str, Any] | BaseException] | None = None
        try:
            prompt_id = new_id("omp-prompt")
            prompt_queue = queue.Queue(maxsize=1)
            with self._pending_lock:
                self._prompt_results[prompt_id] = prompt_queue
                self._responses[prompt_id] = queue.Queue(maxsize=1)
            self._write_frame({"id": prompt_id, "type": "prompt", "message": message})
            acknowledgement = self._wait_for_id(self._responses, prompt_id, "prompt")
            if acknowledgement.get("success") is not True:
                raise OmpRpcError("OMP rejected the instruction")
            data = acknowledgement.get("data")
            if isinstance(data, dict) and data.get("agentInvoked") is False:
                return (
                    {
                        "status": "completed",
                        "agentInvoked": False,
                        "sessionSettled": True,
                    },
                    seen,
                )
            outcome = self._wait_for_id(self._prompt_results, prompt_id, "prompt")
            status = outcome.get("status")
            if status not in _TERMINAL_PROMPT_STATUSES:
                raise OmpRpcError("OMP returned an invalid prompt result")
            return outcome, seen
        finally:
            unsubscribe()
            if prompt_id is not None:
                with self._pending_lock:
                    self._responses.pop(prompt_id, None)
                    self._prompt_results.pop(prompt_id, None)

    def abort(self) -> bool:
        self.request("abort")
        return True

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except (OSError, OmpRpcError):
                self._reader_failed.set()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def _wait_for_id(
        self,
        registry: dict[str, queue.Queue[dict[str, Any] | BaseException]],
        request_id: str,
        command: str,
    ) -> dict[str, Any]:
        with self._pending_lock:
            response_queue = registry[request_id]
        try:
            response = response_queue.get(timeout=self.timeout_seconds)
        except queue.Empty:
            raise OmpRpcError(f"OMP RPC command timed out: {command}") from None
        if isinstance(response, BaseException):
            raise OmpRpcError(f"OMP RPC process stopped during {command}") from None
        return response

    def _write_frame(self, frame: dict[str, Any]) -> None:
        encoded = json.dumps(frame, separators=(",", ":"), ensure_ascii=True)
        if len(encoded.encode("utf-8")) + 1 > _MAX_RPC_LINE:
            raise OmpRpcError("OMP RPC request exceeds the documented frame limit")
        if (
            self._reader_failed.is_set()
            or self.process.stdin is None
            or self.process.poll() is not None
        ):
            raise OmpRpcError("OMP RPC process is not running")
        with self._write_lock:
            try:
                self.process.stdin.write(encoded + "\n")
                self.process.stdin.flush()
            except (OSError, BrokenPipeError):
                raise OmpRpcError("OMP RPC process is not accepting commands") from None

    def _read_stdout(self) -> None:
        stdout = self.process.stdout
        if stdout is None:
            self._fail_pending()
            return
        try:
            while not self._closed.is_set():
                line = stdout.readline(_MAX_RPC_LINE + 2)
                if not line:
                    break
                if len(line.encode("utf-8")) > _MAX_RPC_LINE:
                    raise OmpRpcError("OMP RPC response exceeds the documented frame limit")
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    raise OmpRpcError("OMP RPC emitted an invalid JSONL frame") from None
                if not isinstance(frame, dict):
                    raise OmpRpcError("OMP RPC emitted an invalid frame")
                decoded = self._decode_frame(frame)
                if decoded is not None:
                    self._dispatch_frame(decoded)
        except (OSError, OmpRpcError):
            pass
        finally:
            self._fail_pending()

    def _decode_frame(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        if frame.get("type") != "rpc_chunk":
            if self._chunk_id is not None:
                raise OmpRpcError("OMP RPC interrupted a chunked frame")
            return frame
        if self.protocol_version != 2:
            raise OmpRpcError("OMP RPC sent chunks before protocol v2 negotiation")

        chunk_id = frame.get("chunkId")
        index = frame.get("index")
        count = frame.get("count")
        byte_length = frame.get("byteLength")
        encoded_data = frame.get("data")
        if (
            not isinstance(chunk_id, str)
            or not chunk_id
            or isinstance(index, bool)
            or not isinstance(index, int)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or not 1 <= count <= _MAX_RPC_CHUNKS
            or isinstance(byte_length, bool)
            or not isinstance(byte_length, int)
            or not 1 <= byte_length <= _MAX_RPC_LOGICAL_FRAME
            or not isinstance(encoded_data, str)
        ):
            raise OmpRpcError("OMP RPC sent an invalid chunk frame")

        if index == 0:
            if self._chunk_id is not None:
                raise OmpRpcError("OMP RPC interleaved chunked frames")
            self._chunk_id = chunk_id
            self._chunk_count = count
            self._chunk_byte_length = byte_length
            self._chunk_parts = []
            self._chunk_size = 0
        elif (
            self._chunk_id != chunk_id
            or self._chunk_count != count
            or self._chunk_byte_length != byte_length
        ):
            raise OmpRpcError("OMP RPC sent an out-of-order chunk frame")

        if index != len(self._chunk_parts):
            raise OmpRpcError("OMP RPC sent an out-of-order chunk frame")
        try:
            chunk = base64.b64decode(encoded_data, validate=True)
        except (ValueError, binascii.Error):
            raise OmpRpcError("OMP RPC sent invalid base64 chunk data") from None
        self._chunk_size += len(chunk)
        if self._chunk_size > self._chunk_byte_length:
            raise OmpRpcError("OMP RPC chunks exceed the advertised frame size")
        self._chunk_parts.append(chunk)
        if len(self._chunk_parts) < self._chunk_count:
            return None

        if self._chunk_size != self._chunk_byte_length:
            raise OmpRpcError("OMP RPC chunks do not match the advertised frame size")
        payload = b"".join(self._chunk_parts)
        self._chunk_id = None
        self._chunk_count = 0
        self._chunk_byte_length = 0
        self._chunk_parts = []
        self._chunk_size = 0
        try:
            decoded = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OmpRpcError("OMP RPC chunks did not contain valid UTF-8 JSON") from None
        if not isinstance(decoded, dict):
            raise OmpRpcError("OMP RPC chunks did not contain a JSON object")
        return decoded

    def _dispatch_frame(self, frame: dict[str, Any]) -> None:
        if frame.get("type") == "ready":
            try:
                self._ready.put_nowait(frame)
            except queue.Full:
                pass
            return
        frame_type = frame.get("type")
        request_id = frame.get("id")
        with self._pending_lock:
            if frame_type == "response" and isinstance(request_id, str):
                target = self._responses.get(request_id)
                if target is not None:
                    self._put(target, frame)
                return
            if frame_type == "prompt_result" and isinstance(request_id, str):
                target = self._prompt_results.get(request_id)
                if target is not None:
                    self._put(target, frame)
                return
            safe_frame = _public_activity(frame)
            self._recent_events.append(safe_frame)
            listeners = tuple(self._listeners)
            internal_listeners = tuple(self._internal_listeners)
        for listener in internal_listeners:
            listener(frame)
        for listener in listeners:
            listener(safe_frame)

    @staticmethod
    def _put(
        target: queue.Queue[dict[str, Any] | BaseException], value: dict[str, Any] | BaseException
    ) -> None:
        try:
            target.put_nowait(value)
        except queue.Full:
            pass

    def _fail_pending(self) -> None:
        self._reader_failed.set()
        error = OmpRpcError("OMP RPC process stopped")
        with self._pending_lock:
            registries = (self._responses, self._prompt_results)
            for registry in registries:
                for target in registry.values():
                    self._put(target, error)
            if self._ready.empty():
                self._put(self._ready, error)


@dataclass(slots=True)
class OmpAgentProvider:
    agent_id: str
    workspace_path: Path
    executable: str = "omp"
    timeout_seconds: int = 1800
    environment: dict[str, str] | None = None
    _processes: dict[str, OmpRpcProcess] = field(default_factory=dict, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)

    def start_session(self) -> AgentSession:
        session_id = new_id("session")
        session_dir = self._session_directory(session_id)
        session_dir.mkdir(parents=True, mode=0o700)
        marker = session_dir / _MARKER_NAME
        try:
            with marker.open("x", encoding="utf-8") as owner_file:
                owner_file.write(session_id)
        except OSError:
            raise OmpRpcError("Cannot claim a new Prime Harness OMP session directory") from None
        process: OmpRpcProcess | None = None
        try:
            process = self._new_process()
            opened = process.open_session(session_dir)
            provider_session_id = opened.get("sessionId")
            if (
                opened.get("cancelled") is True
                or not isinstance(provider_session_id, str)
                or not provider_session_id
            ):
                raise OmpRpcError("OMP did not return a persistent session identity")
        except Exception as exc:
            if process is not None:
                process.close()
            if isinstance(exc, OmpRpcError):
                raise
            raise OmpRpcError("OMP session could not be started") from None
        session = AgentSession(
            session_id=session_id,
            lane_id="unassigned",
            agent_id=self.agent_id,
            provider="omp",
            workspace_path=self.workspace_path.resolve(),
            provider_session_id=provider_session_id,
            state=LaneState.IDLE,
            process_status="running",
        )
        with self._lock:
            self._processes[session_id] = process
        return session

    def restore_session(self, session: AgentSession) -> AgentSession:
        if session.provider != "omp" or session.agent_id != self.agent_id:
            raise OmpRpcError("Persisted session does not belong to this OMP provider")
        if session.workspace_path.resolve() != self.workspace_path.resolve():
            raise OmpRpcError("Persisted session workspace does not match the OMP provider")
        if not session.provider_session_id:
            raise OmpRpcError("Persisted session has no OMP session identity")
        session_dir = self._session_directory(session.session_id)
        marker = session_dir / _MARKER_NAME
        try:
            if marker.read_text(encoding="utf-8") != session.session_id:
                raise OmpRpcError("Session directory is not owned by Prime Harness")
        except OSError:
            raise OmpRpcError("Prime Harness OMP session directory is unavailable") from None
        process = self._new_process()
        try:
            opened = process.open_session(session_dir)
            if opened.get("sessionId") != session.provider_session_id:
                raise OmpRpcError("OMP restored a different persistent session")
        except Exception as exc:
            process.close()
            if isinstance(exc, OmpRpcError):
                raise
            raise OmpRpcError("OMP session could not be restored") from None
        with self._lock:
            previous = self._processes.pop(session.session_id, None)
            if previous is not None:
                previous.close()
            self._processes[session.session_id] = process
        session.process_status = "restored"
        session.updated_at = now_utc()
        return session

    def send_instruction(self, session: AgentSession, instruction: str) -> AgentCommandResult:
        process = self._process_for(session)
        if not instruction.strip():
            return AgentCommandResult(False, "", "Instruction is empty", "failed")
        try:
            outcome, events = process.prompt_and_wait(instruction)
        except OmpRpcError as exc:
            return AgentCommandResult(
                False,
                "",
                str(exc),
                "failed",
            )
        status = str(outcome.get("status"))
        assistant_text = self._assistant_text(events)
        changed_files = self._changed_files(events)
        permission_request = self._permission_request(events)

        # Collect structured diagnostics
        diagnostics = self._build_instruction_diagnostics(outcome, events)

        if permission_request:
            diagnostics["approval_blocked"] = True
            return AgentCommandResult(
                False,
                assistant_text,
                "OMP operation requires human approval",
                "approval_required",
                changed_files=changed_files,
                evidence=self._evidence(events, session, status, diagnostics),
            )

        if outcome.get("agentInvoked") is False:
            session.process_status = "failed"
            return AgentCommandResult(
                False,
                assistant_text,
                "OMP agent was not invoked",
                "failed",
                changed_files=changed_files,
                evidence=self._evidence(events, session, status, diagnostics),
            )

        if status == "aborted":
            session.process_status = "stopped"
            return AgentCommandResult(
                False,
                assistant_text,
                "OMP operation was cancelled",
                "aborted",
                changed_files=changed_files,
                evidence=self._evidence(events, session, status, diagnostics),
            )

        if status != "completed" or diagnostics.get("tool_failures"):
            session.process_status = "failed"
            error_msg = self._format_failure_message(diagnostics)
            return AgentCommandResult(
                False,
                assistant_text,
                error_msg,
                "failed",
                changed_files=changed_files,
                evidence=self._evidence(events, session, status, diagnostics),
            )

        session.process_status = "completed"
        session.updated_at = now_utc()
        return AgentCommandResult(
            True,
            assistant_text,
            status="completed",
            changed_files=changed_files,
            evidence=self._evidence(events, session, status, diagnostics),
        )

    def snapshot(self, session: AgentSession) -> AgentSession:
        with self._lock:
            process = self._processes.get(session.session_id)
        session.process_status = (
            "running" if process and process.process.poll() is None else "stopped"
        )
        session.updated_at = now_utc()
        return session

    def stop_session(self, session: AgentSession) -> bool:
        with self._lock:
            process = self._processes.get(session.session_id)
        if process is None:
            return False
        try:
            process.abort()
        except OmpRpcError:
            if process.process.poll() is None:
                return False
        process.close()
        with self._lock:
            self._processes.pop(session.session_id, None)
        session.process_status = "stopped"
        session.state = LaneState.IDLE
        session.updated_at = now_utc()
        return True

    def subscribe_activity(
        self, session_id: str, listener: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        return self._process_for_session_id(session_id).subscribe(listener)

    def recent_activity(self, session_id: str) -> list[dict[str, Any]]:
        return self._process_for_session_id(session_id).recent_events()

    def _new_process(self) -> OmpRpcProcess:
        return OmpRpcProcess(
            self.executable,
            self.workspace_path.resolve(),
            self.timeout_seconds,
            self.environment,
        )

    def _session_directory(self, session_id: str) -> Path:
        if (
            not session_id.startswith("session-")
            or not session_id.removeprefix("session-").isalnum()
        ):
            raise OmpRpcError("Invalid Prime Harness session identity")
        relative = Path(".prime-harness") / "omp-sessions" / session_id
        try:
            return WorkspacePolicy(self.workspace_path.resolve()).resolve_relative(str(relative))
        except ValueError:
            raise OmpRpcError("OMP session directory is outside its workspace") from None

    def _process_for(self, session: AgentSession) -> OmpRpcProcess:
        if session.provider != "omp" or session.agent_id != self.agent_id:
            raise OmpRpcError("Session does not belong to this OMP provider")
        with self._lock:
            process = self._processes.get(session.session_id)
        if process is None:
            raise OmpRpcError("OMP session is not attached; restore it explicitly")
        return process

    def _process_for_session_id(self, session_id: str) -> OmpRpcProcess:
        with self._lock:
            process = self._processes.get(session_id)
        if process is None:
            raise OmpRpcError("OMP session is not attached")
        return process

    def _changed_files(self, events: list[dict[str, Any]]) -> list[str]:
        files: set[str] = set()
        policy = WorkspacePolicy(self.workspace_path.resolve())
        for event in events:
            if (
                event.get("type") != "tool_execution_start"
                or event.get("toolName") not in _MUTATING_FILE_TOOLS
            ):
                continue
            arguments = event.get("args")
            if not isinstance(arguments, dict):
                continue
            candidate = arguments.get("path") or arguments.get("file_path")
            if not isinstance(candidate, str):
                continue
            try:
                resolved = policy.resolve_relative(candidate)
                files.add(resolved.relative_to(self.workspace_path.resolve()).as_posix())
            except (ValueError, OSError):
                continue
        return sorted(files)

    @staticmethod
    def _assistant_text(events: list[dict[str, Any]]) -> str:
        fragments: list[str] = []
        for event in events:
            if event.get("type") == "message_update":
                assistant_event = event.get("assistantMessageEvent")
                if (
                    isinstance(assistant_event, dict)
                    and assistant_event.get("type") == "text_delta"
                    and isinstance(assistant_event.get("delta"), str)
                ):
                    fragments.append(assistant_event["delta"])
        return "".join(fragments)

    @staticmethod
    def _permission_request(events: list[dict[str, Any]]) -> bool:
        for event in events:
            if event.get("type") != "tool_execution_end":
                continue
            if event.get("isError") is not True:
                continue
            result = event.get("result")
            if isinstance(result, (str, bytes, bytearray)):
                rendered = result.decode("utf-8", errors="replace") if isinstance(result, (bytes, bytearray)) else result
            else:
                try:
                    rendered = json.dumps(result, ensure_ascii=True, default=str)
                except (TypeError, ValueError):
                    continue
            if (
                "approval" in rendered.lower()
                or "interactive ui" in rendered.lower()
                or "no ui" in rendered.lower()
            ):
                return True
        return False

    @staticmethod
    def _build_instruction_diagnostics(
        outcome: dict[str, Any], events: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Build bounded, structured diagnostics from outcome and events.
        
        Captures categorical fields without exposing raw prompts, model output, or credentials.
        """
        diagnostics: dict[str, Any] = {}

        # Outcome status and invocation state
        status = outcome.get("status")
        diagnostics["outcome_status"] = status if status in _TERMINAL_PROMPT_STATUSES else "unknown"

        agent_invoked = outcome.get("agentInvoked")
        if isinstance(agent_invoked, bool):
            diagnostics["agent_invoked"] = agent_invoked

        session_settled = outcome.get("sessionSettled")
        if isinstance(session_settled, bool):
            diagnostics["session_settled"] = session_settled

        # Tool invocation and error summary
        tools_invoked: set[str] = set()
        tool_failures: dict[str, str] = {}
        
        for event in events:
            event_type = event.get("type")
            
            if event_type == "tool_execution_start":
                tool_name = event.get("toolName")
                if isinstance(tool_name, str):
                    tools_invoked.add(tool_name[:50] if tool_name in {"write", "edit", "ast_edit", "read", "bash", "shell"} else "other")
            
            elif event_type == "tool_execution_end":
                tool_name = event.get("toolName")
                if isinstance(tool_name, str):
                    result = event.get("result")
                    if event.get("isError") is True or (isinstance(result, dict) and result.get("isError") is True):
                        tool_failures[tool_name if tool_name in {"write", "edit", "ast_edit", "read", "bash", "shell"} else "other"] = "error"

        diagnostics["tools_invoked"] = sorted(tools_invoked)[:12]
        
        diagnostics["tool_failures"] = sorted(tool_failures.keys())[:12]

        # Agent lifecycle events
        agent_started = any(e.get("type") == "agent_start" for e in events)
        agent_ended = any(e.get("type") == "agent_end" for e in events)
        
        diagnostics["agent_lifecycle"] = {"started": agent_started, "ended": agent_ended}

        return diagnostics

    @staticmethod
    def _format_failure_message(diagnostics: dict[str, Any]) -> str:
        """Format a human-readable failure message from diagnostics."""
        status = diagnostics.get("outcome_status", "unknown")
        agent_invoked = diagnostics.get("agent_invoked")
        tool_failures = diagnostics.get("tool_failures")

        if agent_invoked is False:
            return "OMP agent was not invoked"
        if tool_failures:
            return "OMP tool execution failed (" + ", ".join(tool_failures[:2]) + ")"
        if status == "error":
            return "OMP prompt returned error status"
        if status == "aborted":
            return "OMP prompt was aborted"
        return "OMP operation did not complete"

    def _evidence(
        self,
        events: list[dict[str, Any]],
        session: AgentSession,
        status: str,
        diagnostics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        summaries = [
            {
                "type": event.get("type"),
                "tool": self._tool_name(event),
                "status": self._event_status(event),
            }
            for event in events
            if event.get("type")
            in {
                "agent_start",
                "agent_end",
                "tool_execution_start",
                "tool_execution_end",
                "extension_ui_request",
            }
        ]
        evidence: dict[str, Any] = {
            "provider": "omp",
            "provider_session_id": session.provider_session_id,
            "execution_status": status,
            "activity": summaries,
        }
        if diagnostics:
            evidence["diagnostics"] = diagnostics
        return evidence

    @staticmethod
    def _tool_name(event: dict[str, Any]) -> str | None:
        name = event.get("toolName")
        if isinstance(name, str):
            return name[:100]
        call = event.get("toolCall")
        if isinstance(call, dict) and isinstance(call.get("name"), str):
            return call["name"][:100]
        return None

    @staticmethod
    def _event_status(event: dict[str, Any]) -> str | None:
        result = event.get("result")
        if event.get("type") == "tool_execution_end":
            is_error = event.get("isError") is True or (
                isinstance(result, dict) and result.get("isError") is True
            )
            return "failed" if is_error else "completed"
        return None
