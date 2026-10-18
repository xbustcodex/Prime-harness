"""External-chat connectivity for the Prime Harness desktop control room.

This module is the pairing and event layer behind authenticated external
access (phone, laptop, or an MCP-capable chat client):

* :class:`PairingManager` issues one revocable pairing link and token per
  connection. Only the SHA-256 hash of a token is stored; the token itself is
  shown once. A token is bound to one terminal (or to supervisor scope) and to
  an explicit permission set.
* :class:`ControlRoom` keeps every terminal's bridge separate: rendered
  output tail, status, and events. Commands from external clients are routed
  through the existing :class:`~prime_harness.supervisor.Supervisor`, so per
  terminal queues, approval gating, and pause/resume apply unchanged. Contexts
  are never merged across terminals.

Everything here is thread-safe: HTTP worker threads publish and read while the
desktop UI pump delivers commands.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from prime_harness.supervisor import QueuedCommand, Supervisor

ALL_PERMISSIONS = frozenset({"status", "output", "command", "approve"})
DEFAULT_PERMISSIONS = frozenset({"status", "output", "command", "approve"})
OUTPUT_TAIL_LINES = 200
EVENT_LIMIT = 2000


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class PairingRecord:
    token_hash: str
    terminal_id: str | None  # None = supervisor scope: sees all, commands need explicit target
    permissions: frozenset[str]
    label: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return {
            "terminal_id": self.terminal_id,
            "permissions": sorted(self.permissions),
            "label": self.label,
            "created_at": self.created_at,
        }


@dataclass
class ExternalEvent:
    cursor: int
    kind: str
    terminal_id: str | None
    message: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return {
            "cursor": self.cursor,
            "kind": self.kind,
            "terminal_id": self.terminal_id,
            "message": self.message,
            "created_at": self.created_at,
        }


class PairingManager:
    """Issues and revokes pairing tokens. Tokens are shown once; hashes are kept."""

    def __init__(self, public_base: str) -> None:
        self.public_base = public_base.rstrip("/")
        self._records: dict[str, PairingRecord] = {}
        self._lock = threading.RLock()

    def pair(
        self,
        terminal_id: str | None,
        permissions: frozenset[str] | set[str] | None = None,
        label: str = "",
    ) -> tuple[str, str]:
        """Create one pairing. Returns ``(token, pairing_url)``; the token is shown once."""
        perms = frozenset(permissions) if permissions is not None else DEFAULT_PERMISSIONS
        unknown = perms - ALL_PERMISSIONS
        if unknown:
            raise ValueError(f"Unknown permissions: {sorted(unknown)}")
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._records[_digest(token)] = PairingRecord(
                token_hash=_digest(token), terminal_id=terminal_id, permissions=perms, label=label
            )
        return token, f"{self.public_base}/pair/{token}"

    def authenticate(self, token: str | None) -> PairingRecord | None:
        if not token:
            return None
        with self._lock:
            return self._records.get(_digest(token))

    def revoke_token(self, token: str) -> bool:
        with self._lock:
            return self._records.pop(_digest(token), None) is not None

    def revoke_terminal(self, terminal_id: str) -> int:
        """Revoke every pairing bound to one terminal. Returns the count revoked."""
        with self._lock:
            doomed = [
                key for key, record in self._records.items() if record.terminal_id == terminal_id
            ]
            for key in doomed:
                del self._records[key]
            return len(doomed)

    def revoke_all(self) -> int:
        with self._lock:
            count = len(self._records)
            self._records.clear()
            return count

    def records(self) -> list[PairingRecord]:
        with self._lock:
            return list(self._records.values())


class ControlRoom:
    """Facade over Supervisor plus per-terminal bridges and cursor-based events."""

    def __init__(self, supervisor: Supervisor, public_base: str = "http://127.0.0.1:8420") -> None:
        self.supervisor = supervisor
        self.pairings = PairingManager(public_base)
        self._outputs: dict[str, deque[str]] = {}
        self._statuses: dict[str, str] = {}
        self._events: deque[ExternalEvent] = deque(maxlen=EVENT_LIMIT)
        self._seq = 0
        self._cond = threading.Condition()
        self._lock = threading.RLock()

    # ---------------------------------------------------------- terminals
    def register_terminal(self, terminal_id: str) -> None:
        with self._lock:
            self._outputs.setdefault(terminal_id, deque(maxlen=OUTPUT_TAIL_LINES))
            self._statuses.setdefault(terminal_id, "not started")

    def unregister_terminal(self, terminal_id: str) -> None:
        with self._lock:
            self._outputs.pop(terminal_id, None)
            self._statuses.pop(terminal_id, None)
        self.pairings.revoke_terminal(terminal_id)
        self.publish("terminal_removed", terminal_id, f"Terminal {terminal_id} removed")

    def set_status(self, terminal_id: str, status: str) -> None:
        with self._lock:
            self._statuses[terminal_id] = status

    def status(self, terminal_id: str) -> str:
        with self._lock:
            return self._statuses.get(terminal_id, "unknown")

    def publish_output(self, terminal_id: str, rendered_text: str) -> None:
        """Store the current rendered screen tail (already VT-decoded).

        Replaces the stored tail: callers publish the whole visible screen,
        and accumulation would duplicate identical snapshots.
        """
        with self._lock:
            tail = self._outputs.setdefault(terminal_id, deque(maxlen=OUTPUT_TAIL_LINES))
            if not rendered_text:
                return
            tail.clear()
            tail.extend(rendered_text.splitlines()[-OUTPUT_TAIL_LINES:])

    def output(self, terminal_id: str, lines: int = 40) -> list[str]:
        with self._lock:
            tail = list(self._outputs.get(terminal_id, ()))
        return tail[-max(1, min(lines, OUTPUT_TAIL_LINES)) :]

    def sessions(self) -> list[dict]:
        """Session discovery: one entry per registered terminal, contexts kept separate."""
        with self._lock:
            statuses = dict(self._statuses)
        result = []
        for session_id in sorted(self.supervisor.sessions):
            record = self.supervisor.sessions[session_id]
            queued = self.supervisor.queue_for(session_id)
            result.append(
                {
                    "terminal_id": session_id,
                    "label": record.label,
                    "status": statuses.get(session_id, "unknown"),
                    "paused": record.paused,
                    "queued_commands": len(queued),
                }
            )
        return result

    # ------------------------------------------------------------- events
    def publish(self, kind: str, terminal_id: str | None, message: str) -> ExternalEvent:
        with self._cond:
            with self._lock:
                self._seq += 1
                event = ExternalEvent(self._seq, kind, terminal_id, message)
                self._events.append(event)
            self._cond.notify_all()
            return event

    def events(
        self, since: int = 0, terminal_id: str | None = None, wait: float = 0.0
    ) -> tuple[list[ExternalEvent], int]:
        """Events after cursor ``since``; blocks up to ``wait`` seconds when empty."""
        deadline = None
        with self._cond:
            while True:
                with self._lock:
                    snapshot = list(self._events)
                    latest = self._seq
                fresh = [e for e in snapshot if e.cursor > since]
                if terminal_id is not None:
                    fresh = [e for e in fresh if e.terminal_id == terminal_id]
                if fresh or wait <= 0:
                    return fresh, latest
                if deadline is None:
                    import time

                    deadline = time.monotonic() + wait
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return [], latest
                self._cond.wait(remaining)

    # ----------------------------------------------------------- commands
    def submit_command(self, terminal_id: str, text: str, actor: str = "external") -> QueuedCommand:
        """Queue one command for one explicit terminal. Never a broadcast."""
        text = text.strip()
        if not text:
            raise ValueError("Empty command")
        command = self.supervisor.enqueue(terminal_id, text)
        self.publish(
            "command_queued",
            terminal_id,
            f"{actor} queued #{command.command_id}"
            + (" (needs approval)" if command.consequential else ""),
        )
        return command

    def approve(self, command_id: int, actor: str = "external") -> bool:
        command = self.supervisor.find(command_id)
        if command is None:
            return False
        ok = self.supervisor.approve(command_id)
        if ok:
            self.publish("command_approved", command.session_id, f"{actor} approved #{command_id}")
        return ok

    def reject(self, command_id: int, actor: str = "external") -> bool:
        command = self.supervisor.find(command_id)
        if command is None:
            return False
        ok = self.supervisor.reject(command_id)
        if ok:
            self.publish("command_rejected", command.session_id, f"{actor} rejected #{command_id}")
        return ok

    def note_delivered(self, terminal_id: str, command: QueuedCommand) -> None:
        self.publish("command_delivered", terminal_id, f"delivered #{command.command_id}")
