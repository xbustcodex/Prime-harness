from prime_harness.vt_engine import (
    VtScreen,
    encode_key,
    encode_mouse,
    encode_paste,
    encode_text,
)


def _row_text(vt: VtScreen, row: int) -> str:
    return "".join(cell.data for cell in vt.render_rows()[row]).rstrip()


def test_plain_text_and_cursor_position():
    vt = VtScreen(5, 20)
    vt.feed("hello\r\nworld")
    assert _row_text(vt, 0) == "hello"
    assert _row_text(vt, 1) == "world"
    row, col, visible = vt.cursor
    assert (row, col, visible) == (1, 5, True)


def test_cursor_positioning_and_colour_attributes():
    vt = VtScreen(5, 20)
    vt.feed("\x1b[3;4H\x1b[31mX\x1b[0m")
    assert vt.render_rows()[2][3].data == "X"
    assert vt.render_rows()[2][3].fg == "red"


def test_alternate_screen_preserves_main_scrollback():
    vt = VtScreen(4, 20)
    vt.feed("main-line\r\n")
    vt.feed("\x1b[?1049h")
    assert vt.in_alt
    vt.feed("\x1b[HOMP MENU")
    assert _row_text(vt, 0) == "OMP MENU"
    vt.feed("\x1b[?1049l")
    assert not vt.in_alt
    assert "main-line" in vt.plain_text()
    assert "OMP MENU" not in vt.plain_text()


def test_alt_screen_sequence_split_across_chunks():
    vt = VtScreen(4, 20)
    vt.feed("\x1b[?10")
    vt.feed("49h")
    assert vt.in_alt


def test_bracketed_paste_mode_is_tracked():
    vt = VtScreen(4, 20)
    assert not vt.bracketed_paste
    vt.feed("\x1b[?2004h")
    assert vt.bracketed_paste
    vt.feed("\x1b[?2004l")
    assert not vt.bracketed_paste


def test_mouse_modes_and_sgr_are_tracked():
    vt = VtScreen(4, 20)
    vt.feed("\x1b[?1002h\x1b[?1006h")
    assert vt.mouse_level == 1002
    assert vt.mouse_sgr
    vt.feed("\x1b[?1002l")
    assert vt.mouse_level == 0


def test_application_cursor_keys_are_tracked():
    vt = VtScreen(4, 20)
    vt.feed("\x1b[?1h")
    assert vt.app_cursor
    assert encode_key("Up", app_cursor=vt.app_cursor) == "\x1bOA"
    assert encode_key("Up", app_cursor=False) == "\x1b[A"


def test_scrollback_offset_shows_history():
    vt = VtScreen(3, 20, history=100)
    for index in range(10):
        vt.feed(f"line{index}\r\n")
    assert vt.history_length > 0
    vt.scroll_by(3)
    assert vt.scroll_offset == 3
    assert vt.cursor[2] is False
    assert any("line" in _row_text(vt, r) for r in range(3))
    vt.scroll_to_bottom()
    assert vt.scroll_offset == 0


def test_resize_updates_grid_dimensions():
    vt = VtScreen(4, 20)
    vt.resize(10, 40)
    assert (vt.rows, vt.cols) == (10, 40)
    assert len(vt.render_rows()) == 10
    assert len(vt.render_rows()[0]) == 40


def test_unicode_box_drawing_and_wide_characters():
    vt = VtScreen(3, 20)
    vt.feed("┌─┐│中│")
    text = _row_text(vt, 0)
    assert "┌─┐" in text
    assert "中" in text


def test_encode_paste_plain_and_bracketed():
    assert encode_paste("a\nb", bracketed=False) == "a\rb"
    assert encode_paste("x\x1b[201~y", bracketed=True) == "\x1b[200~xy\x1b[201~"


def test_encode_key_and_text():
    assert encode_key("Enter") == "\r"
    assert encode_key("Backspace") == "\x7f"
    assert encode_key("F5") == "\x1b[15~"
    assert encode_key("Left", ctrl=True) == "\x1b[1;5D"
    assert encode_key("Unknown") is None
    assert encode_text("a", alt=True) == "\x1ba"


def test_encode_mouse_sgr_and_legacy():
    assert encode_mouse(0, 4, 2, press=True) == "\x1b[<0;5;3M"
    assert encode_mouse(0, 4, 2, press=False) == "\x1b[<0;5;3m"
    assert encode_mouse(64, 0, 0, press=True, sgr=False) == "\x1b[M`!!"
