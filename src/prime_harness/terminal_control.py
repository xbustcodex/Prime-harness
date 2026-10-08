"""Windows ConPTY-backed terminal sessions, independent of coding-agent providers."""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

try:
    from winpty import PtyProcess
except ImportError:  # Linux imports remain usable for tests
    PtyProcess = None  # type: ignore[assignment,misc]


@dataclass
class TerminalSlot:
    slot_id: int
    process: object | None = None
    token_hash: str = field(default="", repr=False)
    recent: deque[str] = field(default_factory=lambda: deque(maxlen=300))
    subscribers: list[object] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def alive(self) -> bool:
        return self.process is not None and bool(self.process.isalive())  # type: ignore[union-attr]


class TerminalManager:
    """Independent CMD sessions, owned by the host rather than any UI."""

    def __init__(self, count: int = 4, cwd: Path | None = None):
        self.cwd = str((cwd or Path.home()).resolve())
        self.slots = {i: TerminalSlot(i) for i in range(1, count + 1)}
        self._slots_lock = threading.RLock()

    def add_slot(self) -> int:
        """Allocate another independent terminal; caller launches and pairs it."""
        with self._slots_lock:
            slot_id = max(self.slots, default=0) + 1
            self.slots[slot_id] = TerminalSlot(slot_id)
            return slot_id

    def slot(self, slot_id: int) -> TerminalSlot:
        if slot_id not in self.slots:
            raise KeyError("Unknown terminal slot")
        return self.slots[slot_id]

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def rotate_token(self, slot_id: int) -> str:
        slot = self.slot(slot_id)
        token = secrets.token_urlsafe(32)
        with slot.lock:
            slot.token_hash = self._digest(token)
        return token

    def authorized(self, slot_id: int, token: str) -> bool:
        slot = self.slot(slot_id)
        with slot.lock:
            return bool(slot.token_hash) and secrets.compare_digest(
                slot.token_hash, self._digest(token)
            )

    def launch(self, slot_id: int) -> None:
        slot = self.slot(slot_id)
        if os.name != "nt" or PtyProcess is None:
            raise RuntimeError("Windows and pywinpty are required")
        with slot.lock:
            if slot.alive:
                return
            # Use the system CMD, not an agent-specific binary or shell adapter.
            cmd = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
            slot.process = PtyProcess.spawn(
                cmd, cwd=self.cwd, dimensions=(28, 100)
            )
            slot.recent.clear()
            threading.Thread(target=self._reader, args=(slot,), daemon=True).start()

    @staticmethod
    def _reader(slot: TerminalSlot) -> None:
        process = slot.process
        while process is not None:
            try:
                chunk = process.read(4096)  # type: ignore[union-attr]
            except (EOFError, OSError):
                break
            if not chunk:
                break
            with slot.lock:
                slot.recent.append(chunk)
                for queue in list(slot.subscribers):
                    try:
                        queue.put_nowait(chunk)  # type: ignore[union-attr]
                    except Exception:
                        pass

    def write(self, slot_id: int, data: str) -> None:
        slot = self.slot(slot_id)
        if not slot.alive:
            raise RuntimeError("Terminal is not running")
        if len(data) > 8192:
            raise ValueError("Input too long")
        with slot.lock:
            slot.process.write(data)  # type: ignore[union-attr]

    def resize(self, slot_id: int, rows: int, cols: int) -> None:
        if not 5 <= rows <= 200 or not 20 <= cols <= 300:
            raise ValueError("Invalid terminal size")
        slot = self.slot(slot_id)
        if slot.alive:
            with slot.lock:
                slot.process.setwinsize(rows, cols)  # type: ignore[union-attr]
