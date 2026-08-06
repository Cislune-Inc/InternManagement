from pathlib import Path

import pytest

from agent import process_lock
from agent.process_lock import SingleInstanceLock


def test_single_instance_lock_blocks_second_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / "agent.lock"
    with SingleInstanceLock(lock_path):
        with pytest.raises(RuntimeError, match="already running"):
            with SingleInstanceLock(lock_path):
                pass


def test_single_instance_lock_reclaims_stale_file(tmp_path: Path) -> None:
    lock_path = tmp_path / "agent.lock"
    lock_path.write_text('{"pid": 999999}', encoding="utf-8")
    with SingleInstanceLock(lock_path):
        assert lock_path.exists()


def test_single_instance_lock_does_not_reclaim_permission_denied_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "agent.lock"
    lock_path.write_text('{"pid": 4242}', encoding="utf-8")

    def fake_kill(_pid: int, _signal: int) -> None:
        raise PermissionError

    monkeypatch.setattr(process_lock.os, "kill", fake_kill)

    with pytest.raises(RuntimeError, match="already running"):
        with SingleInstanceLock(lock_path):
            pass


def test_single_instance_lock_reclaims_process_lookup_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "agent.lock"
    lock_path.write_text('{"pid": 4242}', encoding="utf-8")

    def fake_kill(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(process_lock.os, "kill", fake_kill)

    with SingleInstanceLock(lock_path):
        assert lock_path.exists()
