from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from agent.integration_health import _check_production_checkout, run_integration_checks
from agent.models import SlackConfig
from agent.operations import OperationalIssueReporter, issue_fingerprint
from agent.state_store import StateStore


class _Response:
    status_code = 200

    def __init__(self, payload=None):
        self.payload = payload or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _runtime(tmp_path):
    state = StateStore(tmp_path / "data" / "agent_state.sqlite3")
    lock = tmp_path / "data" / "agent.lock"
    lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    slack = SimpleNamespace(post_message=None)
    config = SimpleNamespace(
        slack=SlackConfig(
            enabled=True,
            operational_alerts_enabled=False,
        )
    )
    runtime = SimpleNamespace(
        state_store=state,
        bootstrap=SimpleNamespace(state_db_path=tmp_path / "data" / "agent_state.sqlite3"),
        slack=slack,
        clickup=object(),
        config=config,
    )
    runtime.operations = OperationalIssueReporter(
        state_store=state,
        config_provider=lambda: config,
        slack_provider=lambda: None,
        admins_provider=lambda: [],
        timezone_provider=lambda: "America/Los_Angeles",
    )
    return runtime


def test_integration_checks_record_success_and_resolve_prior_failure(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "current.tar.gz.enc").write_bytes(b"encrypted")
    now = datetime.now(timezone.utc)
    runtime.state_store.record_operational_issue(
        fingerprint=issue_fingerprint("integration_health", "dashboard"),
        category="integration_health",
        severity="critical",
        summary="old dashboard failure",
    )
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord")
    monkeypatch.setenv("OPENAI_API_KEY", "openai")

    def request(method, url, **kwargs):
        return _Response({"ok": True} if "slack.com" in url else {})

    result = asyncio.run(
        run_integration_checks(
            runtime,
            backups_path=backups,
            now=now,
            request=request,
        )
    )

    assert result["overall_status"] == "ok"
    assert {item["name"] for item in result["checks"]} == {
        "database",
        "bot",
        "backup",
        "production_checkout",
        "dashboard",
        "discord",
        "slack",
        "clickup",
        "openai",
    }
    assert runtime.state_store.list_operational_issues(status="open") == []


def test_failed_check_enters_issue_queue(tmp_path):
    runtime = _runtime(tmp_path)

    def request(method, url, **kwargs):
        if "127.0.0.1" in url:
            raise RuntimeError("dashboard offline")
        return _Response({"ok": True})

    result = asyncio.run(
        run_integration_checks(
            runtime,
            backups_path=tmp_path / "missing",
            request=request,
        )
    )

    assert result["overall_status"] == "error"
    issues = runtime.state_store.list_operational_issues(status="open")
    summaries = {issue["summary"] for issue in issues}
    assert "Dashboard health check failed." in summaries
    assert "Backup health check failed." in summaries


def test_production_checkout_check_accepts_clean_main(tmp_path, monkeypatch):
    outputs = iter(["main\n", "", "abc123\n"])

    def run(*args, **kwargs):
        return SimpleNamespace(stdout=next(outputs))

    monkeypatch.setattr("agent.integration_health.subprocess.run", run)

    result = _check_production_checkout(tmp_path)

    assert result["status"] == "ok"
    assert result["details"] == {"branch": "main", "revision": "abc123"}


def test_production_checkout_check_rejects_feature_branch(tmp_path, monkeypatch):
    def run(*args, **kwargs):
        return SimpleNamespace(stdout="agent/repair-tony-task-tracking\n")

    monkeypatch.setattr("agent.integration_health.subprocess.run", run)

    result = _check_production_checkout(tmp_path)

    assert result["status"] == "error"
    assert "not main" in result["error"]


def test_controlled_deploy_suppresses_transient_bot_and_dashboard_failures(tmp_path):
    runtime = _runtime(tmp_path)
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "current.tar.gz.enc").write_bytes(b"encrypted")
    now = datetime.now(timezone.utc)
    marker = tmp_path / "data" / "deploy_maintenance.json"
    marker.write_text(
        json.dumps(
            {
                "reason": "controlled deployment",
                "started_at": now.isoformat(),
                "expires_at": now.replace(year=now.year + 1).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "data" / "agent.lock").write_text(
        json.dumps({"pid": 99999999}), encoding="utf-8"
    )

    def request(method, url, **kwargs):
        if "127.0.0.1" in url:
            raise RuntimeError("dashboard restarting")
        return _Response({"ok": True})

    result = asyncio.run(
        run_integration_checks(
            runtime,
            backups_path=backups,
            now=now,
            request=request,
        )
    )

    statuses = {item["name"]: item["status"] for item in result["checks"]}
    assert result["overall_status"] == "ok"
    assert statuses["bot"] == "maintenance"
    assert statuses["dashboard"] == "maintenance"
    assert runtime.state_store.list_operational_issues(status="open") == []
