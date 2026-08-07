from agent import slack_receiver


def test_slack_web_client_uses_verified_ssl_context(monkeypatch) -> None:
    ssl_context = object()
    monkeypatch.setattr(slack_receiver, "build_ssl_context", lambda: ssl_context)

    client = slack_receiver.build_slack_web_client("xoxb-test")

    assert client.token == "xoxb-test"
    assert client.ssl is ssl_context
