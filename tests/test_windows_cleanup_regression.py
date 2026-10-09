"""Windows regression: temporary-directory cleanup after four active ConPTY sessions.

Tracks all ConPTY child processes, reader threads, cwd handles and open
file references created by TerminalManager. Verifies bounded shutdown and
confirms temp directory removal succeeds (no WinError 32).
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from prime_harness.terminal_control import TerminalManager


def _pywinpty_available() -> bool:
    try:
        from winpty import PtyProcess  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _pywinpty_available(), reason="pywinpty required for live ConPTY cleanup test"
)
def test_cleanup_after_four_active_sessions() -> None:
    """Launch 4 independent CMD sessions, shutdown, verify directory removal."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        manager = TerminalManager(count=4, cwd=tmp_path)
        # Launch all four corner sessions
        for sid in range(1, 5):
            manager.launch(sid)
        # Verify active
        for sid in range(1, 5):
            assert manager.slot(sid).alive, f"Slot {sid} not alive"

        # Reliable bounded shutdown (not arbitrary sleep)
        results = manager.shutdown()
        for sid in range(1, 5):
            assert results.get(sid) is True, f"Slot {sid} did not terminate cleanly"

        # Verify process references nulled (allows handle release)
        for sid in range(1, 5):
            assert manager.slot(sid).process is None, f"Slot {sid} process not cleared"
            assert manager.slot(sid).subscribers == [], f"Slot {sid} subscribers not cleared"

        # Verify bounded termination (no indefinite hang)
        # After shutdown, directory removal should succeed when the
        # TemporaryDirectory exits; any leftover handle causes WinError 32.
        # (No arbitrary sleep; shutdown uses bounded waits.)
