import asyncio
from datetime import datetime

from agent.demo import DemoClient, DemoDM, build_live_demo_messages


def test_build_live_demo_messages() -> None:
    messages = build_live_demo_messages("Alex")
    assert len(messages) >= 6
    assert messages[0].startswith("[Live demo]")
    assert "clocked in" in messages[1]


def test_demo_dm_send_accepts_view_argument() -> None:
    client = DemoClient(bot_user_id=999, start_time=datetime(2026, 6, 14, 9, 0, 0))
    dm = DemoDM(client, user_id=111)

    sent = asyncio.run(dm.send("hello", view=None))

    assert sent.content == "hello"
    assert client.outbox == [(111, client.now, "hello")]
