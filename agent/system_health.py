from __future__ import annotations

import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def build_system_health_payload(runtime: Any) -> dict[str, Any]:
    storage_root = runtime.bootstrap.storage_root_path
    workspace_root = runtime.bootstrap.state_db_path.resolve().parent.parent
    lock_path = runtime.bootstrap.state_db_path.parent / "agent.lock"
    lock_payload = _read_json(lock_path)
    bot_pid = _to_int(lock_payload.get("pid"))
    bot_running = _pid_exists(bot_pid)
    storage_bytes, storage_files = _directory_size(storage_root)
    database = runtime.state_store.health_snapshot()
    issues = runtime.state_store.list_operational_issues(status=None, limit=250)
    open_issues = [issue for issue in issues if issue.get("status") == "open"]
    severity_counts: dict[str, int] = {}
    for issue in open_issues:
        severity = str(issue.get("severity") or "unknown")
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
    config = runtime.config
    integration_health = _read_json(
        runtime.bootstrap.state_db_path.parent / "integration_health.json"
    )
    backup = _backup_status(workspace_root / "backups")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_status": _overall_status(bot_running, open_issues),
        "bot": {
            "running": bot_running,
            "pid": bot_pid,
            "lock_path": str(lock_path.resolve()),
        },
        "dashboard": {
            "running": True,
            "url": "http://127.0.0.1:8765/",
        },
        "integrations": {
            "discord_configured": bool(os.environ.get("DISCORD_BOT_TOKEN")),
            "slack_configured": bool(runtime.slack),
            "clickup_configured": bool(runtime.clickup),
            "openai_configured": bool(os.environ.get("OPENAI_API_KEY")),
        },
        "database": database,
        "storage": {
            "path": str(storage_root.resolve()),
            "bytes": storage_bytes,
            "human": _human_bytes(storage_bytes),
            "file_count": storage_files,
        },
        "backup": backup,
        "integration_health": integration_health,
        "issues": issues,
        "open_issue_count": len(open_issues),
        "severity_counts": severity_counts,
        "slack_routes": [
            {
                "label": route.label,
                "channel_id": route.channel_id,
                "labor_code": route.labor_code,
            }
            for route in (config.slack.project_routes if config else [])
        ],
        "notification_targets": [
            {
                "name": admin.name,
                "slack_enabled": bool(admin.slack_user_id),
                "discord_enabled": bool(admin.discord_user_id),
            }
            for admin in (config.admins if config else [])
        ],
    }


def render_system_health_html(payload: dict[str, Any]) -> str:
    issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
    open_issues = [issue for issue in issues if issue.get("status") == "open"]
    resolved_issues = [issue for issue in issues if issue.get("status") == "resolved"]
    bot = payload.get("bot") if isinstance(payload.get("bot"), dict) else {}
    database = payload.get("database") if isinstance(payload.get("database"), dict) else {}
    storage = payload.get("storage") if isinstance(payload.get("storage"), dict) else {}
    integrations = payload.get("integrations") if isinstance(payload.get("integrations"), dict) else {}
    integration_health = payload.get("integration_health") if isinstance(payload.get("integration_health"), dict) else {}
    integration_checks = integration_health.get("checks") if isinstance(integration_health.get("checks"), list) else []
    backup = payload.get("backup") if isinstance(payload.get("backup"), dict) else {}
    slack_routes = payload.get("slack_routes") if isinstance(payload.get("slack_routes"), list) else []
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Don Pollo System Health</title>
  <style>
    :root {{ --ink:#172725; --paper:#f3efe4; --card:#fffdf8; --line:#d7d0c3; --teal:#177c74; --warn:#b35a2e; --bad:#9e2f34; --good:#26734d; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--ink); background:radial-gradient(circle at 12% 0,#d7ebe4 0,transparent 34%),var(--paper); font:15px/1.5 "Avenir Next","Gill Sans",sans-serif; }}
    main {{ width:min(1180px,94vw); margin:28px auto 72px; }}
    header,.panel,.metric {{ background:rgba(255,253,248,.92); border:1px solid var(--line); border-radius:20px; }}
    header,.panel {{ padding:22px; margin-bottom:16px; }}
    h1,h2 {{ font-family:"Iowan Old Style","Baskerville",serif; margin:0 0 8px; }}
    nav {{ display:flex; flex-wrap:wrap; gap:9px; margin-bottom:18px; }}
    nav a {{ color:var(--ink); text-decoration:none; border:1px solid var(--line); border-radius:999px; padding:8px 12px; background:white; font-weight:700; }}
    nav a.active {{ color:white; background:var(--teal); border-color:var(--teal); }}
    .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin-bottom:16px; }}
    .metric {{ padding:16px; }}
    .metric strong {{ display:block; font-size:1.55rem; }}
    .good {{ color:var(--good); }} .warning {{ color:var(--warn); }} .critical,.error {{ color:var(--bad); }}
    table {{ width:100%; border-collapse:collapse; }}
    th,td {{ padding:10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
    th {{ font-size:.75rem; text-transform:uppercase; letter-spacing:.08em; }}
    code {{ font-size:.82rem; }}
    button {{ border:0; border-radius:999px; padding:8px 12px; color:white; background:var(--teal); cursor:pointer; font-weight:700; }}
    details {{ margin-top:6px; }}
    .empty {{ color:var(--good); font-weight:700; }}
    #result {{ min-height:1.5em; color:var(--warn); }}
    @media(max-width:700px) {{ main{{width:96vw;margin-top:10px}} .panel{{overflow:auto}} }}
  </style>
</head>
<body><main>
  <header>
    {_nav("health")}
    <h1>System Health</h1>
    <p>Operational status, integration readiness, storage growth, and the actionable issue queue. Repeated failures are grouped and Slack alerts are rate-limited.</p>
  </header>
  <section class="metrics">
    {_metric("Overall", payload.get("overall_status") or "unknown", str(payload.get("overall_status") or ""))}
    {_metric("Discord bot", f"PID {bot.get('pid')}" if bot.get("running") else "Offline", "good" if bot.get("running") else "critical")}
    {_metric("Open issues", payload.get("open_issue_count") or 0, "good" if not open_issues else "warning")}
    {_metric("Storage", storage.get("human") or "0 B", "")}
    {_metric("DB schema", database.get("schema_version") or 0, "")}
    {_metric("Latest backup", backup.get("age_human") or "not found", "good" if backup.get("healthy") else "warning")}
  </section>
  <section class="panel">
    <h2>Integrations</h2>
    <p>{_integration_status(integrations)}</p>
    {_integration_check_table(integration_checks)}
  </section>
  <section class="panel">
    <h2>Open issues</h2>
    <p id="result"></p>
    {_issue_table(open_issues, resolvable=True, slack_routes=slack_routes)}
  </section>
  <section class="panel">
    <h2>Resolved history</h2>
    {_issue_table(resolved_issues[:50], resolvable=False, slack_routes=[])}
  </section>
</main>
<script>
document.querySelectorAll("[data-resolve-issue]").forEach((button) => {{
  button.addEventListener("click", async () => {{
    const response = await fetch("/api/issues/resolve", {{
      method: "POST",
      headers: {{"Content-Type":"application/json"}},
      body: JSON.stringify({{fingerprint:button.dataset.resolveIssue}})
    }});
    const result = await response.json();
    document.getElementById("result").textContent = response.ok ? "Issue marked resolved." : (result.error || "Could not resolve issue.");
    if (response.ok) window.location.reload();
  }});
}});
document.querySelectorAll("[data-resolve-route]").forEach((button) => {{
  button.addEventListener("click", async () => {{
    const fingerprint = button.dataset.resolveRoute;
    const channel = document.querySelector(`[data-route-channel="${{fingerprint}}"]`);
    const resolvedBy = document.querySelector(`[data-route-resolved-by="${{fingerprint}}"]`);
    const response = await fetch("/api/routes/resolve", {{
      method: "POST",
      headers: {{"Content-Type":"application/json"}},
      body: JSON.stringify({{
        fingerprint,
        channel_id:channel.value,
        resolved_by:resolvedBy.value
      }})
    }});
    const result = await response.json();
    document.getElementById("result").textContent = response.ok ? "Slack route saved and issue resolved." : (result.error || "Could not save route.");
    if (response.ok) window.location.reload();
  }});
}});
setTimeout(() => window.location.reload(), 10 * 60 * 1000);
</script>
</body></html>"""


def _nav(active: str) -> str:
    links = [
        ("time", "/time", "Time Tracking"),
        ("work", "/work", "Work Dashboard"),
        ("payroll", "/payroll", "Payroll"),
        ("exceptions", "/exceptions", "Manager Queue"),
        ("health", "/health", "System Health"),
    ]
    return "<nav>" + "".join(
        f'<a class="{"active" if key == active else ""}" href="{href}">{label}</a>'
        for key, href, label in links
    ) + "</nav>"


def _metric(label: str, value: Any, css_class: str) -> str:
    return (
        f'<div class="metric"><span>{html.escape(str(label))}</span>'
        f'<strong class="{html.escape(css_class)}">{html.escape(str(value))}</strong></div>'
    )


def _integration_status(integrations: dict[str, Any]) -> str:
    labels = {
        "discord_configured": "Discord",
        "slack_configured": "Slack",
        "clickup_configured": "ClickUp",
        "openai_configured": "OpenAI",
    }
    return " · ".join(
        f'<span class="{"good" if integrations.get(key) else "critical"}">{label}: {"ready" if integrations.get(key) else "missing"}</span>'
        for key, label in labels.items()
    )


def _integration_check_table(checks: list[dict[str, Any]]) -> str:
    if not checks:
        return "<p>No scheduled smoke-check result has been written yet.</p>"
    rows = "".join(
        "<tr>"
        f'<td>{html.escape(str(check.get("name") or ""))}</td>'
        f'<td class="{"good" if check.get("status") == "ok" else "critical"}">{html.escape(str(check.get("status") or ""))}</td>'
        f'<td>{html.escape(str(check.get("latency_ms") or 0))} ms</td>'
        f'<td>{html.escape(str(check.get("error") or ""))}</td>'
        "</tr>"
        for check in checks
    )
    return (
        "<table><thead><tr><th>Check</th><th>Status</th><th>Latency</th>"
        "<th>Error</th></tr></thead><tbody>"
        + rows
        + "</tbody></table>"
    )


def _issue_table(
    issues: list[dict[str, Any]],
    *,
    resolvable: bool,
    slack_routes: list[dict[str, Any]],
) -> str:
    if not issues:
        return '<p class="empty">No issues in this section.</p>'
    rows: list[str] = []
    for issue in issues:
        details = issue.get("details") if isinstance(issue.get("details"), dict) else {}
        fingerprint = html.escape(str(issue.get("fingerprint") or ""))
        if resolvable and issue.get("category") == "slack_route_uncertain":
            options = "".join(
                f'<option value="{html.escape(str(route.get("channel_id") or ""))}">{html.escape(str(route.get("label") or route.get("channel_id") or ""))}</option>'
                for route in slack_routes
            )
            action = (
                f'<select data-route-channel="{fingerprint}">{options}</select>'
                f'<input data-route-resolved-by="{fingerprint}" placeholder="Resolved by" aria-label="Resolved by">'
                f'<button data-resolve-route="{fingerprint}">Save route</button>'
            )
        elif resolvable:
            action = f'<button data-resolve-issue="{fingerprint}">Resolve</button>'
        else:
            action = html.escape(str(issue.get("resolved_at") or "resolved"))
        rows.append(
            "<tr>"
            f'<td class="{html.escape(str(issue.get("severity") or ""))}">{html.escape(str(issue.get("severity") or ""))}</td>'
            f'<td><strong>{html.escape(str(issue.get("summary") or ""))}</strong>'
            f'<details><summary>Details</summary><pre>{html.escape(json.dumps(details, indent=2, sort_keys=True))}</pre></details></td>'
            f'<td>{int(issue.get("occurrence_count") or 0)}</td>'
            f'<td>{html.escape(str(issue.get("last_seen_at") or ""))}</td>'
            f"<td>{action}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Severity</th><th>Issue</th><th>Count</th>"
        "<th>Last seen</th><th>Action</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _to_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _pid_exists(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _directory_size(root: Path) -> tuple[int, int]:
    total = 0
    count = 0
    if not root.exists():
        return total, count
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        count += 1
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total, count


def _human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{amount:.1f} TB"


def _backup_status(backups_root: Path) -> dict[str, Any]:
    try:
        latest = max(
            backups_root.glob("*.tar.gz.enc"),
            key=lambda path: path.stat().st_mtime,
        )
    except (ValueError, OSError):
        return {"healthy": False, "path": None, "age_human": "not found"}
    created_at = datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)
    age = datetime.now(timezone.utc) - created_at
    hours = max(0, int(age.total_seconds() // 3600))
    return {
        "healthy": hours <= 26,
        "path": str(latest.resolve()),
        "created_at": created_at.isoformat(),
        "age_hours": hours,
        "age_human": f"{hours}h old",
        "bytes": latest.stat().st_size,
    }


def _overall_status(bot_running: bool, issues: list[dict[str, Any]]) -> str:
    if not bot_running:
        return "critical"
    severities = {str(issue.get("severity") or "") for issue in issues}
    if severities & {"critical", "error"}:
        return "error"
    if "warning" in severities:
        return "warning"
    return "good"
