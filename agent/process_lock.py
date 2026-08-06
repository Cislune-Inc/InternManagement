from __future__ import annotations

import json
import os
from pathlib import Path


class SingleInstanceLock:
    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = None

    def __enter__(self) -> "SingleInstanceLock":
        try:
            self._handle = self.lock_path.open("x", encoding="utf-8")
        except FileExistsError:
            existing_pid = self._read_existing_pid()
            if existing_pid is not None and not _pid_exists(existing_pid):
                self.lock_path.unlink(missing_ok=True)
                self._handle = self.lock_path.open("x", encoding="utf-8")
            else:
                message = self._read_existing_message(existing_pid)
                raise RuntimeError(
                    "Another intern management bot process is already running.\n"
                    f"Lock file: {self.lock_path}\n"
                    f"{message}"
                ) from None
        payload = {"pid": os.getpid()}
        self._handle.write(json.dumps(payload))
        self._handle.flush()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass

    def _read_existing_pid(self) -> int | None:
        try:
            payload = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        pid = payload.get("pid")
        return int(pid) if isinstance(pid, int) else None

    def _read_existing_message(self, pid: int | None) -> str:
        if pid is None:
            return "The existing lock file could not be parsed."
        return f"Existing PID: {pid}" if pid else "The existing lock file did not include a PID."


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True
