from __future__ import annotations

from ops.apply_production_controls import apply_controls


def test_production_controls_disable_practice_channel() -> None:
    payload = {
        "slack": {
            "practice_channel_id": "C0B6JU2PQ8N",
        }
    }

    changed = apply_controls(payload)

    assert payload["slack"]["practice_channel_id"] is None
    assert "slack.practice_channel_id" in changed
