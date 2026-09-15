"""Protect manager credentials and DP private directories; never prints secrets."""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
from pathlib import Path

from agent.manager_auth import KEY_PATH, load_key
from agent.persistence import atomic_write_text


def protect(repo: Path, *, password: str | None = None) -> None:
    for name in ("config", "data", "storage", "backups", "secrets"):
        path = repo / name
        if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
            raise ValueError("Private DP directory must exist, be owned by the service user, and not be a symlink")
    path = repo / KEY_PATH
    if path.is_symlink():
        raise ValueError("Credential path cannot be a symlink")
    if password is not None and (not 24 <= len(password) <= 256 or not password.isascii() or any(c.isspace() for c in password)):
        raise ValueError("Use 24–256 ASCII characters without whitespace")
    for name in ("config", "data", "storage", "backups", "secrets"):
        (repo / name).chmod(0o700)
    if password is not None or not path.exists():
        atomic_write_text(path, password or secrets.token_urlsafe(36))
        path.chmod(0o600)
    if not load_key(path):
        raise ValueError("Manager credential is not valid/private; no insecure fallback permitted")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--set-password", action="store_true", help="Interactive local input only; never a command-line password")
    args = parser.parse_args()
    if not args.apply:
        print("Dry run: protect DP config/data/storage/backups/secrets as service-user-only; create manager key if absent. No roster or hours changes.")
        return
    password = None
    if args.set_password:
        password = getpass.getpass("New manager password (24+ characters): ")
        if password != getpass.getpass("Repeat manager password: "):
            raise SystemExit("Passwords differ; no changes made")
    protect(Path.cwd(), password=password)
    print("DP private directories and manager credential protected. No secret printed. Restart the dashboard after password changes.")


if __name__ == "__main__":
    main()
