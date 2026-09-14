import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent.models import SessionState, UserProfile
from agent.state_store import StateStore
from agent.payroll_reconciliation import build, save, render, period

NOW = datetime(2026, 9, 14, 19, tzinfo=timezone.utc)


def runtime(tmp_path):
    user = UserProfile(user_key="test-worker", display_name="Test Worker", slack_user_id="test-slack")
    return SimpleNamespace(state_store=StateStore(tmp_path / "source.sqlite3"), roster_by_key={user.user_key: user},
                           config=SimpleNamespace(slack=SimpleNamespace(work_intake_beta_slack_user_ids=[user.slack_user_id])),
                           _storage_root_path=lambda: tmp_path)


def put(r, day="2026-09-08", start="2026-09-08T15:00:00+00:00", end="2026-09-08T23:00:00+00:00", **metadata):
    s = SessionState(user_key="test-worker", session_date=day, work_segments=[{"clocked_in_at": start, "clocked_out_at": end}], metadata={"slack_clock_beta": True, **metadata})
    r.state_store.save_session(s)
    return s


def test_union_meals_and_current_day_excluded(tmp_path):
    r = runtime(tmp_path)
    s = put(r, lunch_windows=[{"started_at": "2026-09-08T19:00:00+00:00", "ended_at": "2026-09-08T19:30:00+00:00"}])
    s.work_segments *= 2
    r.state_store.save_session(s)
    put(r, "2026-09-14", "2026-09-14T15:00:00+00:00", None)
    d = build(r, "2026-09-13", NOW)
    assert d["workers"][0]["seconds"] == 27000
    assert len(d["workers"][0]["days"]) == 7
    assert "does not mean zero" in d["gusto_status"]


def test_cross_midnight_split_and_unknown_tail(tmp_path):
    r = runtime(tmp_path)
    put(r, "2026-09-08", "2026-09-09T06:00:00+00:00", "2026-09-09T08:00:00+00:00")
    put(r, "2026-09-10", "2026-09-10T16:00:00+00:00", None)
    d = build(r, "2026-09-13", NOW)["workers"][0]
    assert d["seconds"] == 7200
    assert d["days"][1]["seconds"] == d["days"][2]["seconds"] == 3600
    assert any("Open interval" in i for i in d["days"][3]["issues"])


def test_manual_not_auto_and_stale_draft(tmp_path):
    r = runtime(tmp_path)
    s = put(r, slack_clock_stop_reason="worker_clock_out")
    d = build(r, "2026-09-13", NOW)["workers"][0]["days"][1]
    assert not any("Clock stop" in i for i in d["issues"])
    body = dict(week="2026-09-13", user_key="test-worker", day=d["day"], fingerprint=d["fingerprint"], note="Verified source", target_hours=8, gusto_hours=2)
    original = json.dumps(s.__dict__) if hasattr(s, "__dict__") else r.state_store.get_session(s.user_key, s.session_date).work_segments
    assert save(r, body)["saved"]
    assert r.state_store.get_session(s.user_key, s.session_date).work_segments == original
    assert build(r, "2026-09-13", NOW)["workers"][0]["days"][1]["draft_current"]
    s.work_segments[0]["clocked_out_at"] = "2026-09-08T23:01:00+00:00"
    r.state_store.save_session(s)
    assert not build(r, "2026-09-13", NOW)["workers"][0]["days"][1]["draft_current"]
    with pytest.raises(ValueError, match="Source time changed"):
        save(r, body)


def test_historical_open_not_carried_and_script_safe(tmp_path):
    r = runtime(tmp_path)
    put(r, "2026-08-07", "2026-08-07T16:00:00+00:00", None, slack_clock_legacy_unresolved=True)
    data = build(r, "2026-09-13", NOW)
    assert not any(d["evidence"] for d in data["workers"][0]["days"])
    data["source"] = "</script><script>alert(1)</script>"
    page = render(data)
    assert "</script><script>" not in page
    assert "\\r\\n" in page
    with pytest.raises(ValueError):
        period("2026-09-14", NOW)


def test_observed_gusto_and_week_drafts(tmp_path):
    r = runtime(tmp_path)
    put(r, slack_clock_stop_reason="worker_clock_out", compliance_events=[{"event_type":"meal_due", "recorded_at":"2026-09-08T20:00:00+00:00"}])
    path = tmp_path / "dashboard/payroll/2026-09-13/gusto-observed.json"
    path.parent.mkdir(parents=True)
    source = {"week_ending":"2026-09-13", "workers":{"test-worker":{"days":{f"2026-09-{n:02}":{"minutes":480 if n==8 else 0} for n in range(7,14)},"week_minutes":480}}, "worker_cases":{"test-worker":{"title":"Coverage", "evidence":"Check coverage", "options":[{"id":"confirm","label":"Confirm"}]}}}
    path.write_text(json.dumps(source))
    w = build(r, "2026-09-13", NOW)["workers"][0]
    assert any("Earlier automatic stop" in i for i in w["days"][1]["issues"])
    assert any("Both Gusto" in i for i in w["days"][1]["issues"])
    body = dict(week="2026-09-13", user_key="test-worker", day="week", fingerprint=w["week_review"]["fingerprint"], note="Owner confirms coverage", choice="confirm")
    assert save(r, body)["saved"]
    with pytest.raises(ValueError, match="cannot allocate"):
        save(r, dict(body, target_hours=10))
    source["workers"]["test-worker"]["days"]["2026-09-08"]["minutes"] = 481
    source["workers"]["test-worker"]["week_minutes"] = 481
    path.write_text(json.dumps(source))
    assert not build(r,"2026-09-13",NOW)["workers"][0]["week_review"]["draft_current"]
    source["workers"]["test-worker"]["days"]["2026-08-01"] = source["workers"]["test-worker"]["days"].pop("2026-09-07")
    path.write_text(json.dumps(source))
    with pytest.raises(ValueError, match="coverage"):
        build(r,"2026-09-13",NOW)
