import asyncio
import re
from types import SimpleNamespace

from agent.slack_work_intake import SlackWorkIntake
from agent.state_store import StateStore
from agent.work_sharing import WorkSharing, handle


def test_confirmed_handoff_preserves_sources_and_never_changes_time(tmp_path):
    store = StateStore(tmp_path / "state.db")
    intake, sharing = SlackWorkIntake(store), WorkSharing(store)
    n = 0

    def send(text):
        nonlocal n
        n += 1
        return intake.handle(actor_id="WORKER", actor_name="Worker", is_manager=False, text=text, event_id=str(n))

    send("work GRASP: compare wheel runs")
    assert "No result" in sharing.preview("WORKER")
    send("work edit corrected plan only")
    assert "No result" in sharing.preview("WORKER")
    send("work update Saved the wheel comparison; <!channel> still needs review")
    send("work next Review the plot with the manager")
    draft = sharing.preview("WORKER")
    ident = re.search(r"SH-[a-f0-9]+", draft)[0]
    assert "&lt;!channel&gt;" in draft and "Review the plot" in draft
    assert sharing.preview("WORKER") == draft
    assert sharing.confirmed() == []
    assert "not found" in sharing.confirm("OTHER", ident)
    assert "Confirmed" in sharing.confirm("WORKER", ident)
    assert len(sharing.confirmed()) == 1
    assert sharing.confirmed(actor="OTHER") == []
    send("work update Corrected result: one wheel run failed")
    assert "changed" in sharing.confirm("WORKER", ident)
    assert sharing.confirmed() == []
    assert "one wheel run failed" in sharing.preview("WORKER")
    send("work project cisort")
    assert "No result" in sharing.preview("WORKER")
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_sharing_transport_is_private_and_publishing_is_disabled(tmp_path):
    sent = []

    async def post(channel, text):
        sent.append((channel, text))

    runtime = SimpleNamespace(state_store=StateStore(tmp_path / "state.db"),
        config=SimpleNamespace(admin_discord_user_id="OWNER"),
        admin_profile_by_slack_user_id=lambda actor: None,
        slack=SimpleNamespace(post_message=post))
    assert asyncio.run(handle(runtime, "WORKER", "work publish SH-123 #hq"))
    assert sent[0][0] == "WORKER" and "disabled" in sent[0][1]
    assert not asyncio.run(handle(runtime, "WORKER", "clock in onsite"))


def test_sharing_requires_own_current_preview_and_explicit_route(tmp_path):
    store = StateStore(tmp_path / "state.db")
    intake, service = SlackWorkIntake(store), WorkSharing(store)
    def send(text, n):
        intake.handle(actor_id="W", actor_name="Worker", is_manager=False, text=text, event_id=str(n))
    send("work GRASP: compare wheel runs", 1)
    send("work update Saved the comparison plot and found one mismatch", 2)
    send("work next Review mismatch with manager", 3)
    channels = {"grasp": "C123456789"}
    draft = service.preview("W", channels)
    ident = re.search(r"SH-[a-f0-9]+", draft)[0]
    sent = []
    async def post(channel, text):
        sent.append((channel, text))
        return {"ts": "100.1"}
    slack = SimpleNamespace(post_message=post)
    assert "preview" in asyncio.run(service.share("OTHER", ident, channels, slack))
    assert "destination" in asyncio.run(service.share("W", ident, {}, slack))
    assert not sent
    assert "Shared" in asyncio.run(service.share("W", ident, channels, slack))
    assert "already shared" in asyncio.run(service.share("W", ident, channels, slack))
    assert len(sent) == 1 and sent[0][0] == "C123456789"
    assert "compare wheel runs" not in sent[0][1]  # no private proposal
    send("work update Corrected comparison result after reviewing run inputs", 4)
    assert "changed" in asyncio.run(service.share("W", ident, channels, slack))
    new_ident = re.search(r"SH-[a-f0-9]+", service.preview("W", channels))[0]
    assert "30 minutes" in asyncio.run(service.share("W", new_ident, channels, slack))


def test_uncertain_delivery_is_not_retried(tmp_path):
    store = StateStore(tmp_path / "state.db")
    intake, service = SlackWorkIntake(store), WorkSharing(store)
    for i, text in enumerate(["work GRASP: compare runs", "work update Saved comparison and identified a mismatch", "work next Review plot"]):
        intake.handle(actor_id="W", actor_name="Worker", is_manager=False, text=text, event_id=str(i))
    routes = {"grasp": "C123456789"}
    ident = re.search(r"SH-[a-f0-9]+", service.preview("W", routes))[0]
    attempts = []
    async def fail(*args):
        attempts.append(args)
        raise TimeoutError()
    slack = SimpleNamespace(post_message=fail)
    assert "could not confirm" in asyncio.run(service.share("W", ident, routes, slack))
    assert "manager verification" in asyncio.run(service.share("W", ident, routes, slack))
    assert len(attempts) == 1
