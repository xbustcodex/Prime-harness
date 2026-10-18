import json
import time

from prime_harness.persistence import AppState, PanelConfig, load_state, save_state
from prime_harness.profiles import default_profiles, find_profile
from prime_harness.pty_session import PtySession, SessionState
from prime_harness.supervisor import Supervisor


def test_commands_are_routed_only_to_their_session():
    sup = Supervisor()
    sup.register("1", "Terminal 1")
    sup.register("2", "Terminal 2")
    sup.enqueue("1", "run tests")
    assert [c.text for c in sup.take_deliverable("1")] == ["run tests"]
    assert sup.take_deliverable("2") == []


def test_consequential_commands_need_approval():
    sup = Supervisor()
    sup.register("1", "Terminal 1")
    command = sup.enqueue("1", "rm -rf build")
    assert command.consequential
    assert sup.take_deliverable("1") == []
    assert sup.approve(command.command_id)
    assert [c.text for c in sup.take_deliverable("1")] == ["rm -rf build"]


def test_rejected_command_is_never_delivered():
    sup = Supervisor()
    sup.register("1", "Terminal 1")
    command = sup.enqueue("1", "git push --force")
    assert sup.reject(command.command_id)
    assert sup.take_deliverable("1") == []


def test_pause_holds_queue_and_resume_releases_it():
    sup = Supervisor()
    sup.register("1", "Terminal 1")
    sup.enqueue("1", "step one")
    sup.set_paused("1", True)
    assert sup.take_deliverable("1") == []
    sup.set_paused("1", False)
    assert len(sup.take_deliverable("1")) == 1


def test_chat_parsing_and_notifications():
    sup = Supervisor()
    assert sup.parse_chat("/approve 7") == ("approve", "7")
    assert sup.parse_chat("hello there") == ("send", "hello there")
    sup.register("3", "Terminal 3")
    sup.notify("3", "failed", "exit 1")
    assert sup.unread_count() == 1
    sup.mark_all_read()
    assert sup.unread_count() == 0


def test_persistence_round_trip(tmp_path):
    path = tmp_path / "state.json"
    state = AppState(
        profiles=default_profiles(),
        panels=[PanelConfig("1", "omp", "C:/work"), PanelConfig("5", "cmd", "")],
        supervisor_history=["line one"],
        default_cwd="C:/work",
    )
    save_state(path, state)
    loaded = load_state(path)
    assert [p.panel_id for p in loaded.panels] == ["1", "5"]
    assert loaded.panels[0].cwd == "C:/work"
    assert loaded.supervisor_history == ["line one"]
    assert find_profile(loaded.profiles, "primepi") is not None


def test_corrupt_state_is_set_aside(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    state = load_state(path)
    assert state.panels == []
    assert (tmp_path / "state.corrupt.json").exists()
    assert not path.exists()


def test_builtin_profiles_present_and_custom_profile_loads(tmp_path):
    path = tmp_path / "state.json"
    custom = {"profile_id": "mine", "name": "Mine", "argv": ["python", "-V"], "env": {"A": "1"}}
    path.write_text(json.dumps({"profiles": [custom]}), encoding="utf-8")
    state = load_state(path)
    ids = {p.profile_id for p in state.profiles}
    assert {"omp", "primepi", "powershell", "cmd", "mine"} <= ids


class _FakeProcess:
    """Fake PtyProcess. With exit_on_empty=False it stays alive until terminated."""

    def __init__(self, chunks, exit_on_empty=True):
        self._chunks = list(chunks)
        self._exit_on_empty = exit_on_empty
        self.pid = 4242
        self.written: list[str] = []
        self.size = None
        self.exitstatus = 0
        self._alive = True

    def read(self, _n):
        if self._chunks:
            return self._chunks.pop(0)
        if self._exit_on_empty:
            self._alive = False
            raise EOFError
        time.sleep(0.01)
        return ""

    def isalive(self):
        return self._alive

    def write(self, data):
        self.written.append(data)

    def setwinsize(self, rows, cols):
        self.size = (rows, cols)

    def terminate(self, force=False):
        self._alive = False


def _fake_spawner(process):
    def spawn(argv, cwd=None, env=None, dimensions=None):
        spawn.calls.append({"argv": argv, "cwd": cwd, "env": env, "dimensions": dimensions})
        return process

    spawn.calls = []  # type: ignore[attr-defined]
    return spawn


def test_session_reads_output_and_reports_exit():
    process = _FakeProcess(["hello ", "world"])
    spawner = _fake_spawner(process)
    session = PtySession(
        "s1", ["cmd"], cwd="C:/x", env={"PH_TEST": "1"}, rows=10, cols=40, spawner=spawner
    )
    assert session.start()
    deadline = time.monotonic() + 2
    while session.state is SessionState.RUNNING and time.monotonic() < deadline:
        time.sleep(0.01)
    output = session.drain()
    assert output == "hello world"
    assert session.state is SessionState.EXITED
    assert session.exit_code == 0
    call = spawner.calls[0]
    assert call["env"]["PH_TEST"] == "1"
    assert call["dimensions"] == (10, 40)


def test_session_write_and_resize_only_while_running():
    process = _FakeProcess([], exit_on_empty=False)
    session = PtySession("s2", ["cmd"], spawner=_fake_spawner(process))
    assert not session.write("dir\r")
    session.start()
    assert session.write("dir\r")
    session.resize(30, 120)
    assert process.size == (30, 120)
    assert process.written == ["dir\r"]


def test_session_launch_failure_is_reported():
    def broken(argv, cwd=None, env=None, dimensions=None):
        raise FileNotFoundError("nope")

    session = PtySession("s3", ["missing"], spawner=broken)
    assert not session.start()
    assert session.state is SessionState.FAILED
    assert "nope" in session.error
