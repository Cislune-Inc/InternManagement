import asyncio
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from agent.kiosk_pins import KioskPins
from agent.onsite_kiosk import ORIGIN, create_app
from agent.slack_beta import ledger
from test_slack_beta import runtime, event

PIN = "839271"  # Synthetic credential, never deployed.


def enroll(runtime, actor="WORKER"):
    pins = KioskPins(runtime.state_store)
    pins.allow_setup(actor, authorized_by="test")
    pins.set_pin(actor, PIN, PIN)
    return pins


def test_setup_is_scoped_expiring_single_use_and_does_not_record_time(runtime):
    pins = KioskPins(runtime.state_store)
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="not open"):
        pins.set_pin("WORKER", PIN, PIN)
    pins.allow_setup("WORKER", authorized_by="test", now=now)
    with pytest.raises(ValueError):
        pins.set_pin("OTHER", PIN, PIN, now=now)
    with pytest.raises(ValueError):
        pins.set_pin("WORKER", PIN, PIN, now=now + timedelta(minutes=10))
    with pytest.raises(ValueError):
        pins.set_pin("WORKER", "123456", "123456", now=now)
    pins.set_pin("WORKER", PIN, PIN, now=now)
    assert not pins.setup_allowed("WORKER")
    with pytest.raises(ValueError):
        pins.set_pin("WORKER", "238197", "238197", now=now)
    pins.verify("WORKER", PIN)
    with runtime.state_store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        row = conn.execute("SELECT salt,digest FROM kiosk_pins").fetchone()
        assert len(row[0]) == 16 and len(row[1]) == 32 and PIN.encode() not in row[1]


def test_lockout_persists_across_restart_and_reset_preserves_old_pin_until_saved(runtime):
    pins = enroll(runtime)
    now = datetime.now(timezone.utc)
    for _ in range(5):
        with pytest.raises(ValueError):
            pins.verify("WORKER", "111111", now=now)
    with pytest.raises(ValueError, match="locked"):
        KioskPins(runtime.state_store).verify("WORKER", PIN, now=now)
    pins.verify("WORKER", PIN, now=now + timedelta(minutes=15))
    pins.allow_setup("WORKER", authorized_by="test")
    pins.verify("WORKER", PIN)
    pins.set_pin("WORKER", "273819", "273819")
    with pytest.raises(ValueError):
        pins.verify("WORKER", PIN)
    pins.verify("WORKER", "273819")


@pytest.mark.parametrize("pin", ["42", "073", "8372", "83729", PIN])
def test_keyboard_pin_lengths_preserve_leading_zero_and_existing_hashes(runtime, pin):
    pins = enroll(runtime)
    pins.verify("WORKER", PIN)  # Existing six-digit hashes need no migration.
    pins.allow_setup("WORKER", authorized_by="test")
    pins.set_pin("WORKER", pin, pin)
    KioskPins(runtime.state_store).verify("WORKER", pin)
    if pin.startswith("0"):
        with pytest.raises(ValueError, match="not recognized"):
            pins.verify("WORKER", pin.lstrip("0"))


@pytest.mark.parametrize("pin", ["", "4", "8372941", "42\n", " 42", "４２", "ab", "4.2", "00", "12", "432"])
def test_invalid_or_obvious_short_pin_is_rejected_without_consuming_setup(runtime, pin):
    pins = KioskPins(runtime.state_store)
    pins.allow_setup("WORKER", authorized_by="test")
    with pytest.raises(ValueError):
        pins.set_pin("WORKER", pin, pin)
    assert pins.setup_allowed("WORKER")


def test_short_pin_mismatch_and_failed_attempts_preserve_lockout(runtime):
    pins = KioskPins(runtime.state_store)
    pins.allow_setup("WORKER", authorized_by="test")
    with pytest.raises(ValueError, match="same"):
        pins.set_pin("WORKER", "42", "43")
    pins.set_pin("WORKER", "42", "42")
    now = datetime.now(timezone.utc)
    for _ in range(5):
        with pytest.raises(ValueError, match="not recognized"):
            pins.verify("WORKER", "43", now=now)
    with pytest.raises(ValueError, match="locked"):
        KioskPins(runtime.state_store).verify("WORKER", "42", now=now)
    pins.verify("WORKER", "42", now=now + timedelta(minutes=15))


def test_slack_setup_cannot_target_someone_else_or_start_time(runtime):
    request = {**event("kiosk setup"), "ts": str(datetime.now(timezone.utc).timestamp())}
    asyncio.run(runtime.handle_slack_direct_message(None, request))
    pins = KioskPins(runtime.state_store)
    assert pins.setup_allowed("WORKER") and not pins.setup_allowed("ERIK")
    asyncio.run(runtime.handle_slack_direct_message(None, event("kiosk setup ERIK", 10)))
    assert not pins.setup_allowed("ERIK")
    # Delayed transport retries cannot open a fresh ten-minute window.
    old = {**event("kiosk setup"), "ts": str((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())}
    asyncio.run(runtime.handle_slack_direct_message(None, old))
    assert not pins.setup_allowed("WORKER")
    with runtime.state_store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_kiosk_clock_and_meal_return_require_presence_but_out_does_not(runtime):
    clock, user = ledger(runtime), runtime.roster_by_key["worker"]
    now = datetime.fromisoformat("2026-09-07T09:00:00-07:00")
    def act(command, hours=0, detail="", verified=False):
        return clock.handle(user, command, detail, event_id=f"{command}:{hours}:{verified}",
                            now=now + timedelta(hours=hours), kiosk_verified=verified)
    assert "KIOSK_REQUIRED" in act("in", detail="onsite")[0]
    assert "advance approval" in act("in", detail="remote", hours=.01)[0]
    assert "Clocked in" in act("in", detail="onsite", verified=True, hours=.02)[0]
    act("lunch", 3)
    assert "KIOSK_REQUIRED" in act("back", 3.5)[0]
    assert "running again" in act("back", 3.5, verified=True)[0]
    assert "Clocked out" in act("out", 4)[0]


async def headers(client):
    host = {"Host": "127.0.0.1:8766"}
    page = await client.get("/", headers=host)
    text = await page.text()
    token = re.search(r'csrf="([^"]+)"', text)[1]
    assert "Choose your name" in text and 'type="password"' in text
    assert text.count('pattern="[0-9]{2,6}"') == 2
    assert text.count('minlength="2"') == 2
    assert "keypad" not in text and "six-digit" not in text
    assert "keyboard" in text and "Four or more digits recommended" in text
    assert "Today " not in text and PIN not in text
    return {**host, "Origin": ORIGIN, "X-Kiosk-CSRF": token}


@pytest.mark.parametrize("pin", ["42", "073", PIN])
def test_http_setup_and_real_clock_cycle_retry_without_sends(runtime, pin):
    async def exercise():
        async with TestClient(TestServer(create_app(runtime))) as client:
            h = await headers(client)
            payload = {"actor": "WORKER", "pin": pin, "confirmation": pin, "action": "start", "request_id": str(uuid4())}
            assert (await client.post("/confirm", headers={"Host": "127.0.0.1:8766"}, json=payload)).status == 403
            assert (await client.post("/pin/setup", headers=h, json=payload)).status == 400
            KioskPins(runtime.state_store).allow_setup("WORKER", authorized_by="test")
            assert (await client.post("/pin/setup", headers=h, json=payload)).status == 200
            response = await client.post("/confirm", headers=h, json=payload)
            assert response.status == 200 and "Clocked in" in (await response.json())["message"]
            stopped = await client.post("/confirm", headers=h, json={**payload, "action": "out", "request_id": str(uuid4())})
            assert "Clocked out" in (await stopped.json())["message"]
            # A delayed retry must not reopen after a subsequent stop.
            assert (await client.post("/confirm", headers=h, json=payload)).status == 200
            with runtime.state_store._connect() as conn:
                session = ledger(runtime)._sessions(conn, "worker")[0]
                assert session.clocked_out_at
                assert '"pin"' not in str(session.metadata) and "confirmation" not in str(session.metadata)
                if len(pin) == 6:
                    assert pin not in str(session.metadata)
            assert runtime.test_sent == []
    asyncio.run(exercise())


def test_offboarded_and_wrong_pin_cannot_start_and_http_limits_apply(runtime):
    enroll(runtime)
    async def exercise():
        async with TestClient(TestServer(create_app(runtime))) as client:
            h = await headers(client)
            payload = {"actor": "WORKER", "pin": "111111", "action": "start", "request_id": str(uuid4())}
            assert (await client.post("/confirm", headers=h, json=payload)).status == 400
            runtime.roster_by_key["worker"].active = False
            for _ in range(19):
                assert (await client.post("/confirm", headers=h, json={**payload, "pin": PIN})).status == 400
            assert (await client.post("/confirm", headers=h, json=payload)).status == 429
            assert not runtime.test_archives
    asyncio.run(exercise())


def test_foreign_host_and_forwarded_address_are_rejected(runtime):
    async def exercise():
        app = create_app(runtime)
        request = make_mocked_request("GET", "/", headers={"Host": "127.0.0.1:8766", "X-Forwarded-For": "127.0.0.1"})
        with pytest.raises(web.HTTPForbidden):
            await app.middlewares[0](request, lambda request: None)
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/", headers={"Host": "evil.example"})).status == 403
    asyncio.run(exercise())
