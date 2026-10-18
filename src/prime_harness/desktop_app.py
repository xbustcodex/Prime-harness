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

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
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
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from prime_harness.persistence import AppState, PanelConfig, default_state_path, load_state, save_state
from prime_harness.profiles import LaunchProfile, find_profile, resolve_argv
from prime_harness.pty_session import PtySession, SessionState
from prime_harness.supervisor import CHAT_HELP, Supervisor
from prime_harness.vt_engine import VtScreen, encode_key, encode_mouse, encode_paste, encode_text

PUMP_INTERVAL_MS = 30
DEFAULT_PANEL_PROFILES = ("omp", "primepi", "powershell", "cmd")
FIXED_PANEL_IDS = ("1", "2", "3", "4")

DEFAULT_FG = "#d4d4d4"
DEFAULT_BG = "#1e1e1e"
ANSI_COLORS = {
    "black": "#000000", "red": "#cd3131", "green": "#0dbc79", "brown": "#e5e510",
    "yellow": "#e5e510", "blue": "#2472c8", "magenta": "#bc3fbc", "cyan": "#11a8cd",
    "white": "#e5e5e5", "brightblack": "#666666", "brightred": "#f14c4c",
    "brightgreen": "#23d18b", "brightbrown": "#f5f543", "brightyellow": "#f5f543",
    "brightblue": "#3b8eea", "brightmagenta": "#d670d6", "brightcyan": "#29b8db",
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
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(self.cell_w * 20, self.cell_h * 5)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    # ------------------------------------------------------------- grid
    def sync_grid(self) -> None:
        """Rows and columns come from the rendered character grid, not pixels."""
        cols = max(20, self.width() // self.cell_w)
        rows = max(5, self.height() // self.cell_h)
        if (rows, cols) == (self.vt.rows, self.vt.cols):
            return
        self.vt.resize(rows, cols)
        if self.session is not None:
            self.session.resize(rows, cols)
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self.sync_grid()

    # ------------------------------------------------------------ paint
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(DEFAULT_BG))
        if self.session is None and self.placeholder:
            painter.setPen(QColor("#888888"))
            painter.setFont(self._font)
            painter.drawText(QRectF(8, 8, self.width() - 16, self.height() - 16),
                             int(Qt.TextFlag.TextWordWrap), self.placeholder)
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
        row, col, cursor_visible = self.vt.cursor
        if cursor_visible and self.hasFocus():
            painter.fillRect(col * cw, row * ch, cw, ch, QColor(200, 200, 200, 110))
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
        if shift and key == int(Qt.Key.Key_Insert):
            self.paste_from_clipboard()
            return
        name = _KEY_NAMES.get(key)
        if name is not None:
            sequence = encode_key(name, app_cursor=self.vt.app_cursor,
                                  shift=shift, alt=alt, ctrl=ctrl)
            if sequence is not None:
                self.send(sequence)
            return
        text = event.text()
        if text:
            self.send(encode_text(text, alt=alt))

    # ------------------------------------------------------------ mouse
    def _cell_at(self, pos) -> tuple[int, int]:
        col = min(max(0, int(pos.x()) // self.cell_w), self.vt.cols - 1)
        row = min(max(0, int(pos.y()) // self.cell_h), self.vt.rows - 1)
        return col, row

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        button = _MOUSE_BUTTONS.get(event.button())
        if button is not None and self.vt.mouse_level:
            col, row = self._cell_at(event.position())
            self.send(encode_mouse(button, col, row, press=True, sgr=self.vt.mouse_sgr))
            self._button_down = True

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        button = _MOUSE_BUTTONS.get(event.button())
        if button is not None and self.vt.mouse_level and self._button_down:
            col, row = self._cell_at(event.position())
            self.send(encode_mouse(button, col, row, press=False, sgr=self.vt.mouse_sgr))
        self._button_down = False

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
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

        header = QHBoxLayout()
        self.profile_box = QComboBox()
        for profile in profiles:
            self.profile_box.addItem(profile.name, profile.profile_id)
        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.restart_button = QPushButton("Restart")
        self.folder_button = QPushButton("Folder…")
        self.status = QLabel("Not started")
        header.addWidget(QLabel(f"Terminal {panel_id}"))
        header.addWidget(self.profile_box)
        for button in (self.start_button, self.stop_button, self.restart_button, self.folder_button):
            header.addWidget(button)
        header.addWidget(self.status, 1)
        layout.addLayout(header)

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
        self.input.setPlaceholderText("Plain text goes to the selected terminal. /help for commands.")
        self.send_button = QPushButton("Send")
        self.pause_button = QPushButton("Pause")
        self.resume_button = QPushButton("Resume")
        self.review_button = QPushButton("Review")
        self.stop_button = QPushButton("Stop")
        self.restart_button = QPushButton("Restart")
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Target:"))
        controls.addWidget(self.target_box, 1)
        for button in (self.pause_button, self.resume_button, self.review_button,
                       self.stop_button, self.restart_button):
            controls.addWidget(button)
        entry = QHBoxLayout()
        entry.addWidget(self.input, 1)
        entry.addWidget(self.send_button)
        layout.addWidget(QLabel("Supervisor"))
        layout.addWidget(self.log, 1)
        layout.addLayout(controls)
        layout.addLayout(entry)


class MainWindow(QMainWindow):
    def __init__(self, state_path: Path | None = None, state: AppState | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Prime Harness — Native Windows Control Room")
        self.resize(1400, 900)
        self.state_path = state_path or default_state_path()
        self.state = state if state is not None else load_state(self.state_path)
        self.supervisor = Supervisor()
        self.supervisor.history = list(self.state.supervisor_history)
        self.panels: dict[str, TerminalPanel] = {}

        root = QWidget()
        self.setCentralWidget(root)
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

    # ------------------------------------------------------------ panels
    def _build_panels(self) -> None:
        configured = {p.panel_id: p for p in self.state.panels}
        for index, panel_id in enumerate(FIXED_PANEL_IDS):
            config = configured.get(panel_id) or PanelConfig(
                panel_id, DEFAULT_PANEL_PROFILES[index], self.state.default_cwd)
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
        self.top_row.setSizes([1, 1])
        self.middle_row.setSizes([1, 1, 1])
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
        panel.status.setText("Not started")
        return panel

    def add_terminal(self) -> str:
        numeric = [int(p) for p in self.panels if p.isdigit()]
        panel_id = str(max(numeric, default=0) + 1)
        panel = self._create_panel(PanelConfig(panel_id, "cmd", self.state.default_cwd))
        self.extra_row.addWidget(panel)
        self.extra_row.show()
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
            self.supervisor.log(f"[{panel_id}] started {profile.name} in {cwd}")
            self._refresh_chat_log()
            return True
        panel.status.setText(session.error or "Launch failed")
        self._notify(panel_id, "failed", f"{profile.name} did not start: {session.error}")
        return False

    def stop_panel(self, panel_id: str) -> bool:
        panel = self.panels[panel_id]
        if panel.session is None:
            return True
        stopped = panel.session.stop()
        if stopped:
            panel.status.setText("Stopped by operator")
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
        chosen = QFileDialog.getExistingDirectory(self, "Working directory",
                                                  panel.cwd or str(Path.home()))
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
                self._notify(panel_id, kind, message)
            if session.running:
                for command in self.supervisor.take_deliverable(panel_id):
                    if session.write(command.text + "\r"):
                        self.supervisor.log(f"[{panel_id}] delivered #{command.command_id}")
                    else:
                        command.status = "queued"
                        break
        self._refresh_bar()

    def _notify(self, panel_id: str, kind: str, message: str) -> None:
        self.supervisor.notify(panel_id, kind, message)
        self._refresh_chat_log()

    def _refresh_bar(self) -> None:
        unread = self.supervisor.unread_count()
        self.notifications_button.setText(f"Notifications ({unread})" if unread else "Notifications")
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
                self.supervisor.log(f"--- Review Terminal {panel_id} (last 60 lines) ---\n{text}\n---")
        elif action in ("pause", "resume"):
            panel_id = ident or target or ""
            if panel_id in self.panels:
                self.supervisor.set_paused(panel_id, action == "pause")
                note = ("Delivery of queued commands paused; the agent process keeps running."
                        if action == "pause" else "Delivery resumed.")
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
                self.supervisor.log(f"Terminal {panel_id}: {state}{paused} · "
                                    f"{panel.profile_box.currentText()}")
        elif action == "help":
            self.supervisor.log(CHAT_HELP)
        else:
            self.supervisor.log(f"Unknown command /{action}. Try /help.")
        self._refresh_chat_log()
        self._persist()

    def _refresh_chat_log(self) -> None:
        bar = self.chat.log.verticalScrollBar()
        self.chat.log.setPlainText("\n".join(self.supervisor.history[-400:]))
        bar.setValue(bar.maximum())
        self._refresh_bar()

    def _show_sessions_menu(self) -> None:
        menu = QMenu(self)
        for panel_id, panel in self.panels.items():
            state = panel.session.state.value if panel.session else "not started"
            action = menu.addAction(f"Terminal {panel_id} · {panel.profile_box.currentText()} · {state}")
            action.triggered.connect(lambda _checked=False, pid=panel_id: self._select_from_menu(pid))
        menu.exec(self.sessions_button.mapToGlobal(self.sessions_button.rect().bottomLeft()))

    def _select_from_menu(self, panel_id: str) -> None:
        self.supervisor.select(panel_id)
        self._refresh_targets()
        self.panels[panel_id].view.setFocus()

    def _show_notifications(self) -> None:
        items = self.supervisor.notifications[-50:]
        text = "\n".join(f"[{n.created_at}] Terminal {n.session_id}: {n.kind} — {n.message}"
                         for n in items) or "No notifications."
        self.supervisor.mark_all_read()
        self._refresh_bar()
        QMessageBox.information(self, "Notifications", text)

    # ----------------------------------------------------- persistence
    def _persist(self) -> None:
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
        for panel in self.panels.values():
            if panel.session is not None:
                panel.session.stop()
        self._persist()
        super().closeEvent(event)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
