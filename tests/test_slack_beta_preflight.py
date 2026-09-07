import json
import sqlite3

from ops.slack_beta_preflight import inspect


def inputs(tmp_path):
    config = tmp_path / "agent.config.json"
    config.write_text(json.dumps({"admin_discord_user_id": 1, "admin_slack_user_id": "ERIK", "roster_file_name": "roster.json", "clickup": {"workspace_id": "unused"}}))
    (tmp_path / "roster.json").write_text(json.dumps([{"user_key": "worker", "slack_user_id": "WORKER", "compensation_plan": "needs_review"}]))
    db = tmp_path / "state.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE sessions(user_key TEXT, session_date TEXT, payload TEXT)")
    return config, db, {"SLACK_BOT_TOKEN": "secret-bot", "SLACK_APP_TOKEN": "secret-app", "OPENAI_API_KEY": "secret-ai"}


def test_preflight_redacts_credentials_and_never_changes_config_or_database(tmp_path):
    config, db, credentials = inputs(tmp_path)
    before = (config.read_bytes(), db.read_bytes())
    report = inspect(config, db, credentials)
    assert report["blocks"] == []
    assert report["live_verification_required"] is True
    assert report["warnings"]
    assert "secret-" not in json.dumps(report)
    assert (config.read_bytes(), db.read_bytes()) == before


def test_missing_database_is_not_created_and_missing_slack_blocks(tmp_path):
    config, _, _ = inputs(tmp_path)
    missing = tmp_path / "missing.sqlite3"
    report = inspect(config, missing, {})
    assert not missing.exists()
    assert any("database" in message for message in report["blocks"])
    assert any("SLACK_BOT_TOKEN" in message for message in report["blocks"])
    assert any("OpenAI key missing" in message for message in report["warnings"])


def test_duplicate_open_shifts_require_reconciliation_without_modifying_them(tmp_path):
    config, db, credentials = inputs(tmp_path)
    with sqlite3.connect(db) as conn:
        for day in (6, 7):
            conn.execute("INSERT INTO sessions VALUES (?,?,?)", ("worker", f"2026-09-{day:02d}", json.dumps({"clocked_in_at": f"2026-09-{day:02d}T09:00:00-07:00"})))
    report = inspect(config, db, credentials)
    assert report["checks"]["open_shift_records"] == 2
    assert any("multiple open shifts" in message for message in report["blocks"])
