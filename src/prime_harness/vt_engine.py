"""VT terminal engine for embedded Prime Harness panels (Qt-independent).

Consumes text emitted by a ConPTY session and maintains a character grid with
scrollback. Handles the DEC private modes that full-screen agents such as OMP
depend on and that pyte does not model itself:

* alternate screen buffer (1049 / 1047 / 47) — kept as a separate grid so the
  main-screen scrollback is preserved when the agent exits;
* bracketed paste (2004);
* application cursor keys (1);
* mouse tracking (1000 / 1002 / 1003) with SGR (1006) or legacy X10 encoding.

Also provides key and mouse encoders that turn UI events into the byte
sequences a terminal application expects.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import pyte
from pyte.screens import Char

Row = list[Char]

_ALT_SPLIT_RE = re.compile(r"(\x1b\[\?(?:1049|1047|47)[hl])")
_MODE_RE = re.compile(r"\x1b\[\?([0-9;]+)([hl])")
# A chunk may end part-way through a CSI sequence; hold that tail back.
_INCOMPLETE_TAIL_RE = re.compile(r"\x1b(?:\[\??[0-9;]*)?\Z")
_MOUSE_LEVELS = {1000, 1002, 1003}


class VtScreen:
    """Character grid with scrollback and terminal-mode tracking."""

    def __init__(self, rows: int = 24, cols: int = 80, history: int = 5000) -> None:
        self.rows = rows
        self.cols = cols
        self._main = pyte.HistoryScreen(cols, rows, history=history)
        self._alt = pyte.Screen(cols, rows)
        self._main_stream = pyte.Stream(self._main)
        self._alt_stream = pyte.Stream(self._alt)
        self._pending = ""
        self.in_alt = False
        self.bracketed_paste = False
        self.app_cursor = False
        self.mouse_level = 0
        self.mouse_sgr = False
        self.scroll_offset = 0

    # ------------------------------------------------------------------ input
    @property
    def active(self) -> pyte.Screen:
        return self._alt if self.in_alt else self._main

    def feed(self, text: str) -> None:
        if not text:
            return
        data = self._pending + text
        self._pending = ""
        tail = _INCOMPLETE_TAIL_RE.search(data)
        if tail:
            self._pending = data[tail.start():]
            data = data[: tail.start()]
        for match in _MODE_RE.finditer(data):
            self._apply_modes(match.group(1), match.group(2) == "h")
        for index, part in enumerate(_ALT_SPLIT_RE.split(data)):
            if index % 2 == 1:
                self._switch_alt(part.endswith("h"))
            elif part:
                stream = self._alt_stream if self.in_alt else self._main_stream
                stream.feed(part)
        if self.scroll_offset and not self.in_alt:
            self.scroll_offset = min(self.scroll_offset, len(self._main.history.top))

    def _apply_modes(self, params: str, enable: bool) -> None:
        for raw in params.split(";"):
            if not raw.isdigit():
                continue
            mode = int(raw)
            if mode == 1:
                self.app_cursor = enable
            elif mode == 2004:
                self.bracketed_paste = enable
            elif mode in _MOUSE_LEVELS:
                if enable:
                    self.mouse_level = mode
                elif self.mouse_level == mode:
                    self.mouse_level = 0
            elif mode == 1006:
                self.mouse_sgr = enable

    def _switch_alt(self, enable: bool) -> None:
        if enable and not self.in_alt:
            self.in_alt = True
            self._alt.reset()
            self.scroll_offset = 0
        elif not enable and self.in_alt:
            self.in_alt = False

    # ------------------------------------------------------------------ size
    def resize(self, rows: int, cols: int) -> None:
        if rows == self.rows and cols == self.cols:
            return
        self.rows, self.cols = rows, cols
        self._main.resize(rows, cols)
        self._alt.resize(rows, cols)

    # ------------------------------------------------------------- scrollback
    @property
    def history_length(self) -> int:
        return 0 if self.in_alt else len(self._main.history.top)

    def scroll_by(self, lines: int) -> None:
        if self.in_alt:
            return
        self.scroll_offset = max(0, min(self.scroll_offset + lines, self.history_length))

    def scroll_to_bottom(self) -> None:
        self.scroll_offset = 0

    # ----------------------------------------------------------------- output
    @property
    def cursor(self) -> tuple[int, int, bool]:
        """(row, col, visible) of the active screen's cursor."""
        screen = self.active
        hidden = screen.cursor.hidden or self.scroll_offset > 0
        return screen.cursor.y, screen.cursor.x, not hidden

    def render_rows(self) -> list[Row]:
        """Visible rows as lists of pyte Char cells, honouring scroll offset."""
        screen = self.active
        if self.in_alt or self.scroll_offset == 0:
            return [self._row_from_buffer(screen.buffer, y) for y in range(self.rows)]
        history = list(self._main.history.top)
        start = len(history) - self.scroll_offset
        rows: list[Row] = []
        for index in range(start, start + self.rows):
            if index < len(history):
                rows.append([history[index][x] for x in range(self.cols)])
            else:
                rows.append(self._row_from_buffer(screen.buffer, index - len(history)))
        return rows

    def _row_from_buffer(self, buffer, y: int) -> Row:
        line = buffer[y]
        return [line[x] for x in range(self.cols)]

    def plain_text(self, last_lines: int = 60) -> str:
        """Plain text of scrollback plus the active screen, for supervisor review."""
        lines: list[str] = []
        if not self.in_alt:
            for history_line in self._main.history.top:
                lines.append("".join(history_line[x].data for x in range(self.cols)).rstrip())
        lines.extend(line.rstrip() for line in self.active.display)
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines[-last_lines:])


# ----------------------------------------------------------------- key input
_SPECIAL_KEYS = {
    "Enter": "\r",
    "Backspace": "\x7f",
    "Tab": "\t",
    "Backtab": "\x1b[Z",
    "Escape": "\x1b",
    "Insert": "\x1b[2~",
    "Delete": "\x1b[3~",
    "PageUp": "\x1b[5~",
    "PageDown": "\x1b[6~",
    "F1": "\x1bOP",
    "F2": "\x1bOQ",
    "F3": "\x1bOR",
    "F4": "\x1bOS",
    "F5": "\x1b[15~",
    "F6": "\x1b[17~",
    "F7": "\x1b[18~",
    "F8": "\x1b[19~",
    "F9": "\x1b[20~",
    "F10": "\x1b[21~",
    "F11": "\x1b[23~",
    "F12": "\x1b[24~",
}
_CURSOR_KEYS = {"Up": "A", "Down": "B", "Right": "C", "Left": "D", "Home": "H", "End": "F"}


def encode_key(
    name: str,
    *,
    app_cursor: bool = False,
    shift: bool = False,
    alt: bool = False,
    ctrl: bool = False,
) -> str | None:
    """Return the VT sequence for a named non-printable key, or None if unknown."""
    modifier = 1 + (1 if shift else 0) + (2 if alt else 0) + (4 if ctrl else 0)
    if name in _CURSOR_KEYS:
        letter = _CURSOR_KEYS[name]
        if modifier > 1:
            return f"\x1b[1;{modifier}{letter}"
        return f"\x1bO{letter}" if app_cursor else f"\x1b[{letter}"
    sequence = _SPECIAL_KEYS.get(name)
    if sequence is None:
        return None
    if alt and name not in {"Escape"}:
        return "\x1b" + sequence
    return sequence


def encode_text(text: str, *, alt: bool = False) -> str:
    """Printable input from the keyboard; Alt is sent as an ESC prefix."""
    return "\x1b" + text if alt and text else text


def encode_paste(text: str, *, bracketed: bool) -> str:
    """Clipboard text as terminal input. Strips embedded paste terminators."""
    normalized = text.replace("\r\n", "\n").replace("\n", "\r")
    if bracketed:
        normalized = normalized.replace("\x1b[200~", "").replace("\x1b[201~", "")
        return f"\x1b[200~{normalized}\x1b[201~"
    return normalized


def encode_mouse(
    button: int,
    col: int,
    row: int,
    *,
    press: bool,
    motion: bool = False,
    sgr: bool = True,
) -> str:
    """Encode a mouse event (0-based cell coordinates).

    button: 0 left, 1 middle, 2 right, 64 wheel up, 65 wheel down.
    """
    if sgr:
        code = button | (32 if motion else 0)
        return f"\x1b[<{code};{col + 1};{row + 1}{'M' if press else 'm'}"
    code = (3 if not press else button) | (32 if motion else 0)
    return "\x1b[M" + chr(code + 32) + chr(min(col + 33, 255)) + chr(min(row + 33, 255))


def chunks_to_text(chunks: Iterable[str]) -> str:
    return "".join(chunks)
