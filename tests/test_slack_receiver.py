import asyncio
from types import SimpleNamespace

from agent import slack_receiver


def test_slack_web_client_uses_verified_ssl_context(monkeypatch) -> None:
    ssl_context = object()
    monkeypatch.setattr(slack_receiver, "build_ssl_context", lambda: ssl_context)

    client = slack_receiver.build_slack_web_client("xoxb-test")

    assert client.token == "xoxb-test"
    assert client.ssl is ssl_context


def test_publish_app_home_refreshes_config_and_publishes_view() -> None:
    calls: list[tuple[str, object]] = []
    refreshed: list[bool] = []

    async def refresh_configuration() -> None:
        refreshed.append(True)

    def build_view(user_id: str) -> dict[str, object]:
        return {"type": "home", "user": user_id}

    async def views_publish(*, user_id: str, view: object) -> None:
        calls.append((user_id, view))

    runtime = SimpleNamespace(
        refresh_configuration=refresh_configuration,
        build_slack_app_home_view=build_view,
    )
    web_client = SimpleNamespace(views_publish=views_publish)

    asyncio.run(slack_receiver.publish_app_home(runtime, web_client, "UERIK"))

    assert refreshed == [True]
    assert calls == [("UERIK", {"type": "home", "user": "UERIK"})]


def test_pin_setup_button_uses_clicking_slack_identity() -> None:
    calls = []

    async def handle(client, event):
        calls.append(event)

    runtime = SimpleNamespace(handle_slack_direct_message=handle)
    asyncio.run(slack_receiver.handle_clock_action(runtime, None, {
        "user": {"id": "UACTOR"},
        "actions": [{"action_id": "dp_clock_pin_setup", "action_ts": "123.456"}],
    }))
    assert calls == [{"user": "UACTOR", "text": "kiosk setup", "event_ts": "123.456"}]
