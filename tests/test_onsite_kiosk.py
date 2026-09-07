import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from agent.onsite_kiosk import KioskCodes, ORIGIN, create_app
from agent.slack_beta import ledger
from test_slack_beta import runtime  # shared isolated fake transport fixture


def test_code_is_one_time_expiring_and_new_request_invalidates_old(runtime):
    codes = KioskCodes(runtime.state_store)
    now = datetime.now(timezone.utc)
    first = codes.issue("WORKER", "in", "onsite", now=now)
    second = codes.issue("WORKER", "in", "onsite", now=now)
    with pytest.raises(ValueError):
        codes.consume(first, now=now)
    assert codes.consume(second, now=now)["actor"] == "WORKER"
    with pytest.raises(ValueError):
        codes.consume(second, now=now)
    third = codes.issue("WORKER", "back", "", now=now)
    with pytest.raises(ValueError):
        codes.consume(third, now=now + timedelta(minutes=2))
    with runtime.state_store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert not conn.execute("SELECT digest FROM kiosk_codes WHERE digest=?", (third,)).fetchone()


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


def test_http_boundary_and_real_confirmation_without_slack_sends(runtime):
    async def exercise():
        async with TestClient(TestServer(create_app(runtime))) as client:
            host = {"Host": "127.0.0.1:8766"}
            assert (await client.get("/", headers={"Host": "evil.example"})).status == 403
            page = await client.get("/", headers=host)
            text = await page.text()
            token = re.search(r"'X-Kiosk-CSRF':\"([^\"]+)\"", text)[1]
            code = KioskCodes(runtime.state_store).issue("WORKER", "in", "onsite")
            assert (await client.post("/confirm", headers=host, json={"code": code})).status == 403
            headers = {**host, "Origin": ORIGIN, "X-Kiosk-CSRF": token}
            response = await client.post("/confirm", headers=headers, json={"code": code})
            assert response.status == 200, await response.text()
            assert "Clocked in" in (await response.json())["message"]
            assert (await client.post("/confirm", headers=headers, json={"code": code})).status == 400
            assert runtime.test_sent == []
            assert runtime.test_archives
    asyncio.run(exercise())


def test_lan_and_forwarded_addresses_cannot_pass_boundary(runtime):
    async def exercise():
        app = create_app(runtime)
        request = make_mocked_request("GET", "/", headers={"Host": "127.0.0.1:8766", "X-Forwarded-For": "127.0.0.1"})
        # The mocked request has no trusted loopback peer.
        from aiohttp import web
        with pytest.raises(web.HTTPForbidden):
            await app.middlewares[0](request, lambda request: None)
    asyncio.run(exercise())


def test_offboarded_identity_and_excess_attempts_cannot_start(runtime):
    async def exercise():
        async with TestClient(TestServer(create_app(runtime))) as client:
            host = {"Host": "127.0.0.1:8766"}
            page = await client.get("/", headers=host)
            token = re.search(r"'X-Kiosk-CSRF':\"([^\"]+)\"", await page.text())[1]
            headers = {**host, "Origin": ORIGIN, "X-Kiosk-CSRF": token}
            code = KioskCodes(runtime.state_store).issue("WORKER", "in", "onsite")
            runtime.roster_by_key["worker"].active = False
            assert (await client.post("/confirm", headers=headers, json={"code": code})).status == 400
            for _ in range(11):
                assert (await client.post("/confirm", headers=headers, json={"code": "00000000"})).status == 400
            assert (await client.post("/confirm", headers=headers, json={"code": "00000000"})).status == 429
            assert not runtime.test_archives
    asyncio.run(exercise())
