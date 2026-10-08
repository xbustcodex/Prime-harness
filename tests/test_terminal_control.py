from pathlib import Path

import pytest

from prime_harness.terminal_control import TerminalManager


def test_tokens_are_unique_and_scoped(tmp_path: Path) -> None:
    manager = TerminalManager(cwd=tmp_path)
    first = manager.rotate_token(1)
    second = manager.rotate_token(2)
    assert first != second
    assert manager.authorized(1, first)
    assert not manager.authorized(2, first)
    assert not manager.authorized(1, second)
    rotated = manager.rotate_token(1)
    assert not manager.authorized(1, first)
    assert manager.authorized(1, rotated)


def test_unknown_slot_and_input_limits(tmp_path: Path) -> None:
    manager = TerminalManager(cwd=tmp_path)
    with pytest.raises(KeyError):
        manager.slot(7)
    with pytest.raises(RuntimeError):
        manager.write(1, "omp\r")
    with pytest.raises(ValueError):
        manager.resize(1, 0, 100)


def test_tokens_are_not_stored_as_plaintext(tmp_path: Path) -> None:
    manager = TerminalManager(cwd=tmp_path)
    token = manager.rotate_token(3)
    assert token not in repr(manager.slot(3))
    assert manager.slot(3).token_hash != token
