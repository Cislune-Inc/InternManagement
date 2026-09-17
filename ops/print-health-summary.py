from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.manager_auth import local_headers


def summarize_health(payload: dict[str, Any]) -> dict[str, Any]:
    bot = payload.get("bot") if isinstance(payload.get("bot"), dict) else {}
    backup = payload.get("backup") if isinstance(payload.get("backup"), dict) else {}
    integration_health = (
        payload.get("integration_health")
        if isinstance(payload.get("integration_health"), dict)
        else {}
    )
    checks = integration_health.get("checks")
    failing_checks = [
        str(check.get("name") or "unknown")
        for check in checks or []
        if isinstance(check, dict) and str(check.get("status") or "") != "ok"
    ]
    return {
        "overall_status": payload.get("overall_status"),
        "bot_running": bool(bot.get("running")),
        "bot_pid": bot.get("pid"),
        "dashboard_running": bool(
            isinstance(payload.get("dashboard"), dict)
            and payload["dashboard"].get("running")
        ),
        "backup_healthy": bool(backup.get("healthy")),
        "backup_age": backup.get("age_human"),
        "integrations_status": integration_health.get("overall_status"),
        "failing_checks": failing_checks,
        "open_issue_count": payload.get("open_issue_count"),
        "generated_at": payload.get("generated_at"),
    }


def main() -> int:
    url = "http://127.0.0.1:8765/api/health"
    with urlopen(Request(url, headers=local_headers(url)), timeout=10) as response:
        payload = json.load(response)
    print(json.dumps(summarize_health(payload), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
