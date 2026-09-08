import plistlib
from types import SimpleNamespace

import pytest

from ops.ensure_launchd_service import restart


def setup(tmp_path, *, disabled=False, loaded=False, state_word=None):
    label = "com.pm.internmanagement.bot"
    (tmp_path / (label + ".plist")).write_bytes(plistlib.dumps({
        "Label": label, "WorkingDirectory": str(tmp_path),
        "ProgramArguments": [str(tmp_path / ".venv/bin/python"), "-m", "agent.main"]}))
    calls = []

    def run(args, **kwargs):
        calls.append(args[1:])
        return SimpleNamespace(returncode=1 if args[1] == "print" and not loaded else 0,
                               stdout=f'"{label}" => {state_word or str(disabled).lower()}', stderr="")
    return calls, run


@pytest.mark.parametrize('state_word', ['true', 'disabled'])
def test_disabled_requires_deliberate_enable(tmp_path, state_word):
    calls, run = setup(tmp_path, disabled=True, state_word=state_word)
    with pytest.raises(ValueError, match="explicitly"):
        restart(tmp_path, "bot", tmp_path, uid=501, run=run)
    assert [x[0] for x in calls] == ["print-disabled", "print"]
    restart(tmp_path, "bot", tmp_path, uid=501, enable_disabled=True, run=run)
    assert [x[0] for x in calls[-3:]] == ["enable", "bootstrap", "kickstart"]
    assert calls[-1][-1] == "gui/501/com.pm.internmanagement.bot"


def test_loaded_enabled_only_restarts(tmp_path):
    calls, run = setup(tmp_path, loaded=True)
    restart(tmp_path, "bot", tmp_path, uid=501, run=run)
    assert [x[0] for x in calls] == ["print-disabled", "print", "kickstart"]


def test_wrong_checkout_rejected_before_launchctl(tmp_path):
    calls, run = setup(tmp_path)
    with pytest.raises(ValueError, match="checkout"):
        restart(tmp_path / "wrong", "bot", tmp_path, uid=501, run=run)
    assert not calls


def test_failure_does_not_leak_environment(tmp_path):
    setup(tmp_path)
    def run(*args, **kwargs):
        return SimpleNamespace(returncode=5, stdout="SECRET", stderr="SECRET")
    with pytest.raises(RuntimeError) as exc:
        restart(tmp_path, "bot", tmp_path, uid=501, run=run)
    assert "SECRET" not in str(exc.value)


@pytest.mark.parametrize("disabled", [True, False])
def test_manager_host_migration_preserves_disabled_gate(tmp_path, disabled):
    label = "com.pm.internmanagement.time-tracking"
    path = tmp_path / (label + ".plist")
    original = plistlib.dumps({"Label": label, "WorkingDirectory": str(tmp_path),
        "ProgramArguments": [str(tmp_path / ".venv/bin/python"), "-m", "agent.hours_editor", "--host", "0.0.0.0"]})
    path.write_bytes(original)
    calls = []
    def run(args, **kwargs):
        calls.append(args[1])
        return SimpleNamespace(returncode=0, stdout=f'"{label}" => {str(disabled).lower()}', stderr="")
    if disabled:
        with pytest.raises(ValueError, match="explicitly"):
            restart(tmp_path, "time-tracking", tmp_path, uid=501, run=run)
        assert path.read_bytes() == original
        assert calls == ["print-disabled", "print"]
    else:
        restart(tmp_path, "time-tracking", tmp_path, uid=501, run=run)
        assert plistlib.loads(path.read_bytes())["ProgramArguments"][-1] == "127.0.0.1"
        assert calls[-3:] == ["bootout", "bootstrap", "kickstart"]
