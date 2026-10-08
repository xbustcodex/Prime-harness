from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from prime_harness.config import AgentConfig
from prime_harness.domain import (
    AgentCommandResult,
    AgentSession,
    LaneState,
    WorkspacePolicy,
    new_id,
    now_utc,
)


class CodingAgentProvider(Protocol):
    def start_session(self) -> AgentSession: ...
    def restore_session(self, session: AgentSession) -> AgentSession: ...
    def send_instruction(self, session: AgentSession, instruction: str) -> AgentCommandResult: ...
    def snapshot(self, session: AgentSession) -> AgentSession: ...
    def stop_session(self, session: AgentSession) -> bool: ...


@dataclass(slots=True)
class ShellSession:
    session: AgentSession
    process: subprocess.Popen[str] | None
    output: list[str]
    provider_session_id: str


class ShellAgentProvider:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self._sessions: dict[str, ShellSession] = {}

    def _workspace(self) -> Path:
        return self.config.workspace_path.resolve()

    def start_session(self) -> AgentSession:
        workspace = self._workspace()
        workspace.mkdir(parents=True, exist_ok=True)
        session = AgentSession(
            session_id=new_id("session"),
            lane_id="unassigned",
            agent_id=self.config.agent_id,
            provider=self.config.provider,
            workspace_path=workspace,
            state=LaneState.IDLE,
            process_status="running",
        )
        session.provider_session_id = session.session_id
        self._sessions[session.session_id] = ShellSession(
            session, None, [], session.provider_session_id
        )
        return session

    def restore_session(self, session: AgentSession) -> AgentSession:
        if session.provider != self.config.provider or session.agent_id != self.config.agent_id:
            raise ValueError("Persisted session does not belong to this provider")
        if session.workspace_path.resolve() != self._workspace():
            raise ValueError("Persisted session workspace does not match provider workspace")
        if not session.provider_session_id:
            raise ValueError("Persisted session has no provider session identity")
        self._sessions[session.session_id] = ShellSession(
            session, None, [], session.provider_session_id
        )
        session.process_status = "restored"
        session.updated_at = now_utc()
        return session

    def _write_file_command(self, target_path: Path, content: str) -> list[str]:
        configured_runtime = shutil.which(self.config.command)
        if configured_runtime is None:
            raise ValueError("Configured shell command is not installed")
        executable = Path(configured_runtime).name.lower()
        if executable.startswith("python"):
            script = (
                "from pathlib import Path; import sys; "
                "Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')"
            )
            return [configured_runtime, "-c", script, str(target_path), content]
        if executable in {"bash", "sh", "dash"}:
            script = (
                'python3 -c "from pathlib import Path; import sys; '
                'Path(sys.argv[1]).write_text(sys.argv[2], encoding=\'utf-8\')" "$1" "$2"'
            )
            return [configured_runtime, "-c", script, "prime-harness", str(target_path), content]
        raise ValueError("Configured shell command is not an allowed file-operation runtime")

    def _redact(self, output: str) -> str:
        redacted = output
        for key, value in (self.config.environment or {}).items():
            if value and any(
                marker in key.lower() for marker in ("secret", "token", "password", "key")
            ):
                redacted = redacted.replace(value, "[REDACTED]")
        return redacted[: 64 * 1024]

    def send_instruction(self, session: AgentSession, instruction: str) -> AgentCommandResult:
        active = self._sessions.get(session.session_id)
        if active is None:
            return AgentCommandResult(False, "", "Agent session is not active", "failed")
        normalized = instruction.strip()
        if not normalized:
            return AgentCommandResult(False, "", "Instruction is empty", "failed")
        try:
            target_match = re.search(
                (
                    r"^(?:Create|Write)\s+(?P<path>\S+)\s+containing\s+"
                    r"(?P<quote>['\"])(?P<content>.*)(?P=quote)"
                    r"(?:\s+using a Python command\.)?$"
                ),
                normalized,
                re.IGNORECASE,
            )
            if target_match:
                target_path = WorkspacePolicy(self._workspace()).resolve_relative(
                    target_match.group("path")
                )
                payload = target_match.group("content")
                process = subprocess.Popen(  # noqa: S603 - argv comes from a fixed operation template.
                    self._write_file_command(target_path, payload),
                    cwd=self._workspace(),
                    env={**os.environ, **(self.config.environment or {})},
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                active.process = process
                try:
                    output, _ = process.communicate(timeout=self.config.timeout_seconds)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    session.state = LaneState.FAILED
                    session.process_status = "timeout"
                    session.updated_at = now_utc()
                    return AgentCommandResult(
                        False,
                        "",
                        f"Agent command timed out after {self.config.timeout_seconds}s",
                        "timeout",
                    )
                safe_output = self._redact(output)
                active.output = safe_output.splitlines()[-200:]
                if process.returncode != 0:
                    session.state = LaneState.FAILED
                    session.process_status = "failed"
                    session.updated_at = now_utc()
                    return AgentCommandResult(
                        False,
                        "",
                        safe_output[-500:] or "File operation failed",
                        "failed",
                        process.returncode,
                    )
                session.state = LaneState.COMPLETED
                session.process_status = "completed"
                session.updated_at = now_utc()
                return AgentCommandResult(
                    True,
                    safe_output,
                    None,
                    "completed",
                    0,
                    [str(target_path.relative_to(self._workspace()))],
                )

            if re.search(r"(?i)\bread\b.*(?:/|\\|%2e|\\.\\.)", normalized):
                return AgentCommandResult(
                    False,
                    "",
                    "Requested operation is outside the configured workspace",
                    "failed",
                )
            return AgentCommandResult(
                False, "", "Unsupported instruction; no operation was executed", "failed"
            )
        except (OSError, ValueError) as exc:
            diagnostic = self._redact(str(exc))[:500]
            session.state = LaneState.FAILED
            session.process_status = "failed"
            session.updated_at = now_utc()
            active.output = [diagnostic]
            return AgentCommandResult(False, "", diagnostic, "failed")

    def snapshot(self, session: AgentSession) -> AgentSession:
        active = self._sessions.get(session.session_id)
        if active is not None:
            if active.process is None:
                session.process_status = active.session.process_status
            else:
                poll = active.process.poll()
                session.process_status = (
                    "completed" if poll is None else ("failed" if poll != 0 else "completed")
                )
            session.updated_at = now_utc()
        return session

    def stop_session(self, session: AgentSession) -> bool:
        active = self._sessions.get(session.session_id)
        if active is None:
            return False
        if active.process and active.process.poll() is None:
            active.process.terminate()
            try:
                active.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                active.process.kill()
                active.process.wait(timeout=5)
        session.state = LaneState.IDLE
        session.process_status = "stopped"
        session.updated_at = now_utc()
        self._sessions.pop(session.session_id, None)
        return True
