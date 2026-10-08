from pathlib import Path

from prime_harness.agents import ShellAgentProvider
from prime_harness.config import AgentConfig


def test_shell_agent_creates_checkpoint_and_can_continue(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "result.txt"
    provider = ShellAgentProvider(
        config=AgentConfig(
            agent_id="shell-1",
            provider="shell",
            command="python",
            workspace_path=workspace,
            timeout_seconds=10,
        )
    )

    session = provider.start_session()
    result = provider.send_instruction(
        session,
        f"Create {marker.name} containing 'verified' using a Python command.",
    )
    assert result.success is True
    assert result.changed_files == ["result.txt"]
    assert marker.read_text() == "verified"

    snapshot = provider.snapshot(session)
    assert snapshot.status == "completed"
    assert provider.stop_session(session) is True


def test_shell_agent_rejects_workspace_escape(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = ShellAgentProvider(
        config=AgentConfig(
            agent_id="shell-1",
            provider="shell",
            command="python",
            workspace_path=workspace,
            timeout_seconds=10,
        )
    )
    session = provider.start_session()

    result = provider.send_instruction(session, "Read /etc/passwd")

    assert result.success is False
    assert result.error is not None
    assert "workspace" in result.error.lower()
    assert provider.stop_session(session) is True
