from pathlib import Path

import pytest

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
