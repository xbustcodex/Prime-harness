"""End-to-end tests for the authenticated external-chat connectivity layer."""

import pytest

from prime_harness.control_room import ControlRoom
from prime_harness.mcp_api import create_control_api
from prime_harness.supervisor import Supervisor


@pytest.fixture
def room():
    supervisor = Supervisor()
    for terminal_id in ("1", "2", "3", "4"):
        supervisor.register(terminal_id, f"Terminal {terminal_id}")
    control = ControlRoom(supervisor, public_base="http://testserver")
    for terminal_id in ("1", "2", "3", "4"):
        control.register_terminal(terminal_id)
        control.set_status(terminal_id, "running · cmd")
        control.publish_output(terminal_id, f"screen line A for {terminal_id}\nscreen line B")
    return control


@pytest.fixture
def client(room):
    from fastapi.testclient import TestClient

    return TestClient(create_control_api(room))


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_sessions_discovery_and_output(client, room):
    token, _url = room.pairings.pair("1")
    response = client.get("/api/sessions", headers=_auth(token))
    assert response.status_code == 200
    sessions = response.json()["sessions"]
    assert [s["terminal_id"] for s in sessions] == ["1"]
    output = client.get("/api/sessions/1/output?lines=10", headers=_auth(token))
    assert output.status_code == 200
    assert "screen line A for 1" in output.json()["lines"]


def test_missing_and_invalid_tokens_are_rejected(client, room):
    assert client.get("/api/sessions").status_code == 401
    assert client.get("/api/sessions", headers=_auth("nope")).status_code == 401
    token, _url = room.pairings.pair("1")
    assert client.get("/api/sessions", headers=_auth(token)).status_code == 200


def test_routing_isolation_cross_terminal_is_403(client, room):
    token, _url = room.pairings.pair("1", label="t1")
    assert client.get("/api/sessions/2/output", headers=_auth(token)).status_code == 403
    assert client.get("/api/sessions/2", headers=_auth(token)).status_code == 403
    sent = client.post("/api/sessions/2/commands", json={"text": "echo hi"}, headers=_auth(token))
    assert sent.status_code == 403
    assert room.supervisor.queue_for("2") == []


def test_token_revocation_invalidates_immediately(client, room):
    token, _url = room.pairings.pair("1")
    assert client.get("/api/sessions", headers=_auth(token)).status_code == 200
    assert room.pairings.revoke_terminal("1") == 1
    assert client.get("/api/sessions", headers=_auth(token)).status_code == 401
    assert client.get("/pair/" + token).status_code == 401


def test_reconnection_same_token_keeps_cursors(client, room):
    token, _url = room.pairings.pair("1")
    first = client.get("/api/events?since=0&terminal=1", headers=_auth(token))
    assert first.status_code == 200
    cursor = first.json()["cursor"]
    # A new client (simulating a phone reconnecting) resumes from its cursor.
    room.publish_output("1", "new output after reconnect")
    room.publish("notification", "1", "background job finished")
    second = client.get(f"/api/events?since={cursor}&terminal=1", headers=_auth(token))
    events = second.json()["events"]
    assert any(e["kind"] == "notification" for e in events)
    assert all(e["terminal_id"] == "1" for e in events)
    advanced = second.json()["cursor"]
    third = client.get(f"/api/events?since={advanced}&terminal=1", headers=_auth(token))
    assert third.json()["events"] == []  # nothing new; cursor advanced


def test_supervisor_scope_token_sees_all_but_commands_need_explicit_target(client, room):
    token, _url = room.pairings.pair(None, label="supervisor")
    sessions = client.get("/api/sessions", headers=_auth(token)).json()["sessions"]
    assert [s["terminal_id"] for s in sessions] == ["1", "2", "3", "4"]
    sent = client.post(
        "/api/sessions/3/commands", json={"text": "echo from supervisor"}, headers=_auth(token)
    )
    assert sent.status_code == 202
    assert [c.text for c in room.supervisor.queue_for("3")] == ["echo from supervisor"]
    assert room.supervisor.queue_for("1") == []


def test_command_queue_and_approval_flow(client, room):
    token, _url = room.pairings.pair("2")
    risky = client.post(
        "/api/sessions/2/commands", json={"text": "git push --force"}, headers=_auth(token)
    )
    assert risky.status_code == 202
    body = risky.json()
    assert body["needs_approval"] is True
    # Held at approval; never delivered.
    assert room.supervisor.take_deliverable("2") == []
    approvals = client.get("/api/approvals", headers=_auth(token)).json()["approvals"]
    assert [a["command_id"] for a in approvals] == [body["command_id"]]
    decided = client.post(f"/api/approvals/{body['command_id']}/approve", headers=_auth(token))
    assert decided.status_code == 200
    delivered = room.supervisor.take_deliverable("2")
    assert [c.text for c in delivered] == ["git push --force"]


def test_rejected_command_is_never_delivered(client, room):
    token, _url = room.pairings.pair("2")
    sent = client.post(
        "/api/sessions/2/commands", json={"text": "rm -rf build"}, headers=_auth(token)
    )
    command_id = sent.json()["command_id"]
    assert (
        client.post(f"/api/approvals/{command_id}/reject", headers=_auth(token)).status_code == 200
    )
    assert room.supervisor.take_deliverable("2") == []


def test_approve_permission_is_enforced(room):
    token, _url = room.pairings.pair("1", permissions={"status", "output", "command"})
    from fastapi.testclient import TestClient

    client = TestClient(create_control_api(room))
    sent = client.post("/api/sessions/1/commands", json={"text": "rm -rf x"}, headers=_auth(token))
    command_id = sent.json()["command_id"]
    assert client.get("/api/approvals", headers=_auth(token)).status_code == 403
    assert (
        client.post(f"/api/approvals/{command_id}/approve", headers=_auth(token)).status_code == 403
    )


def test_event_delivery_blocks_until_event_when_wait_requested(client, room):
    import threading

    token, _url = room.pairings.pair("4")
    cursor = client.get("/api/events?since=0&terminal=4", headers=_auth(token)).json()["cursor"]
    result = {}

    def background():
        result["response"] = client.get(
            f"/api/events?since={cursor}&terminal=4&wait=5", headers=_auth(token)
        )

    thread = threading.Thread(target=background)
    thread.start()
    room.publish("notification", "4", "agent finished")
    thread.join(10)
    assert not thread.is_alive()
    events = result["response"].json()["events"]
    assert [e["message"] for e in events] == ["agent finished"]


def test_pairing_page_served_with_token_and_scope(client, room):
    token, url = room.pairings.pair("1")
    assert url == "http://testserver/pair/" + token
    page = client.get(url)
    assert page.status_code == 200
    assert f'const TOKEN = "{token}"' in page.text
    assert 'const TID = "1"' in page.text
    # A supervisor-scope pairing cannot open a terminal chat page.
    sup_token, _ = room.pairings.pair(None)
    assert client.get("/pair/" + sup_token).status_code == 403


def test_mcp_initialize_tools_and_calls(client, room):
    token, _url = room.pairings.pair("1")
    init = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init.status_code == 200
    assert init.json()["result"]["serverInfo"]["name"] == "prime-harness-control"

    listed = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert listed.status_code == 401  # tools/list requires a Bearer token

    authed = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
    named = client.post("/mcp", json=authed, headers=_auth(token))
    tool_names = {t["name"] for t in named.json()["result"]["tools"]}
    assert {
        "list_sessions",
        "get_session_output",
        "submit_command",
        "approve_command",
    } <= tool_names

    call = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "submit_command",
                "arguments": {"terminal_id": "1", "text": "echo mcp"},
            },
        },
        headers=_auth(token),
    )
    payload = call.json()["result"]
    assert payload["isError"] is False
    assert [c.text for c in room.supervisor.queue_for("1")] == ["echo mcp"]

    cross = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "submit_command",
                "arguments": {"terminal_id": "2", "text": "echo nope"},
            },
        },
        headers=_auth(token),
    )
    assert cross.json()["error"]["message"] == "Token is not paired to this terminal"
    assert cross.json()["error"]["code"] == 403
    assert room.supervisor.queue_for("2") == []


def test_mcp_rejects_unknown_method(client):
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "method": "bogus"})
    assert response.json()["error"]["code"] == -32601


def test_live_api_server_serves_over_tcp(room):
    """The real uvicorn thread serves authenticated requests on a TCP port."""
    import socket
    import time

    import httpx

    from prime_harness.mcp_api import ApiServer

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    server = ApiServer(room, "127.0.0.1", port)
    assert server.start()
    token, _url = room.pairings.pair("1")
    try:
        response = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                response = httpx.get(
                    f"http://127.0.0.1:{port}/api/sessions", headers=_auth(token), timeout=2
                )
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        assert response is not None and response.status_code == 200
        assert response.json()["sessions"][0]["terminal_id"] == "1"
        denied = httpx.get(f"http://127.0.0.1:{port}/api/sessions", timeout=2)
        assert denied.status_code == 401
    finally:
        assert server.stop(timeout=10)


def _token_from_history(history, needle: str) -> str:
    line = next(line for line in reversed(history) if needle in line)
    return line.rsplit("/pair/", 1)[1].split()[0]


def test_chat_pairing_issues_terminal_and_supervisor_tokens(tmp_path, monkeypatch):
    """The supervisor chat can issue both a terminal pairing and a supervisor pairing."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from prime_harness.desktop_app import MainWindow

    window = MainWindow(state_path=tmp_path / "state.json")
    try:
        window.handle_chat("/pair 2")
        terminal_token = _token_from_history(window.supervisor.history, "[2] pairing link")
        record = window.room.pairings.authenticate(terminal_token)
        assert record is not None and record.terminal_id == "2"

        window.handle_chat("/pair all")
        supervisor_token = _token_from_history(window.supervisor.history,
                                               "Supervisor pairing link")
        supervisor_record = window.room.pairings.authenticate(supervisor_token)
        assert supervisor_record is not None and supervisor_record.terminal_id is None

        from fastapi.testclient import TestClient

        from prime_harness.mcp_api import create_control_api

        client = TestClient(create_control_api(window.room))
        seen = client.get("/api/sessions", headers=_auth(supervisor_token))
        assert seen.status_code == 200
        assert len(seen.json()["sessions"]) == 4  # all terminals, contexts separate

        window.handle_chat("/revoke all")
        assert window.room.pairings.authenticate(supervisor_token) is None
        assert client.get("/api/sessions", headers=_auth(supervisor_token)).status_code == 401
    finally:
        window.close()


# ------------------------------------------------------------- clipboard
class _FakeSession:
    running = True

    def __init__(self):
        self.written: list[str] = []

    def write(self, data: str) -> None:
        self.written.append(data)


class FakeClipboard:
    """Deterministic stand-in for QApplication.clipboard() (offscreen-safe)."""

    _text = ""

    @classmethod
    def setText(cls, text: str) -> None:  # noqa: N802 - Qt API
        cls._text = text

    @classmethod
    def text(cls) -> str:
        return cls._text


@pytest.fixture
def fake_clipboard(monkeypatch):
    monkeypatch.setattr(
        "PySide6.QtWidgets.QApplication.clipboard", staticmethod(lambda: FakeClipboard)
    )
    return FakeClipboard


@pytest.fixture
def view(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from prime_harness.desktop_app import TerminalView

    widget = TerminalView("1")
    widget.resize(400, 200)
    widget.vt.feed("hello world\r\nsecond line\r\nthird line")
    widget.session = _FakeSession()
    return widget


def _drag(widget, from_cell, to_cell):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    def at(cell):
        # cell is (row, col); QPointF is (x, y) = (col, row).
        return QPointF(cell[1] * widget.cell_w + 2, cell[0] * widget.cell_h + 2)

    def event(kind, pos, button, buttons):
        return QMouseEvent(kind, pos, pos, pos, button, buttons, Qt.KeyboardModifier.NoModifier)

    widget.mousePressEvent(
        event(
            QEvent.Type.MouseButtonPress,
            at(from_cell),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
    )
    widget.mouseMoveEvent(
        event(
            QEvent.Type.MouseMove, at(to_cell), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton
        )
    )
    widget.mouseReleaseEvent(
        event(
            QEvent.Type.MouseButtonRelease,
            at(to_cell),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
    )


def test_mouse_drag_selection_and_copy(view, fake_clipboard):
    _drag(view, (0, 0), (0, 4))
    assert view.selected_text() == "hello"
    assert view.copy_selection()
    assert fake_clipboard.text() == "hello"


def test_ctrl_c_with_selection_copies_instead_of_sigint(view, fake_clipboard):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    def key(k, text):
        return QKeyEvent(QEvent.Type.KeyPress, k, Qt.KeyboardModifier.ControlModifier, text)

    _drag(view, (0, 0), (0, 4))
    view.keyPressEvent(key(Qt.Key.Key_C, "\x03"))
    assert fake_clipboard.text() == "hello"
    assert view.session.written == []  # nothing went to the child
    assert view.selected_text() == ""  # selection cleared after copy
    # Without a selection, Ctrl+C still sends SIGINT to the child.
    view.keyPressEvent(key(Qt.Key.Key_C, "\x03"))
    assert view.session.written == ["\x03"]


def test_selection_click_does_not_select(view):
    _drag(view, (1, 2), (1, 2))
    assert view.selected_text() == ""


def test_selection_cleared_on_typing(view):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    _drag(view, (0, 0), (0, 4))
    assert view.selected_text() != ""
    view.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, "a")
    )
    assert view.selected_text() == ""
    assert view.session.written == ["a"]


def test_bracketed_paste_still_works(view, fake_clipboard):
    fake_clipboard.setText("paste me")
    view.paste_from_clipboard()
    assert view.session.written == ["paste me"]  # bracketed mode off
    view.vt.feed("\x1b[?2004h")
    view.paste_from_clipboard()
    assert view.session.written[-1] == "\x1b[200~paste me\x1b[201~"
