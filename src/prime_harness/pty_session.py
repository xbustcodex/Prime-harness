"""One independent ConPTY session per embedded terminal panel.

Each session owns its own pseudo-console, process tree, working directory,
environment copy, and output queue. Nothing is shared between sessions.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

try:
    from winpty import PtyProcess
except ImportError:  # non-Windows environments: tests use fakes
    PtyProcess = None  # type: ignore[assignment,misc]


class SessionState(StrEnum):
    NOT_STARTED = "not started"
    RUNNING = "running"
    EXITED = "exited"
    FAILED = "failed"


@dataclass
class PtySession:
    session_id: str
    argv: Sequence[str]
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    rows: int = 24
    cols: int = 80
    # Test seam: a factory returning an object with the PtyProcess interface.
    spawner: object | None = None

    state: SessionState = SessionState.NOT_STARTED
    exit_code: int | None = None
    error: str = ""
    _process: Any = None  # duck-typed pywinpty PtyProcess handle
    _chunks: queue.Queue[str] = field(default_factory=queue.Queue)
    _reader: threading.Thread | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def pid(self) -> int | None:
        return getattr(self._process, "pid", None) if self._process else None

    @property
    def running(self) -> bool:
        return self.state is SessionState.RUNNING

    def start(self) -> bool:
        """Spawn the process in a new ConPTY. Returns False and sets error on failure."""
        with self._lock:
            if self.running:
                return True
            environment = dict(os.environ)
            environment.update(self.env)
            try:
                if self.spawner is not None:
                    process = self.spawner(  # type: ignore[operator]
                        list(self.argv),
                        cwd=self.cwd,
                        env=environment,
                        dimensions=(self.rows, self.cols),
                    )
                else:
                    if PtyProcess is None:
                        raise RuntimeError("pywinpty is required for ConPTY sessions")
                    process = PtyProcess.spawn(
                        list(self.argv),
                        cwd=self.cwd,
                        env=environment,
                        dimensions=(self.rows, self.cols),
                    )
            except Exception as exc:  # noqa: BLE001 - surfaced to the operator
                self.state = SessionState.FAILED
                self.error = f"Launch failed: {exc}"
                return False
            self._process = process
            self.exit_code = None
            self.error = ""
            self.state = SessionState.RUNNING
            self._reader = threading.Thread(
                target=self._read_loop, name=f"pty-{self.session_id}", daemon=True
            )
            self._reader.start()
            return True

    def _read_loop(self) -> None:
        process = self._process
        while True:
            try:
                chunk = process.read(4096)  # type: ignore[union-attr]
            except (EOFError, OSError):
                break
            if not chunk:
                # pywinpty returns '' on a transient read; stop only when the child is gone.
                if not self._alive(process):
                    break
                time.sleep(0.01)
                continue
            self._chunks.put(chunk)
        self._finish(process)

    def _finish(self, process: object) -> None:
        code = None
        try:
            code = getattr(process, "exitstatus", None)
        except Exception:  # noqa: BLE001
            code = None
        with self._lock:
            if self._process is process:
                self.exit_code = code if isinstance(code, int) else None
                self.state = SessionState.EXITED

    @staticmethod
    def _alive(process: object) -> bool:
        try:
            return bool(process.isalive())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return False

    def drain(self) -> str:
        """All output received since the last drain, in order."""
        parts: list[str] = []
        while True:
            try:
                parts.append(self._chunks.get_nowait())
            except queue.Empty:
                break
        return "".join(parts)

    def write(self, data: str) -> bool:
        with self._lock:
            if not self.running or self._process is None:
                return False
            try:
                self._process.write(data)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                self.error = f"Input failed: {exc}"
                return False
            return True

    def resize(self, rows: int, cols: int) -> None:
        if not (2 <= rows <= 500 and 2 <= cols <= 1000):
            return
        self.rows, self.cols = rows, cols
        with self._lock:
            if self.running and self._process is not None:
                try:
                    self._process.setwinsize(rows, cols)  # type: ignore[attr-defined]
                except Exception as exc:  # noqa: BLE001
                    self.error = f"Resize failed: {exc}"

    def stop(self, timeout: float = 3.0) -> bool:
        """Terminate the whole process tree. Returns True once the child is gone."""
        with self._lock:
            process = self._process
            if process is None or not self._alive(process):
                return True
            pid = getattr(process, "pid", None)
            if os.name == "nt" and pid:
                taskkill = os.path.join(
                    os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe"
                )
                try:
                    # Fixed argv, integer PID, no shell: no untrusted input is interpolated.
                    subprocess.run(  # noqa: S603
                        [taskkill, "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        timeout=timeout,
                        check=False,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    self.error = f"taskkill failed: {exc}"
            try:
                process.terminate(force=True)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                self.error = f"Terminate failed: {exc}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self._alive(process):
            time.sleep(0.05)
        gone = not self._alive(process)
        if gone:
            self._finish(process)
        return gone
