"""Local kiosk credentials. PINs never enter Slack, logs, URLs or clock events."""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any


class KioskPins:
    def __init__(self, store: Any) -> None:
        self.store = store
        with store._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS kiosk_pins (
                    actor TEXT PRIMARY KEY, salt BLOB NOT NULL, digest BLOB NOT NULL,
                    failures INTEGER NOT NULL DEFAULT 0, locked_until TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS kiosk_pin_setup (
                    actor TEXT PRIMARY KEY, expires_at TEXT NOT NULL, authorized_by TEXT NOT NULL
                );
            """)

    @staticmethod
    def _digest(pin: str, salt: bytes) -> bytes:
        return hashlib.scrypt(pin.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)

    def allow_setup(self, actor: str, *, authorized_by: str, now: datetime | None = None) -> None:
        """Call only after Slack identity verification or trusted operator approval."""
        now = now or datetime.now(timezone.utc)
        with self.store._connect() as conn:
            conn.execute("INSERT INTO kiosk_pin_setup VALUES (?,?,?) ON CONFLICT(actor) DO UPDATE SET expires_at=excluded.expires_at,authorized_by=excluded.authorized_by",
                         (actor, (now + timedelta(minutes=10)).isoformat(), authorized_by))

    def setup_allowed(self, actor: str, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        with self.store._connect() as conn:
            row = conn.execute("SELECT expires_at FROM kiosk_pin_setup WHERE actor=?", (actor,)).fetchone()
            return bool(row and datetime.fromisoformat(row[0]) > now)

    def set_pin(self, actor: str, pin: str, confirmation: str, *, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        if not re.fullmatch(r"[0-9]{6}", pin) or pin != confirmation:
            raise ValueError("Enter the same six-digit PIN twice.")
        if len(set(pin)) == 1 or pin in {"123456", "654321", "012345", "543210"}:
            raise ValueError("Choose a less obvious PIN, not repeated or sequential digits.")
        salt = secrets.token_bytes(16)
        digest = self._digest(pin, salt)
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            grant = conn.execute("SELECT expires_at FROM kiosk_pin_setup WHERE actor=?", (actor,)).fetchone()
            if not grant or datetime.fromisoformat(grant[0]) <= now:
                raise ValueError("Setup is not open. Ask Erik to verify you, or send `kiosk setup` to DP from your own Slack account, then return here.")
            conn.execute("INSERT INTO kiosk_pins VALUES (?,?,?,0,NULL,?) ON CONFLICT(actor) DO UPDATE SET salt=excluded.salt,digest=excluded.digest,failures=0,locked_until=NULL,updated_at=excluded.updated_at",
                         (actor, salt, digest, now.isoformat()))
            conn.execute("DELETE FROM kiosk_pin_setup WHERE actor=?", (actor,))

    def verify(self, actor: str, pin: str, *, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        error = ""
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM kiosk_pins WHERE actor=?", (actor,)).fetchone()
            if not row:
                error = "Your PIN needs one-time setup. Ask Erik, or send `kiosk setup` to DP from your own Slack account."
            elif row["locked_until"] and datetime.fromisoformat(row["locked_until"]) > now:
                error = "PIN temporarily locked. Wait 15 minutes or verify a reset with Erik. Actual hours can still be reported in Slack."
            elif not re.fullmatch(r"[0-9]{6}", pin) or not secrets.compare_digest(self._digest(pin, row["salt"]), row["digest"]):
                failures = (0 if row["locked_until"] else row["failures"]) + 1
                until = (now + timedelta(minutes=15)).isoformat() if failures >= 5 else None
                conn.execute("UPDATE kiosk_pins SET failures=?,locked_until=? WHERE actor=?", (failures, until, actor))
                error = "PIN not recognized. Five failed attempts temporarily lock this PIN."
            else:
                conn.execute("UPDATE kiosk_pins SET failures=0,locked_until=NULL WHERE actor=?", (actor,))
        if error:
            raise ValueError(error)  # After commit, so failure counters survive.
