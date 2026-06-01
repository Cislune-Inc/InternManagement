from agent.demo import build_live_demo_messages


def test_build_live_demo_messages() -> None:
    messages = build_live_demo_messages("Alex")
    assert len(messages) >= 6
    assert messages[0].startswith("[Live demo]")
    assert "clocked in" in messages[1]
