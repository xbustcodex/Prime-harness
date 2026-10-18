import os
import shutil
import time

import pytest

from prime_harness.profiles import resolve_argv
from prime_harness.pty_session import PtySession, SessionState
from prime_harness.vt_engine import VtScreen

WINDOWS = os.name == "nt"


def _wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(not WINDOWS, reason="ConPTY requires Windows")
def test_live_conpty_cmd_round_trip_and_exit_code():
    session = PtySession(
        "live-cmd", ["cmd.exe", "/d", "/c", "echo PH_LIVE_OK && exit 3"], rows=24, cols=100
    )
    assert session.start(), session.error
    vt = VtScreen(24, 100)
    assert _wait_for(lambda: session.state is SessionState.EXITED, 15)
    vt.feed(session.drain())
    assert any("PH_LIVE_OK" in line for line in vt.active.display)
    assert session.exit_code == 3


@pytest.mark.skipif(not WINDOWS, reason="ConPTY requires Windows")
def test_live_interactive_input_is_echoed_through_vt_engine():
    session = PtySession("live-input", ["cmd.exe", "/d", "/q"], rows=24, cols=100)
    assert session.start(), session.error
    vt = VtScreen(24, 100)
    try:
        time.sleep(1.0)
        session.write("echo PH_TYPED_42\r")
        found = _wait_for(
            lambda: (
                (vt.feed(session.drain()) or True)
                and any("PH_TYPED_42" in line for line in vt.active.display)
            ),
            10,
        )
        assert found
    finally:
        assert session.stop()


@pytest.mark.skipif(not WINDOWS, reason="ConPTY requires Windows")
def test_live_window_resize_reaches_conpty_grid(monkeypatch, tmp_path):
    """Real ConPTY: each window resize updates the child's reported console size."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from prime_harness import desktop_app
    from prime_harness.persistence import AppState

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = desktop_app.MainWindow(state_path=tmp_path / "state.json", state=AppState())
    window.show()
    try:
        panel = window.panels["1"]
        panel.profile_box.setCurrentIndex(panel.profile_box.findData("cmd"))
        assert window.start_panel("1"), panel.status.text()
        for width, height in [(1000, 600), (1400, 800)]:
            window.resize(width, height)
            for _ in range(3):
                app.processEvents()
            panel.view.flush_grid()
            time.sleep(0.2)
            expected = (panel.view.vt.rows, panel.view.vt.cols)
            assert panel.session is not None and panel.session.running
            assert tuple(panel.session._process.getwinsize()) == expected
    finally:
        window.close()


@pytest.mark.skipif(not WINDOWS, reason="ConPTY requires Windows")
def test_live_stop_terminates_whole_process_tree():
    session = PtySession("live-stop", ["cmd.exe", "/d", "/q"], rows=24, cols=80)
    assert session.start(), session.error
    time.sleep(0.5)
    assert session.stop(timeout=5)
    assert session.state is SessionState.EXITED


@pytest.mark.skipif(not WINDOWS, reason="ConPTY requires Windows")
@pytest.mark.skipif(shutil.which("omp") is None, reason="OMP is not on PATH")
def test_live_omp_renders_full_screen_interface():
    argv, error = resolve_argv(["omp"])
    assert not error
    session = PtySession("live-omp", argv, rows=40, cols=120)
    assert session.start(), session.error
    vt = VtScreen(40, 120)
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            vt.feed(session.drain())
            if vt.in_alt and any(ch in "┌┐└┘│─" for line in vt.active.display for ch in line):
                break
            time.sleep(0.1)
        assert vt.in_alt, "OMP did not enter the alternate screen"
        assert any(ch in "┌┐└┘│─" for line in vt.active.display for ch in line), (
            "OMP box-drawing borders not rendered"
        )
    finally:
        session.stop(timeout=5)


def test_desktop_window_layout_and_routing(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from prime_harness.desktop_app import MainWindow

    app = QApplication.instance() or QApplication([])
    state_path = tmp_path / "state.json"
    window = MainWindow(state_path=state_path)
    window.show()
    app.processEvents()
    try:
        assert {"1", "2", "3", "4"} <= set(window.panels)
        assert window.top_row.count() == 2
        assert window.middle_row.count() == 3
        assert window.middle_row.widget(1) is window.chat
        new_id = window.add_terminal()
        assert new_id == "5"
        window.handle_chat("/to 5")
        assert window.supervisor.target == "5"
        window.handle_chat("/help")
        assert any("Commands:" in line for line in window.supervisor.history)
        window.handle_chat("/approve 99")
        assert any("not awaiting approval" in line for line in window.supervisor.history)
        window.resize(1000, 700)
        app.processEvents()
        window.panels["1"].view.sync_grid()
        assert window.panels["1"].view.vt.cols >= 20
        assert state_path.exists()
    finally:
        window.close()
    reopened = MainWindow(state_path=state_path)
    try:
        assert "5" in reopened.panels
        assert reopened.panels["1"].profile_box.currentData() == "omp"
    finally:
        reopened.close()
