import json
from pathlib import Path

import pytest

from prime_harness.pty_session import PtySession
from prime_harness.responsive_layout import (
    clamp_window_rect,
    fractions_from_sizes,
    initial_window_rect,
    rect_fits,
    sizes_from_fractions,
)

QtCore = pytest.importorskip("PySide6.QtCore")
QRect = QtCore.QRect
QSize = QtCore.QSize

# Usable screen areas (logical pixels, taskbar already excluded) for common displays.
SCREEN_1366 = QRect(0, 0, 1366, 728)  # 1366x768 with a 40px taskbar
SCREEN_1920 = QRect(0, 0, 1920, 1032)  # 1920x1080 with a 48px taskbar
SCREEN_1920_150 = QRect(0, 0, 1280, 688)  # 1920x1080 at 150% scaling, logical pixels
SCREEN_TINY = QRect(0, 0, 1024, 600)  # deliberately below the layout minimum
MIN = QSize(1210, 398)
RESOLUTIONS = [SCREEN_1366, SCREEN_1920, SCREEN_1920_150]


# ----------------------------------------------------------- pure geometry
@pytest.mark.parametrize("area", RESOLUTIONS + [SCREEN_TINY])
def test_initial_rect_fits_usable_area(area):
    rect = initial_window_rect(area)
    assert rect_fits(rect, area)


def test_initial_rect_is_centred_and_capped_on_large_screens():
    rect = initial_window_rect(SCREEN_1920)
    assert (rect.width(), rect.height()) == (1400, 900)
    assert rect.x() == (1920 - 1400) // 2


def test_clamp_shrinks_saved_geometry_after_resolution_drop():
    saved = QRect(300, 100, 1800, 1000)  # saved on a 1920x1080 display
    clamped = clamp_window_rect(saved, SCREEN_1366, MIN)
    assert rect_fits(clamped, SCREEN_1366)
    assert clamped.width() <= 1366
    assert clamped.height() <= 728


def test_clamp_moves_offscreen_position_back_on_screen():
    clamped = clamp_window_rect(QRect(-500, -300, 900, 600), SCREEN_1366, MIN)
    assert rect_fits(clamped, SCREEN_1366)


def test_clamp_keeps_valid_geometry_unchanged():
    saved = QRect(20, 60, 1300, 600)
    assert clamp_window_rect(saved, SCREEN_1920, MIN) == saved


def test_fractions_round_trip_and_sum_exactly():
    sizes = [400, 1000, 300]
    fractions = fractions_from_sizes(sizes)
    assert abs(sum(fractions) - 1.0) < 1e-9
    assert sizes_from_fractions(fractions, sum(sizes)) == sizes


def test_proportional_sizes_respect_minimums_and_hidden_panes():
    sizes = sizes_from_fractions([0.2, 0.8], 900, minimums=[400, 100])
    assert sizes[0] >= 400
    assert sum(sizes) == 900
    hidden = sizes_from_fractions([0.5, 0.5, 0.0], 800, minimums=[100, 100, 0])
    assert hidden[2] == 0
    assert sum(hidden) == 800


# ------------------------------------------------------------- window tests
@pytest.fixture
def qt_app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _settle(qt_app, window) -> None:
    for _ in range(3):
        qt_app.processEvents()
    for panel in window.panels.values():
        panel.view.flush_grid()
    qt_app.processEvents()


def _window(tmp_path: Path, qt_app, screen: QRect | None, state_name: str = "state.json"):
    """Build and show a window on a simulated screen (or the real offscreen one)."""
    from prime_harness.desktop_app import MainWindow

    window = MainWindow(state_path=tmp_path / state_name)
    if screen is not None:
        window._screen_available = lambda point=None: screen  # simulated display
        window._apply_startup_geometry()
    window.show()
    _settle(qt_app, window)
    return window


@pytest.mark.parametrize("screen", RESOLUTIONS, ids=["1366x768", "1920x1080", "1920x1080@150%"])
def test_startup_frame_fits_screen_without_clipping(tmp_path, qt_app, screen):
    window = _window(tmp_path, qt_app, screen)
    try:
        assert rect_fits(window.frameGeometry(), screen)
        assert window.geometry().width() >= MIN.width()
        assert window.geometry().height() >= MIN.height()
        assert window._layout_ready
        assert not window.isMaximized()
        for panel in window.panels.values():
            assert panel.width() >= panel.minimumSizeHint().width()
    finally:
        window.close()


def test_screen_too_small_scrolls_instead_of_clipping(tmp_path, qt_app):
    window = _window(tmp_path, qt_app, SCREEN_TINY)
    try:
        assert rect_fits(window.frameGeometry(), SCREEN_TINY)
        assert window.centralWidget().widget() is window._root
        assert window._root.minimumSizeHint().width() > window.geometry().width()
        assert window.panels["1"].view.vt.cols >= 20
    finally:
        window.close()


def test_repeated_resizing_recalculates_grid_each_time(tmp_path, qt_app):
    window = _window(tmp_path, qt_app, SCREEN_1366)
    try:
        for width, height in [(1366, 688), (1920, 992), (1280, 646), (1024, 560), (1366, 688)]:
            window.resize(width, height)
            _settle(qt_app, window)
            for panel in window.panels.values():
                view = panel.view
                assert view.vt.cols == max(20, view.width() // view.cell_w)
                assert view.vt.rows == max(8, view.height() // view.cell_h)
    finally:
        window.close()


def test_manual_splitter_move_survives_window_resize(tmp_path, qt_app):
    window = _window(tmp_path, qt_app, SCREEN_1366)
    try:
        window.top_row.setSizes([400, 1000])
        _settle(qt_app, window)
        before = window.top_row.sizes()
        ratio_before = before[0] / sum(before)
        window.resize(1920, 992)
        _settle(qt_app, window)
        after = window.top_row.sizes()
        assert abs(after[0] / sum(after) - ratio_before) < 0.02
        assert sum(after) > sum(before)
    finally:
        window.close()


def test_maximize_then_restore_returns_to_previous_size(tmp_path, qt_app):
    window = _window(tmp_path, qt_app, SCREEN_1366)
    try:
        window.resize(1100, 640)
        _settle(qt_app, window)
        normal_width = window.width()
        window.showMaximized()
        _settle(qt_app, window)
        assert window.isMaximized()
        view = window.panels["1"].view
        assert view.vt.cols == max(20, view.width() // view.cell_w)
        window.showNormal()
        _settle(qt_app, window)
        assert not window.isMaximized()
        assert abs(window.width() - normal_width) <= 2
    finally:
        window.close()


def test_persisted_geometry_and_splitters_restore_clamped_to_current_screen(tmp_path, qt_app):
    state_file = tmp_path / "state.json"
    first = _window(tmp_path, qt_app, SCREEN_1920)
    try:
        first.resize(1600, 900)
        first.top_row.setSizes([700, 900])
        _settle(qt_app, first)
        first._persist()
    finally:
        first.close()
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["window"]["geometry"][2] == 1600
    assert set(saved["window"]["splitters"]) >= {"vertical", "top", "middle"}

    # Next launch on a 1366x768 laptop: the saved 1600x900 must clamp onto the screen.
    second = _window(tmp_path, qt_app, SCREEN_1366)
    try:
        assert rect_fits(second.frameGeometry(), SCREEN_1366)
        assert second.geometry().width() <= 1366
        top = second.top_row.sizes()
        assert min(top) > 0
        restored = fractions_from_sizes(top)[0]
        assert abs(restored - saved["window"]["splitters"]["top"][0]) < 0.05
    finally:
        second.close()


def test_terminal_panel_drives_conpty_resize_with_grid(tmp_path, qt_app, monkeypatch):
    from prime_harness import desktop_app

    created: list[object] = []

    class _FakeProcess:
        pid = 9001
        exitstatus = None

        def __init__(self):
            self.size = None
            self._alive = True

        def read(self, _n):
            import time

            time.sleep(0.01)
            return ""

        def isalive(self):
            return self._alive

        def write(self, _data):
            return None

        def setwinsize(self, rows, cols):
            self.size = (rows, cols)

        def terminate(self, force=False):
            self._alive = False

    def factory(**kwargs):
        process = _FakeProcess()
        created.append(process)
        return PtySession(
            spawner=lambda argv, cwd=None, env=None, dimensions=None: process, **kwargs
        )

    monkeypatch.setattr(desktop_app, "PtySession", factory)
    window = _window(tmp_path, qt_app, SCREEN_1366)
    try:
        panel = window.panels["1"]
        panel.profile_box.setCurrentIndex(panel.profile_box.findData("cmd"))
        assert window.start_panel("1")
        window.resize(1920, 992)
        _settle(qt_app, window)
        view = panel.view
        assert created[0].size == (view.vt.rows, view.vt.cols)
        window.resize(1280, 646)
        _settle(qt_app, window)
        assert created[0].size == (view.vt.rows, view.vt.cols)
    finally:
        window.close()
