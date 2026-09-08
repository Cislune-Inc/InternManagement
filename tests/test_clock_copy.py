"""Routine copy stays concise; the same clock and correction controls remain."""
from agent.slack_timekeeping import HELP, paid_seconds
from test_slack_timekeeping import at, clock, command, user


def test_routine_messages_are_direct_without_repeated_correction_footer(clock, user):
    messages = []
    for action, time, detail in [
        ("in", at(9), "onsite"), ("rest", at(11), ""),
        ("back", at(11, 1), ""), ("back", at(11, 10), ""),
        ("lunch", at(12), ""), ("back", at(12, 30), ""),
        ("hours", at(13), ""), ("out", at(14), ""),
    ]:
        response, session = command(clock, user, action, time, detail)
        messages.append(response)
    for response in messages:
        assert "report hours" not in response
        for removed in ("look compliant", "actually worked", "all work must", "guessed timestamps", "not retroactively"):
            assert removed not in response
    assert "09:00" in messages[0]
    assert "09:00 remaining" in messages[2]
    assert paid_seconds([session], at(14)) == 270 * 60
    assert "report hours" in HELP
    assert "saved for manager review" in command(clock, user, "report", at(14, 1), "Review start time")[0]
    assert len(clock.reports()) == 1


def test_deadline_notice_keeps_action_without_disclaimer(clock, user):
    command(clock, user, "in", at(9), "onsite")
    notices, session = clock.tick(user, at(14))
    assert session.clocked_out_at
    assert "Stop work now" in notices[0] and "Reply `lunch`" in notices[0]
    assert "report hours" not in notices[0]
    assert session.metadata["compliance_events"][0]["confirmation"] == "stop_instruction_not_proof_of_stopped_work"
