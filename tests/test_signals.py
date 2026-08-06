from agent.signals import detect_signals, is_clock_out_cancellation


def test_detect_clock_in() -> None:
    result = detect_signals("Yep, I clocked in a few minutes ago.")
    assert result.clocked_in is True
    assert result.clocking_out is False


def test_detect_stuck_and_recovered() -> None:
    stuck = detect_signals("I am stuck and need help with this bug.")
    recovered = detect_signals("I fixed it and I am back on track.")
    resumed = detect_signals("I am unblocked now and ready to continue.")
    assert stuck.stuck is True
    assert recovered.recovered is True
    assert resumed.recovered is True


def test_detect_clock_out() -> None:
    result = detect_signals("I am clocking out now.")
    assert result.clocking_out is True


def test_detect_explicit_clock_out_requests() -> None:
    assert detect_signals("can i clock out?").clocking_out is True
    assert detect_signals("i have to go can i clock out?").clocking_out is True
    assert detect_signals("clock out").clocking_out is True
    assert detect_signals("Clock me out pollo").clocking_out is True
    assert detect_signals("CLOCK ME OUT DON POLLO").clocking_out is True


def test_clock_out_mentions_are_not_treated_as_requests() -> None:
    assert detect_signals("I will send my status update in the afternoon or when I clock out.").clocking_out is False
    assert detect_signals("Before I clock out, I need to finish the CAD part.").clocking_out is False


def test_detect_clock_out_cancellation() -> None:
    assert is_clock_out_cancellation("no i did not mean to clock out") is True
    assert is_clock_out_cancellation("no please i did not mean too") is True
    assert detect_signals("no i did not mean to clock out").clocking_out is False


def test_detect_lunch_start_and_end() -> None:
    starting = detect_signals("I'm going to lunch now.")
    eating = detect_signals("I want to eat lunch right now.")
    ending = detect_signals("I'm back from lunch.")
    assert starting.starting_lunch is True
    assert eating.starting_lunch is True
    assert ending.ending_lunch is True
