"""Prime Harness native Windows desktop application (Qt).

Layout:

    [ Terminal 1 ][ Terminal 2 ]
    [ Terminal 3 ][ Supervisor ][ Terminal 4 ]
    [ extra terminals added with + Add Terminal ]
    [ + Add Terminal | Sessions | Notifications ]

Every terminal panel is a real embedded VT surface (prime_harness.vt_engine)
fed by its own ConPTY session (prime_harness.pty_session). OMP, PrimePi,
PowerShell, Command Prompt and custom profiles run unmodified.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from prime_harness.control_room import ControlRoom
from prime_harness.mcp_api import ApiServer, api_settings
from prime_harness.persistence import (
    AppState,
    PanelConfig,
    default_state_path,
    load_state,
    save_state,
)
from prime_harness.profiles import LaunchProfile, find_profile, resolve_argv
from prime_harness.pty_session import PtySession, SessionState
from prime_harness.responsive_layout import (
    clamp_window_rect,
    fractions_from_sizes,
    initial_window_rect,
    sizes_from_fractions,
)
from prime_harness.supervisor import CHAT_HELP, Supervisor
from prime_harness.vt_engine import VtScreen, encode_key, encode_mouse, encode_paste, encode_text

PUMP_INTERVAL_MS = 30
# Smallest character grid that still shows an agent UI legibly.
MIN_GRID_COLS = 20
MIN_GRID_ROWS = 8
# Settle time before a drag-resize is applied to the ConPTY.
GRID_DEBOUNCE_MS = 60
# Typical window-frame margins (left, top, right, bottom); used until the window is shown.
FRAME_ESTIMATE = (8, 31, 8, 8)
# Default pane proportions for each splitter, used when no layout was saved.
DEFAULT_SPLITTER_FRACTIONS: dict[str, list[float]] = {
    "vertical": [0.5, 0.5, 0.0],
    "top": [0.5, 0.5],
    "middle": [1 / 3, 1 / 3, 1 / 3],
}
SPLITTER_NAMES = ("vertical", "top", "middle", "extra")
DEFAULT_PANEL_PROFILES = ("omp", "primepi", "powershell", "cmd")
FIXED_PANEL_IDS = ("1", "2", "3", "4")

DEFAULT_FG = "#d4d4d4"
DEFAULT_BG = "#1e1e1e"
ANSI_COLORS = {
    "black": "#000000",
    "red": "#cd3131",
    "green": "#0dbc79",
    "brown": "#e5e510",
    "yellow": "#e5e510",
    "blue": "#2472c8",
    "magenta": "#bc3fbc",
    "cyan": "#11a8cd",
    "white": "#e5e5e5",
    "brightblack": "#666666",
    "brightred": "#f14c4c",
    "brightgreen": "#23d18b",
    "brightbrown": "#f5f543",
    "brightyellow": "#f5f543",
    "brightblue": "#3b8eea",
    "brightmagenta": "#d670d6",
    "brightcyan": "#29b8db",
    "brightwhite": "#ffffff",
}

_KEY_NAMES: dict[int, str] = {
    int(Qt.Key.Key_Return): "Enter",
    int(Qt.Key.Key_Enter): "Enter",
    int(Qt.Key.Key_Backspace): "Backspace",
    int(Qt.Key.Key_Tab): "Tab",
    int(Qt.Key.Key_Backtab): "Backtab",
    int(Qt.Key.Key_Escape): "Escape",
    int(Qt.Key.Key_Up): "Up",
    int(Qt.Key.Key_Down): "Down",
    int(Qt.Key.Key_Left): "Left",
    int(Qt.Key.Key_Right): "Right",
    int(Qt.Key.Key_Home): "Home",
    int(Qt.Key.Key_End): "End",
    int(Qt.Key.Key_Insert): "Insert",
    int(Qt.Key.Key_Delete): "Delete",
    int(Qt.Key.Key_PageUp): "PageUp",
    int(Qt.Key.Key_PageDown): "PageDown",
}
for _index in range(1, 13):
    _KEY_NAMES[int(getattr(Qt.Key, f"Key_F{_index}"))] = f"F{_index}"

_MOUSE_BUTTONS = {
    Qt.MouseButton.LeftButton: 0,
    Qt.MouseButton.MiddleButton: 1,
    Qt.MouseButton.RightButton: 2,
}


def _resolve_color(name: str, default: str | None) -> QColor | None:
    if not name or name == "default":
        return QColor(default) if default else None
    if name in ANSI_COLORS:
        return QColor(ANSI_COLORS[name])
    if len(name) == 6:
        color = QColor(f"#{name}")
        if color.isValid():
            return color
    return QColor(default) if default else None


class TerminalView(QWidget):
    """Embedded VT surface: paints the grid, sends keyboard, mouse and paste input."""

    focused_panel = Signal(str)

    def __init__(self, panel_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.panel_id = panel_id
        self.vt = VtScreen(24, 80)
        self.session: PtySession | None = None
        self.placeholder = ""
        font = QFont("Cascadia Mono")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
        font.setPointSize(10)
        self._font = font
        self._bold = QFont(font)
        self._bold.setBold(True)
        metrics = QFontMetrics(font)
        self.cell_w = max(1, metrics.horizontalAdvance("M"))
        self.cell_h = max(1, metrics.height())
        self._ascent = metrics.ascent()
        self._button_down = False
        # Mouse-drag selection (used when the application has not enabled mouse tracking).
        self._selection: tuple[tuple[int, int], tuple[int, int]] | None = None
        self._selecting = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(self.cell_w * MIN_GRID_COLS, self.cell_h * MIN_GRID_ROWS)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        # Drag-resizing fires many resize events; the ConPTY is resized once it settles.
        self._grid_timer = QTimer(self)
        self._grid_timer.setSingleShot(True)
        self._grid_timer.setInterval(GRID_DEBOUNCE_MS)
        self._grid_timer.timeout.connect(self.sync_grid)

    # ------------------------------------------------------------- grid
    def sync_grid(self) -> None:
        """Rows and columns come from the rendered character grid, not pixels.

        Updates the VT grid and calls the real ConPTY resize when the count changes.
        """
        cols = max(MIN_GRID_COLS, self.width() // self.cell_w)
        rows = max(MIN_GRID_ROWS, self.height() // self.cell_h)
        if (rows, cols) == (self.vt.rows, self.vt.cols):
            return
        self.vt.resize(rows, cols)
        if self.session is not None:
            self.session.resize(rows, cols)
        self.update()

    def flush_grid(self) -> None:
        """Apply a pending grid change immediately (used at settle points and in tests)."""
        self._grid_timer.stop()
        self.sync_grid()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._grid_timer.start()

    # ------------------------------------------------------------ paint
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(DEFAULT_BG))
        if self.session is None and self.placeholder:
            painter.setPen(QColor("#888888"))
            painter.setFont(self._font)
            painter.drawText(
                QRectF(8, 8, self.width() - 16, self.height() - 16),
                int(Qt.TextFlag.TextWordWrap),
                self.placeholder,
            )
            painter.end()
            return
        painter.setFont(self._font)
        cw, ch = self.cell_w, self.cell_h
        for row_index, row in enumerate(self.vt.render_rows()):
            y = row_index * ch
            for col_index, cell in enumerate(row):
                x = col_index * cw
                if cell.reverse:
                    fg = _resolve_color(cell.bg, DEFAULT_BG) or QColor(DEFAULT_BG)
                    bg = _resolve_color(cell.fg, DEFAULT_FG)
                else:
                    fg = _resolve_color(cell.fg, DEFAULT_FG) or QColor(DEFAULT_FG)
                    bg = _resolve_color(cell.bg, None)
                if bg is not None:
                    painter.fillRect(x, y, cw, ch, bg)
                if cell.data and cell.data != " ":
                    painter.setPen(fg)
                    painter.setFont(self._bold if cell.bold else self._font)
                    painter.drawText(QPointF(x, y + self._ascent), cell.data)
                if cell.underscore:
                    painter.setPen(fg)
                    painter.drawLine(x, y + ch - 1, x + cw - 1, y + ch - 1)
        range_ = self._normalize_selection()
        if range_ is not None:
            (sel_r1, sel_c1), (sel_r2, sel_c2) = range_
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 50))
            for sel_row in range(sel_r1, min(sel_r2 + 1, self.vt.rows)):
                start = sel_c1 if sel_row == sel_r1 else 0
                end = (sel_c2 + 1) if sel_row == sel_r2 else self.vt.cols
                painter.drawRect(start * cw, sel_row * ch, (end - start) * cw, ch)
            painter.restore()
        cursor_row, cursor_col, cursor_visible = self.vt.cursor
        if cursor_visible and self.hasFocus():
            painter.fillRect(cursor_col * cw, cursor_row * ch, cw, ch, QColor(200, 200, 200, 110))
        painter.setPen(QColor("#3b8eea") if self.hasFocus() else QColor("#333333"))
        painter.drawRect(0, 0, self.width() - 1, self.height() - 1)
        painter.end()

    # ------------------------------------------------------------ focus
    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.focused_panel.emit(self.panel_id)
        self.update()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.update()

    # ------------------------------------------------------------ input
    def send(self, data: str) -> None:
        if data and self.session is not None and self.session.running:
            self.vt.scroll_to_bottom()
            self.session.write(data)

    def paste_from_clipboard(self) -> None:
        text = QApplication.clipboard().text()
        if text:
            self.send(encode_paste(text, bracketed=self.vt.bracketed_paste))

    def copy_visible(self) -> None:
        QApplication.clipboard().setText(self.vt.plain_text(self.vt.rows))

    # ---------------------------------------------------------- selection
    def _normalize_selection(self) -> tuple[tuple[int, int], tuple[int, int]] | None:
        """Selection as ((start_row, start_col), (end_row, end_col)), start <= end."""
        if self._selection is None:
            return None
        (r1, c1), (r2, c2) = self._selection
        return ((r2, c2), (r1, c1)) if (r2, c2) < (r1, c1) else ((r1, c1), (r2, c2))

    def selected_text(self) -> str:
        """Text inside the current selection, rows joined, trailing blanks trimmed."""
        range_ = self._normalize_selection()
        if range_ is None:
            return ""
        (r1, c1), (r2, c2) = range_
        rows = self.vt.render_rows()
        lines: list[str] = []
        for row_index in range(r1, min(r2 + 1, len(rows))):
            start = c1 if row_index == r1 else 0
            end = (c2 + 1) if row_index == r2 else self.vt.cols  # end column inclusive
            lines.append("".join(cell.data for cell in rows[row_index][start:end]).rstrip())
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def clear_selection(self) -> None:
        if self._selection is not None:
            self._selection = None
            self.update()

    def copy_selection(self) -> bool:
        """Copy the selection to the clipboard. False when there is no selection."""
        text = self.selected_text()
        if not text:
            return False
        QApplication.clipboard().setText(text)
        return True

    def keyPressEvent(self, event) -> None:  # noqa: N802
        modifiers = event.modifiers()
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)
        key = event.key()
        if ctrl and shift and key == int(Qt.Key.Key_V):
            self.paste_from_clipboard()
            return
        if ctrl and shift and key == int(Qt.Key.Key_C):
            self.copy_visible()
            return
        if ctrl and not shift and key == int(Qt.Key.Key_C) and self.selected_text():
            # With an active selection, Ctrl+C copies instead of sending SIGINT.
            self.copy_selection()
            self.clear_selection()
            return
        if shift and key == int(Qt.Key.Key_Insert):
            self.paste_from_clipboard()
            return
        if key == int(Qt.Key.Key_Escape) and self._selection is not None:
            self.clear_selection()
            return
        name = _KEY_NAMES.get(key)
        if name is not None:
            sequence = encode_key(
                name, app_cursor=self.vt.app_cursor, shift=shift, alt=alt, ctrl=ctrl
            )
            if sequence is not None:
                self.clear_selection()
                self.send(sequence)
            return
        text = event.text()
        if text:
            self.clear_selection()
            self.send(encode_text(text, alt=alt))

    # ------------------------------------------------------------ mouse
    def _cell_at(self, pos) -> tuple[int, int]:
        col = min(max(0, int(pos.x()) // self.cell_w), self.vt.cols - 1)
        row = min(max(0, int(pos.y()) // self.cell_h), self.vt.rows - 1)
        return col, row

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        button = _MOUSE_BUTTONS.get(event.button())
        if button == 0 and not self.vt.mouse_level:
            col, row = self._cell_at(event.position())
            self._selection = ((row, col), (row, col))
            self._selecting = True
            self.update()
            return
        if button is not None and self.vt.mouse_level:
            col, row = self._cell_at(event.position())
            self.send(encode_mouse(button, col, row, press=True, sgr=self.vt.mouse_sgr))
            self._button_down = True

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._selecting and event.button() == Qt.MouseButton.LeftButton:
            self._selecting = False
            if self._selection is not None and self._selection[0] == self._selection[1]:
                self._selection = None  # plain click, not a drag
            self.update()
            return
        button = _MOUSE_BUTTONS.get(event.button())
        if button is not None and self.vt.mouse_level and self._button_down:
            col, row = self._cell_at(event.position())
            self.send(encode_mouse(button, col, row, press=False, sgr=self.vt.mouse_sgr))
        self._button_down = False

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._selecting and self._selection is not None:
            anchor = self._selection[0]
            col, row = self._cell_at(event.position())
            self._selection = (anchor, (row, col))
            self.update()
            return
        level = self.vt.mouse_level
        if level == 1003 or (level == 1002 and self._button_down):
            col, row = self._cell_at(event.position())
            self.send(encode_mouse(0, col, row, press=True, motion=True, sgr=self.vt.mouse_sgr))

    def wheelEvent(self, event) -> None:  # noqa: N802
        steps = event.angleDelta().y() // 120
        if not steps:
            return
        if self.vt.mouse_level:
            col, row = self._cell_at(event.position())
            button = 64 if steps > 0 else 65
            for _ in range(abs(steps)):
                self.send(encode_mouse(button, col, row, press=True, sgr=self.vt.mouse_sgr))
        else:
            self.vt.scroll_by(steps * 3)
            self.update()

    def feed(self, text: str) -> None:
        if text:
            self.vt.feed(text)
            self.update()


class TerminalPanel(QFrame):
    """Header (profile, status, controls) above one embedded terminal view."""

    def __init__(self, panel_id: str, profiles: list[LaunchProfile], parent=None) -> None:
        super().__init__(parent)
        self.panel_id = panel_id
        self.cwd = ""
        self.session: PtySession | None = None
        self.reported_exit = False
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Two header rows so the panel stays usable at narrow widths on small screens.
        title_line = QHBoxLayout()
        self.profile_box = QComboBox()
        for profile in profiles:
            self.profile_box.addItem(profile.name, profile.profile_id)
        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.restart_button = QPushButton("Restart")
        self.folder_button = QPushButton("Folder…")
        self.status = QLabel("Not started")
        self.status.setMinimumWidth(0)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title_line.addWidget(QLabel(f"Terminal {panel_id}"))
        title_line.addWidget(self.profile_box)
        title_line.addWidget(self.status, 1)
        layout.addLayout(title_line)
        button_line = QHBoxLayout()
        for button in (
            self.start_button,
            self.stop_button,
            self.restart_button,
            self.folder_button,
        ):
            button_line.addWidget(button)
        button_line.addStretch(1)
        layout.addLayout(button_line)

        self.view = TerminalView(panel_id)
        layout.addWidget(self.view, 1)


class SupervisorChat(QFrame):
    """Central supervisor: explicit targeting, queues, approvals and review."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        self.target_box = QComboBox()
        self.input = QLineEdit()
        self.input.setPlaceholderText(
            "Plain text goes to the selected terminal. /help for commands."
        )
        self.send_button = QPushButton("Send")
        self.pause_button = QPushButton("Pause")
        self.resume_button = QPushButton("Resume")
        self.review_button = QPushButton("Review")
        self.stop_button = QPushButton("Stop")
        self.restart_button = QPushButton("Restart")
        self.target_box.setMinimumContentsLength(8)
        self.target_box.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        # Controls wrap to two rows so the supervisor stays usable on narrow screens.
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Target:"))
        controls.addWidget(self.target_box, 1)
        controls.addWidget(self.pause_button)
        controls.addWidget(self.resume_button)
        actions = QHBoxLayout()
        for button in (self.review_button, self.stop_button, self.restart_button):
            actions.addWidget(button)
        actions.addStretch(1)
        entry = QHBoxLayout()
        entry.addWidget(self.input, 1)
        entry.addWidget(self.send_button)
        layout.addWidget(QLabel("Supervisor"))
        layout.addWidget(self.log, 1)
        layout.addLayout(controls)
        layout.addLayout(actions)
        layout.addLayout(entry)


class MainWindow(QMainWindow):
    def __init__(self, state_path: Path | None = None, state: AppState | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Prime Harness — Native Windows Control Room")
        self.state_path = state_path or default_state_path()
        self.state = state if state is not None else load_state(self.state_path)
        self.supervisor = Supervisor()
        self.supervisor.history = list(self.state.supervisor_history)
        self.panels: dict[str, TerminalPanel] = {}
        # External-chat connectivity: pairing, per-terminal bridges, event cursors.
        _host, _port, public = api_settings()
        self.room = ControlRoom(self.supervisor, public_base=public)
        self.api_server: ApiServer | None = None

        # The whole interface lives in a scroll area: on screens smaller than the
        # layout minimum every panel stays reachable instead of being clipped.
        root = QWidget()
        self._root = root
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(root)
        self.setCentralWidget(scroll)
        outer = QVBoxLayout(root)
        self.top_row = QSplitter(Qt.Orientation.Horizontal)
        self.middle_row = QSplitter(Qt.Orientation.Horizontal)
        self.extra_row = QSplitter(Qt.Orientation.Horizontal)
        self.vertical = QSplitter(Qt.Orientation.Vertical)
        self.vertical.addWidget(self.top_row)
        self.vertical.addWidget(self.middle_row)
        self.vertical.addWidget(self.extra_row)
        self.extra_row.hide()
        outer.addWidget(self.vertical, 1)
        self._splitters = {
            "vertical": self.vertical,
            "top": self.top_row,
            "middle": self.middle_row,
            "extra": self.extra_row,
        }
        self._layout_applied = False
        self._layout_ready = False
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._persist)
        for splitter in self._splitters.values():
            splitter.splitterMoved.connect(lambda *_args: self._save_timer.start())

        self.chat = SupervisorChat()
        self.chat.send_button.clicked.connect(self._on_chat_submit)
        self.chat.input.returnPressed.connect(self._on_chat_submit)
        self.chat.target_box.currentIndexChanged.connect(self._on_target_changed)
        self.chat.pause_button.clicked.connect(lambda: self.handle_chat("/pause"))
        self.chat.resume_button.clicked.connect(lambda: self.handle_chat("/resume"))
        self.chat.review_button.clicked.connect(lambda: self.handle_chat("/review"))
        self.chat.stop_button.clicked.connect(lambda: self.handle_chat("/stop"))
        self.chat.restart_button.clicked.connect(lambda: self.handle_chat("/restart"))

        bar = QHBoxLayout()
        self.add_button = QPushButton("+ Add Terminal")
        self.sessions_button = QPushButton("Sessions")
        self.notifications_button = QPushButton("Notifications")
        self.bar_status = QLabel("")
        bar.addWidget(self.add_button)
        bar.addWidget(self.sessions_button)
        bar.addWidget(self.notifications_button)
        bar.addStretch(1)
        bar.addWidget(self.bar_status)
        outer.addLayout(bar)
        self.add_button.clicked.connect(self.add_terminal)
        self.sessions_button.clicked.connect(self._show_sessions_menu)
        self.notifications_button.clicked.connect(self._show_notifications)

        self._build_panels()
        self.supervisor.log("Supervisor ready. " + CHAT_HELP.splitlines()[0])
        self._refresh_targets()
        self._refresh_chat_log()

        self.pump = QTimer(self)
        self.pump.timeout.connect(self._pump)
        self.pump.start(PUMP_INTERVAL_MS)
        self._apply_startup_geometry()

    # ------------------------------------------------------------ panels
    def _build_panels(self) -> None:
        configured = {p.panel_id: p for p in self.state.panels}
        for index, panel_id in enumerate(FIXED_PANEL_IDS):
            config = configured.get(panel_id) or PanelConfig(
                panel_id, DEFAULT_PANEL_PROFILES[index], self.state.default_cwd
            )
            self._create_panel(config)
        self.top_row.addWidget(self.panels["1"])
        self.top_row.addWidget(self.panels["2"])
        self.middle_row.addWidget(self.panels["3"])
        self.middle_row.addWidget(self.chat)
        self.middle_row.addWidget(self.panels["4"])
        for config in self.state.panels:
            if config.panel_id not in self.panels:
                self._create_panel(config)
                self.extra_row.addWidget(self.panels[config.panel_id])
                self.extra_row.show()
        self._persist()

    def _create_panel(self, config: PanelConfig) -> TerminalPanel:
        panel = TerminalPanel(config.panel_id, self.state.profiles)
        panel.cwd = config.cwd
        index = panel.profile_box.findData(config.profile_id)
        panel.profile_box.setCurrentIndex(max(0, index))
        panel.start_button.clicked.connect(lambda: self.start_panel(panel.panel_id))
        panel.stop_button.clicked.connect(lambda: self.stop_panel(panel.panel_id))
        panel.restart_button.clicked.connect(lambda: self.restart_panel(panel.panel_id))
        panel.folder_button.clicked.connect(lambda: self.choose_folder(panel.panel_id))
        panel.profile_box.currentIndexChanged.connect(lambda _i: self._persist())
        panel.view.focused_panel.connect(self._on_panel_focused)
        panel.view.placeholder = "Press Start to launch this profile in its folder."
        self.panels[config.panel_id] = panel
        self.supervisor.register(config.panel_id, f"Terminal {config.panel_id}")
        self.room.register_terminal(config.panel_id)
        panel.status.setText("Not started")
        return panel

    def add_terminal(self) -> str:
        numeric = [int(p) for p in self.panels if p.isdigit()]
        panel_id = str(max(numeric, default=0) + 1)
        panel = self._create_panel(PanelConfig(panel_id, "cmd", self.state.default_cwd))
        self.extra_row.addWidget(panel)
        self._show_extra_row()
        self._refresh_targets()
        self._persist()
        self.supervisor.log(f"Added Terminal {panel_id} (Command Prompt). Press Start to launch.")
        self._refresh_chat_log()
        return panel_id

    def _profile_for(self, panel: TerminalPanel) -> LaunchProfile | None:
        return find_profile(self.state.profiles, str(panel.profile_box.currentData()))

    def start_panel(self, panel_id: str) -> bool:
        panel = self.panels[panel_id]
        if panel.session is not None and panel.session.running:
            return True
        profile = self._profile_for(panel)
        if profile is None:
            panel.status.setText("No profile selected")
            return False
        argv, error = resolve_argv(profile.argv)
        if error:
            panel.status.setText(error)
            self._notify(panel_id, "failed", f"{profile.name}: {error}")
            return False
        cwd = panel.cwd or self.state.default_cwd or str(Path.home())
        session = PtySession(
            session_id=f"panel-{panel_id}-{os.getpid()}-{id(panel)}",
            argv=argv,
            cwd=cwd,
            env=dict(profile.env),
            rows=panel.view.vt.rows,
            cols=panel.view.vt.cols,
        )
        panel.reported_exit = False
        panel.view.vt = VtScreen(panel.view.vt.rows, panel.view.vt.cols)
        panel.view.placeholder = ""
        if session.start():
            panel.session = session
            panel.view.session = session
            panel.status.setText(f"Running · {profile.name} · {cwd}")
            self.room.set_status(panel_id, f"running · {profile.name}")
            self.room.publish("session_started", panel_id, f"{profile.name} started in {cwd}")
            self.supervisor.log(f"[{panel_id}] started {profile.name} in {cwd}")
            self._refresh_chat_log()
            return True
        panel.status.setText(session.error or "Launch failed")
        self.room.set_status(panel_id, "failed to start")
        self._notify(panel_id, "failed", f"{profile.name} did not start: {session.error}")
        return False

    def stop_panel(self, panel_id: str) -> bool:
        panel = self.panels[panel_id]
        if panel.session is None:
            return True
        stopped = panel.session.stop()
        if stopped:
            panel.status.setText("Stopped by operator")
            self.room.set_status(panel_id, "stopped by operator")
            self.room.publish("session_stopped", panel_id, "stopped by operator")
            self.supervisor.log(f"[{panel_id}] stopped")
        else:
            panel.status.setText("Stop requested; process still exiting")
        self._refresh_chat_log()
        return stopped

    def restart_panel(self, panel_id: str) -> bool:
        self.stop_panel(panel_id)
        return self.start_panel(panel_id)

    def choose_folder(self, panel_id: str) -> None:
        panel = self.panels[panel_id]
        chosen = QFileDialog.getExistingDirectory(
            self, "Working directory", panel.cwd or str(Path.home())
        )
        if chosen:
            panel.cwd = chosen
            panel.status.setText(f"Folder set: {chosen} (applies on next Start)")
            self._persist()

    # ----------------------------------------------------------- pump
    def _pump(self) -> None:
        for panel_id, panel in self.panels.items():
            session = panel.session
            if session is None:
                continue
            data = session.drain()
            if data:
                panel.view.feed(data)
                self.room.publish_output(panel_id, panel.view.vt.plain_text(60))
            if session.state is SessionState.EXITED and not panel.reported_exit:
                panel.reported_exit = True
                code = session.exit_code
                if code in (0, None):
                    kind = "completed"
                    message = "process exited" + (f" (code {code})" if code is not None else "")
                else:
                    kind = "failed"
                    message = f"process exited with code {code}"
                panel.status.setText(f"Exited · {message}")
                self.room.set_status(panel_id, f"exited ({code})")
                self._notify(panel_id, kind, message)
            if session.running:
                for command in self.supervisor.take_deliverable(panel_id):
                    if session.write(command.text + "\r"):
                        self.supervisor.log(f"[{panel_id}] delivered #{command.command_id}")
                        self.room.note_delivered(panel_id, command)
                    else:
                        command.status = "queued"
                        break
        self._refresh_bar()

    def _notify(self, panel_id: str, kind: str, message: str) -> None:
        self.supervisor.notify(panel_id, kind, message)
        self.room.publish("notification", panel_id, f"{kind}: {message}")
        self._refresh_chat_log()

    def _refresh_bar(self) -> None:
        unread = self.supervisor.unread_count()
        self.notifications_button.setText(
            f"Notifications ({unread})" if unread else "Notifications"
        )
        running = sum(1 for p in self.panels.values() if p.session and p.session.running)
        self.bar_status.setText(f"{running} running · {len(self.panels)} terminals")

    # ------------------------------------------------------ supervisor
    def _refresh_targets(self) -> None:
        box = self.chat.target_box
        box.blockSignals(True)
        box.clear()
        for panel_id in self.panels:
            box.addItem(f"Terminal {panel_id}", panel_id)
        if self.supervisor.target in self.panels:
            box.setCurrentIndex(box.findData(self.supervisor.target))
        box.blockSignals(False)

    def _on_target_changed(self, _index: int) -> None:
        data = self.chat.target_box.currentData()
        if data:
            self.supervisor.select(str(data))

    def _on_panel_focused(self, panel_id: str) -> None:
        if self.supervisor.select(panel_id):
            self._refresh_targets()

    def _on_chat_submit(self) -> None:
        line = self.chat.input.text()
        self.chat.input.clear()
        if line.strip():
            self.handle_chat(line)

    def handle_chat(self, line: str) -> None:
        """Route one supervisor line. Plain text is only ever sent to the target."""
        action, argument = self.supervisor.parse_chat(line)
        target = self.supervisor.target
        ident = argument.split(" ", 1)[0] if argument else ""
        if action == "send":
            if not target:
                self.supervisor.log("No target selected. Use /to <id>.")
            else:
                self.supervisor.enqueue(target, argument)
        elif action == "to":
            if self.supervisor.select(ident):
                self._refresh_targets()
                self.supervisor.log(f"Target: Terminal {ident}")
            else:
                self.supervisor.log(f"Unknown terminal '{ident}'.")
        elif action in ("approve", "reject"):
            try:
                command_id = int(ident)
            except ValueError:
                self.supervisor.log(f"Usage: /{action} <cmd#>")
            else:
                decide = self.supervisor.approve if action == "approve" else self.supervisor.reject
                if not decide(command_id):
                    self.supervisor.log(f"#{ident} is not awaiting approval.")
        elif action == "queue":
            pending = self.supervisor.queue_for(target) if target else []
            listing = ", ".join(f"#{c.command_id} {c.status}" for c in pending) or "empty"
            self.supervisor.log(f"Queue for Terminal {target}: {listing}")
        elif action == "review":
            panel_id = ident or target or ""
            if panel_id in self.panels:
                text = self.panels[panel_id].view.vt.plain_text(60) or "(no output yet)"
                self.supervisor.log(
                    f"--- Review Terminal {panel_id} (last 60 lines) ---\n{text}\n---"
                )
        elif action in ("pause", "resume"):
            panel_id = ident or target or ""
            if panel_id in self.panels:
                self.supervisor.set_paused(panel_id, action == "pause")
                note = (
                    "Delivery of queued commands paused; the agent process keeps running."
                    if action == "pause"
                    else "Delivery resumed."
                )
                self.supervisor.log(f"[{panel_id}] {note}")
        elif action in ("stop", "restart"):
            panel_id = ident or target or ""
            if panel_id in self.panels:
                if action == "stop":
                    self.stop_panel(panel_id)
                else:
                    self.restart_panel(panel_id)
        elif action == "status":
            for panel_id, panel in self.panels.items():
                state = panel.session.state.value if panel.session else "not started"
                paused = " (paused)" if self.supervisor.sessions[panel_id].paused else ""
                self.supervisor.log(
                    f"Terminal {panel_id}: {state}{paused} · {panel.profile_box.currentText()}"
                )
        elif action == "pair":
            panel_id = ident or target or ""
            if ident in {"all", "supervisor"}:
                # Supervisor scope: sees every session; commands still name one target.
                _token, url = self.room.pairings.pair(None, label="supervisor")
                self.supervisor.log(f"Supervisor pairing link (token shown once):\n{url}")
                self.room.publish("paired", None, "supervisor pairing link issued")
            elif panel_id not in self.panels:
                self.supervisor.log("Usage: /pair <id> (or select a target first)")
            else:
                token, url = self.room.pairings.pair(panel_id, label=f"terminal-{panel_id}")
                self.supervisor.log(f"[{panel_id}] pairing link (token shown once):\n{url}")
                self.room.publish("paired", panel_id, "pairing link issued")
        elif action == "revoke":
            if ident == "all":
                count = self.room.pairings.revoke_all()
                self.supervisor.log(f"Revoked {count} pairing token(s).")
                self.room.publish("revoked", None, f"all pairings revoked ({count})")
            else:
                count = self.room.pairings.revoke_terminal(ident)
                self.supervisor.log(f"Revoked {count} pairing token(s) for Terminal {ident}.")
                if ident in self.panels:
                    self.room.publish("revoked", ident, f"{count} pairing token(s) revoked")
        elif action == "api":
            sub = ident.lower() if ident else "status"
            if sub == "start":
                self._start_api()
            elif sub == "stop":
                self._stop_api()
            else:
                if self.api_server is not None and self.api_server.running:
                    self.supervisor.log(
                        f"API running on {self.api_server.host}:{self.api_server.port}"
                    )
                else:
                    self.supervisor.log("API not running. Use /api start.")
        elif action == "help":
            self.supervisor.log(CHAT_HELP)
        else:
            self.supervisor.log(f"Unknown command /{action}. Try /help.")
        self._refresh_chat_log()
        self._persist()

    def _start_api(self) -> None:
        """Start the authenticated control API (pairing links, MCP, remote access)."""
        if self.api_server is not None and self.api_server.running:
            self.supervisor.log(
                f"API already running on {self.api_server.host}:{self.api_server.port}"
            )
            return
        host, port, public = api_settings()
        self.room.pairings.public_base = public.rstrip("/")
        self.api_server = ApiServer(self.room, host, port)
        if self.api_server.start():
            self.supervisor.log(f"Control API listening on {host}:{port} · pair links use {public}")
        else:
            self.supervisor.log("Control API failed to start.")

    def _stop_api(self) -> None:
        if self.api_server is None:
            self.supervisor.log("API is not running.")
            return
        stopped = self.api_server.stop()
        self.api_server = None
        self.supervisor.log("Control API stopped." if stopped else "API stop requested.")

    def _refresh_chat_log(self) -> None:
        bar = self.chat.log.verticalScrollBar()
        self.chat.log.setPlainText("\n".join(self.supervisor.history[-400:]))
        bar.setValue(bar.maximum())
        self._refresh_bar()

    def _show_sessions_menu(self) -> None:
        menu = QMenu(self)
        for panel_id, panel in self.panels.items():
            state = panel.session.state.value if panel.session else "not started"
            action = menu.addAction(
                f"Terminal {panel_id} · {panel.profile_box.currentText()} · {state}"
            )
            action.triggered.connect(
                lambda _checked=False, pid=panel_id: self._select_from_menu(pid)
            )
        menu.exec(self.sessions_button.mapToGlobal(self.sessions_button.rect().bottomLeft()))

    def _select_from_menu(self, panel_id: str) -> None:
        self.supervisor.select(panel_id)
        self._refresh_targets()
        self.panels[panel_id].view.setFocus()

    def _show_notifications(self) -> None:
        items = self.supervisor.notifications[-50:]
        text = (
            "\n".join(
                f"[{n.created_at}] Terminal {n.session_id}: {n.kind} — {n.message}" for n in items
            )
            or "No notifications."
        )
        self.supervisor.mark_all_read()
        self._refresh_bar()
        QMessageBox.information(self, "Notifications", text)

    # ----------------------------------------------------- persistence
    # ---------------------------------------------------------- geometry
    def _screen_available(self, point: QPoint | None = None) -> QRect:
        """Usable screen area (taskbar excluded, logical pixels) under ``point``."""
        screen = QGuiApplication.screenAt(point) if point is not None else None
        screen = screen or QGuiApplication.primaryScreen()
        return screen.availableGeometry()

    def _frame_margins(self) -> tuple[int, int, int, int]:
        """Window decoration around the client area; estimated until the window is shown."""
        if not self.isVisible():
            return FRAME_ESTIMATE
        frame = self.frameGeometry()
        client = self.geometry()
        return (
            client.x() - frame.x(),
            client.y() - frame.y(),
            frame.right() - client.right(),
            frame.bottom() - client.bottom(),
        )

    def _usable_area(self, point: QPoint | None = None) -> QRect:
        """Client area of the screen under ``point`` (primary screen when None).

        Qt's availableGeometry already excludes the Windows taskbar and is in
        logical pixels, so display scaling is handled by Qt. The top is pushed
        down by the title bar so the window can always be grabbed.
        """
        available = self._screen_available(point)
        left, top, right, bottom = self._frame_margins()
        return QRect(
            available.x() + left,
            available.y() + top,
            max(1, available.width() - left - right),
            max(1, available.height() - top - bottom),
        )

    def _fit_to_screen(self) -> None:
        """Once shown, clamp the real window frame (title bar and borders) onto the screen."""
        if self.isMaximized() or self.isFullScreen():
            return
        area = self._usable_area(self.frameGeometry().center())
        target = clamp_window_rect(self.geometry(), area, self._root.minimumSizeHint())
        if target != self.geometry():
            self.setGeometry(target)

    def _apply_startup_geometry(self) -> None:
        """Fit the window to the usable screen: a saved geometry clamped to it, or a fresh fit."""
        saved = self.state.window
        geometry = saved.get("geometry")
        if isinstance(geometry, list) and len(geometry) == 4:
            saved_rect = QRect(*(int(value) for value in geometry))
            minimum = self._root.minimumSizeHint()
            target = clamp_window_rect(saved_rect, self._usable_area(saved_rect.center()), minimum)
        else:
            target = initial_window_rect(self._usable_area())
        self.setGeometry(target)
        if saved.get("maximized") is True:
            self.showMaximized()

    def _window_state(self) -> dict:
        """Serializable geometry and splitter proportions for the next launch."""
        rect = self.normalGeometry() if self.isMaximized() else self.geometry()
        splitters = {
            name: fractions_from_sizes(splitter.sizes())
            for name, splitter in self._splitters.items()
            if splitter.isVisible()
        }
        return {
            "geometry": [rect.x(), rect.y(), rect.width(), rect.height()],
            "maximized": self.isMaximized(),
            "splitters": splitters,
        }

    @staticmethod
    def _splitter_metrics(splitter: QSplitter) -> tuple[int, list[int]]:
        horizontal = splitter.orientation() == Qt.Orientation.Horizontal
        children = [splitter.widget(i) for i in range(splitter.count())]
        minimums = [
            (child.minimumSizeHint().width() if horizontal else child.minimumSizeHint().height())
            for child in children
            if child is not None
        ]
        span = splitter.width() if horizontal else splitter.height()
        total = span - splitter.handleWidth() * max(0, splitter.count() - 1)
        return total, minimums

    def _set_splitter_fractions(self, splitter: QSplitter, fractions: list[float]) -> None:
        total, minimums = self._splitter_metrics(splitter)
        if len(fractions) != splitter.count() or total <= 0:
            return
        splitter.setSizes(sizes_from_fractions(fractions, total, minimums))

    def _restore_splitters(self) -> None:
        saved = self.state.window.get("splitters", {})
        if not isinstance(saved, dict):
            saved = {}
        for name, splitter in self._splitters.items():
            if splitter.isHidden() or splitter.count() == 0:
                continue
            fractions = saved.get(name)
            if not isinstance(fractions, list) or len(fractions) != splitter.count():
                count = splitter.count()
                fractions = DEFAULT_SPLITTER_FRACTIONS.get(name, [1 / count] * count)
                if name == "vertical" and not self.extra_row.isHidden():
                    fractions = [0.4, 0.4, 0.2]
            self._set_splitter_fractions(splitter, [float(value) for value in fractions])

    def _show_extra_row(self) -> None:
        newly_shown = self.extra_row.isHidden()
        self.extra_row.show()
        if newly_shown and self.vertical.isVisible():
            self._set_splitter_fractions(self.vertical, [0.4, 0.4, 0.2])
        self._schedule_save()

    def _schedule_save(self) -> None:
        self._save_timer.start()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        if not self._layout_applied:
            self._layout_applied = True
            # Run after the first layout pass so splitter widths are known.
            QTimer.singleShot(0, self._finish_startup_layout)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if self._layout_ready:
            self._schedule_save()

    def moveEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().moveEvent(event)
        if self._layout_ready:
            self._schedule_save()

    def _finish_startup_layout(self) -> None:
        self._fit_to_screen()
        self._restore_splitters()
        self._layout_ready = True
        self._schedule_save()

    def _persist(self) -> None:
        if self._layout_ready:
            self.state.window = self._window_state()
        self.state.panels = [
            PanelConfig(panel_id, str(panel.profile_box.currentData()), panel.cwd)
            for panel_id, panel in self.panels.items()
        ]
        self.state.supervisor_history = self.supervisor.history[-500:]
        try:
            save_state(self.state_path, self.state)
        except OSError as exc:
            self.supervisor.log(f"Could not save settings: {exc}")

    def closeEvent(self, event) -> None:  # noqa: N802
        self.pump.stop()
        if self.api_server is not None:
            self.api_server.stop()
            self.api_server = None
        for panel in self.panels.values():
            if panel.session is not None:
                panel.session.stop()
        self._persist()
        super().closeEvent(event)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.show()
    if os.environ.get("PRIME_HARNESS_API_AUTOSTART", "").lower() in {"1", "true", "yes"}:
        window._start_api()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
