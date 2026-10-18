"""Supervisor model: routing, per-session queues, approvals, notifications.

Commands are always addressed to one explicit session. There is no broadcast,
and a session's queue is never shared with another session. Consequential
commands wait for explicit approval before delivery.

Pause/resume controls only the delivery of queued commands from this
application to the agent. It does not suspend the agent process; the
supervisor never claims otherwise.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from datetime import datetime

CONSEQUENTIAL_PATTERN = re.compile(
    r"\b(rm|del|rmdir|erase|Remove-Item|format|shutdown|reboot|git\s+(push|reset|clean|checkout)"
    r"|drop\s+(table|database)|reg\s+delete|diskpart)\b|\|\s*(sh|bash|iex|powershell)\b",
    re.IGNORECASE,
)


@dataclass
class QueuedCommand:
    command_id: int
    session_id: str
    text: str
    consequential: bool
    status: str = "awaiting approval"  # awaiting approval | queued | sent | rejected
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


@dataclass
class Notification:
    session_id: str
    kind: str  # completed | failed | info
    message: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    read: bool = False


@dataclass
class SessionRecord:
    session_id: str
    label: str
    paused: bool = False


class Supervisor:
    HISTORY_LIMIT = 500

    def __init__(self) -> None:
        self.sessions: dict[str, SessionRecord] = {}
        self.commands: list[QueuedCommand] = []
        self.notifications: list[Notification] = []
        self.history: list[str] = []
        self.target: str | None = None
        self._ids = itertools.count(1)

    # ------------------------------------------------------------ sessions
    def register(self, session_id: str, label: str) -> None:
        self.sessions[session_id] = SessionRecord(session_id, label)
        if self.target is None:
            self.target = session_id

    def unregister(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
        if self.target == session_id:
            self.target = next(iter(self.sessions), None)

    def select(self, session_id: str) -> bool:
        if session_id not in self.sessions:
            return False
        self.target = session_id
        return True

    def set_paused(self, session_id: str, paused: bool) -> None:
        self.sessions[session_id].paused = paused

    # ------------------------------------------------------------- commands
    def enqueue(self, session_id: str, text: str) -> QueuedCommand:
        if session_id not in self.sessions:
            raise KeyError(f"Unknown session {session_id}")
        consequential = bool(CONSEQUENTIAL_PATTERN.search(text))
        command = QueuedCommand(
            command_id=next(self._ids),
            session_id=session_id,
            text=text,
            consequential=consequential,
            status="awaiting approval" if consequential else "queued",
        )
        self.commands.append(command)
        self.log(f"[{session_id}] queued #{command.command_id}: {text}")
        if consequential:
            self.log(f"#{command.command_id} needs approval (/approve {command.command_id})")
        return command

    def find(self, command_id: int) -> QueuedCommand | None:
        return next((c for c in self.commands if c.command_id == command_id), None)

    def approve(self, command_id: int) -> bool:
        command = self.find(command_id)
        if command is None or command.status != "awaiting approval":
            return False
        command.status = "queued"
        self.log(f"#{command_id} approved")
        return True

    def reject(self, command_id: int) -> bool:
        command = self.find(command_id)
        if command is None or command.status != "awaiting approval":
            return False
        command.status = "rejected"
        self.log(f"#{command_id} rejected")
        return True

    def pending_approvals(self) -> list[QueuedCommand]:
        return [c for c in self.commands if c.status == "awaiting approval"]

    def take_deliverable(self, session_id: str) -> list[QueuedCommand]:
        """Approved commands for one session, in order. Empty while paused."""
        record = self.sessions.get(session_id)
        if record is None or record.paused:
            return []
        ready = [c for c in self.commands if c.session_id == session_id and c.status == "queued"]
        for command in ready:
            command.status = "sent"
        return ready

    def queue_for(self, session_id: str) -> list[QueuedCommand]:
        return [
            c
            for c in self.commands
            if c.session_id == session_id and c.status in {"awaiting approval", "queued"}
        ]

    # --------------------------------------------------------- notifications
    def notify(self, session_id: str, kind: str, message: str) -> None:
        self.notifications.append(Notification(session_id, kind, message))
        self.log(f"[{session_id}] {kind}: {message}")

    def unread_count(self) -> int:
        return sum(1 for n in self.notifications if not n.read)

    def mark_all_read(self) -> None:
        for notification in self.notifications:
            notification.read = True

    # ---------------------------------------------------------------- chat
    def log(self, line: str) -> None:
        self.history.append(line)
        if len(self.history) > self.HISTORY_LIMIT:
            del self.history[: len(self.history) - self.HISTORY_LIMIT]

    def parse_chat(self, line: str) -> tuple[str, str]:
        """Interpret one chat line. Returns (action, argument)."""
        stripped = line.strip()
        if not stripped.startswith("/"):
            return "send", stripped
        head, _, rest = stripped[1:].partition(" ")
        return head.lower(), rest.strip()


CHAT_HELP = (
    "Commands: /to <id> (select target) · /send <text> · /approve <cmd#> · /reject <cmd#>\n"
    "/queue · /review [id] · /pause [id] · /resume [id] · /stop [id] · /restart [id] · /status\n"
    "Plain text goes to the selected target only. Pause stops delivery of queued commands;\n"
    "it does not suspend the agent process."
)
