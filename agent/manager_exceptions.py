from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .work_dashboard import build_work_dashboard_payload


_MISSING_TASK_MANAGER_GRACE = timedelta(minutes=60)


async def build_manager_exceptions_payload(
    runtime: Any,
    storage_root: Path,
    *,
    reference_now: datetime | None = None,
) -> dict[str, Any]:
    now = reference_now or datetime.now(timezone.utc)
    work = await build_work_dashboard_payload(
        storage_root,
        runtime=runtime,
        reference_now=now,
    )
    exceptions: list[dict[str, Any]] = []
    for person in work.get("people", []):
        if not isinstance(person, dict):
            continue
        exceptions.extend(_person_exceptions(person, reference_now=now))
    for issue in runtime.state_store.list_operational_issues(status="open", limit=250):
        details = issue.get("details")
        details = details if isinstance(details, dict) else {}
        exceptions.append(
            {
                "id": str(issue.get("fingerprint") or ""),
                "severity": str(issue.get("severity") or "warning"),
                "category": str(issue.get("category") or "operational"),
                "person": str(details.get("display_name") or ""),
                "user_key": str(details.get("user_key") or ""),
                "session_date": str(details.get("session_date") or ""),
                "summary": str(issue.get("summary") or ""),
                "details": details,
                "source": "operational_issue",
                "last_seen_at": str(issue.get("last_seen_at") or ""),
                "occurrence_count": int(issue.get("occurrence_count") or 1),
            }
        )
    exceptions = _deduplicate_exceptions(exceptions)
    severity_order = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    exceptions.sort(
        key=lambda item: (
            severity_order.get(str(item.get("severity")), 9),
            str(item.get("person") or "").lower(),
            str(item.get("category") or ""),
        )
    )
    return {
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "exception_count": len(exceptions),
        "people_count": len(work.get("people", [])),
        "exceptions": exceptions,
    }


def render_manager_exceptions_html(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Don Pollo Manager Queue</title>
  <style>
    :root {{ --ink:#1b2928; --paper:#f4f0e5; --card:#fffdf8; --line:#d8d1c2; --teal:#177c74; --warn:#a3542b; --bad:#992f35; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--ink); background:radial-gradient(circle at 15% 0,#d8ebe4 0,transparent 35%),var(--paper); font:15px/1.5 "Avenir Next","Gill Sans",sans-serif; }}
    main {{ width:min(1180px,94vw); margin:28px auto 70px; }}
    header,.panel,.card {{ background:rgba(255,253,248,.94); border:1px solid var(--line); border-radius:20px; }}
    header,.panel {{ padding:22px; margin-bottom:16px; }}
    h1 {{ font-family:"Iowan Old Style","Baskerville",serif; margin:0 0 8px; }}
    nav {{ display:flex; flex-wrap:wrap; gap:9px; margin-bottom:18px; }}
    nav a,.action {{ color:var(--ink); text-decoration:none; border:1px solid var(--line); border-radius:999px; padding:8px 12px; background:white; font-weight:700; }}
    nav a.active {{ color:white; background:var(--teal); border-color:var(--teal); }}
    .controls {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:16px; }}
    select,input {{ padding:10px; border:1px solid var(--line); border-radius:10px; background:white; }}
    .grid {{ display:grid; gap:12px; }}
    .card {{ padding:17px; border-left:6px solid var(--warn); }}
    .card.critical,.card.error {{ border-left-color:var(--bad); }}
    .meta {{ color:#667270; font-size:.88rem; }}
    .empty {{ color:#26734d; font-weight:700; }}
    pre {{ white-space:pre-wrap; overflow-wrap:anywhere; }}
  </style>
</head>
<body><main>
  <header>
    {_nav()}
    <h1>Manager Exception Queue</h1>
    <p>Only items that may need attention: incomplete close-outs, blockers, review queues, time/task mismatches, integration failures, and uncertain Slack routing.</p>
  </header>
  <section class="panel">
    <div class="controls">
      <select id="severity"><option value="">All severities</option><option>critical</option><option>error</option><option>warning</option><option>info</option></select>
      <select id="category"><option value="">All categories</option></select>
      <input id="search" placeholder="Search person or issue">
    </div>
    <div id="queue" class="grid"></div>
  </section>
</main>
<script id="queue-data" type="application/json">{data}</script>
<script>
const payload = JSON.parse(document.getElementById('queue-data').textContent || '{{}}');
const rows = Array.isArray(payload.exceptions) ? payload.exceptions : [];
const severity = document.getElementById('severity');
const category = document.getElementById('category');
const search = document.getElementById('search');
const queue = document.getElementById('queue');
category.innerHTML += [...new Set(rows.map(row => row.category).filter(Boolean))].sort().map(value => `<option>${{escapeHtml(value)}}</option>`).join('');
function render() {{
  const query = search.value.trim().toLowerCase();
  const visible = rows.filter(row => (!severity.value || row.severity === severity.value) && (!category.value || row.category === category.value) && (!query || JSON.stringify(row).toLowerCase().includes(query)));
  queue.innerHTML = visible.length ? visible.map(row => `<article class="card ${{escapeHtml(row.severity)}}"><strong>${{escapeHtml(row.summary)}}</strong><div class="meta">${{escapeHtml(row.severity)}} · ${{escapeHtml(row.category)}}${{row.person ? ` · ${{escapeHtml(row.person)}}` : ''}}${{row.session_date ? ` · ${{escapeHtml(row.session_date)}}` : ''}}</div>${{row.details?.recommended_action ? `<p>${{escapeHtml(row.details.recommended_action)}}</p>` : ''}}<details><summary>Evidence and history</summary><pre>${{escapeHtml(JSON.stringify(row.details || {{}}, null, 2))}}</pre></details><p>${{row.user_key ? `<a class="action" href="/work?worker=${{encodeURIComponent(row.user_key)}}${{row.session_date ? `&session=${{encodeURIComponent(row.session_date)}}` : ''}}">Assign or correct ClickUp task</a>` : ''}}${{row.source === 'operational_issue' ? ` <button class="action" type="button" data-dismiss-fingerprint="${{escapeHtml(row.id)}}">Dismiss duplicate / obsolete</button>` : ''}}</p></article>`).join('') : '<p class="empty">No manager exceptions are currently open.</p>';
}}
function escapeHtml(value) {{ return String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'","&#39;"); }}
[severity, category, search].forEach(control => control.addEventListener('input', render));
queue.addEventListener('click', async event => {{
  const button = event.target.closest('[data-dismiss-fingerprint]');
  if (!button) return;
  if (!window.confirm('Dismiss this only if it is a duplicate or the underlying issue is already obsolete.')) return;
  button.disabled = true;
  try {{
    const response = await fetch('/api/issues/resolve', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{fingerprint: button.dataset.dismissFingerprint}}),
    }});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Dismissal failed.');
    window.location.reload();
  }} catch (error) {{
    button.disabled = false;
    window.alert(error.message);
  }}
}});
render();
setTimeout(() => window.location.reload(), 10 * 60 * 1000);
</script>
</body></html>"""


def _person_exceptions(
    person: dict[str, Any],
    *,
    reference_now: datetime | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    base = {
        "person": str(person.get("display_name") or ""),
        "user_key": str(person.get("user_key") or ""),
        "session_date": str(person.get("current_session_date") or ""),
        "source": "session",
        "last_seen_at": str(person.get("last_user_message_at") or ""),
        "occurrence_count": 1,
    }
    stage = str(person.get("current_stage") or "")
    if stage == "awaiting_clock_out_artifacts":
        rows.append(
            _exception(
                base,
                "warning",
                "incomplete_clock_out",
                "Clock-out is waiting for required artifacts.",
                {"stage": stage},
            )
        )
    blocker = str(person.get("latest_blocker") or "").strip()
    if blocker:
        rows.append(
            _exception(
                base,
                "warning",
                "active_blocker",
                "Worker has an unresolved blocker.",
                {"blocker": blocker},
            )
        )
    pending_reviews = int(person.get("pending_admin_review_count") or 0)
    if pending_reviews:
        rows.append(
            _exception(
                base,
                "info",
                "admin_review",
                f"{pending_reviews} task review(s) await admin action.",
                {"pending_review_count": pending_reviews},
            )
        )
    quality_warning = person.get("portal_quality_warning")
    if isinstance(quality_warning, dict) and quality_warning.get("deadline_at"):
        rows.append(
            _exception(
                base,
                "warning",
                "quality_correction_due",
                "Worker has a 10-minute work-detail correction deadline.",
                {
                    "deadline_at": quality_warning.get("deadline_at"),
                    "context": quality_warning.get("context"),
                    "reasons": quality_warning.get("reasons") or [],
                    "action": "Worker can clear this by submitting concrete detail before the deadline.",
                },
            )
        )
    quality_block = person.get("portal_quality_restart_blocked")
    if isinstance(quality_block, dict) and quality_block:
        rows.append(
            _exception(
                base,
                "error",
                "quality_restart_approval",
                "Worker was clocked out after an uncorrected quality warning and needs manager review.",
                {
                    **quality_block,
                    "approval_command": (
                        f"run review.quality_restart user={base['user_key']} "
                        'comments="Reviewed corrected plan and approved scope."'
                    ),
                },
            )
        )
    active_task_id = str(person.get("active_task_id") or "")
    timer_task_id = str(person.get("active_timer_task_id") or "")
    if active_task_id and timer_task_id and active_task_id != timer_task_id:
        rows.append(
            _exception(
                base,
                "error",
                "task_timer_mismatch",
                "Active ClickUp task and running timer disagree.",
                {
                    "active_task_id": active_task_id,
                    "active_task_name": person.get("active_task_name"),
                    "timer_task_id": timer_task_id,
                    "timer_task_name": person.get("active_timer_task_name"),
                },
            )
        )
    if person.get("clock_state") == "clocked in" and not active_task_id:
        wait_minutes = _minutes_since_last_worker_message(
            person,
            reference_now or datetime.now(timezone.utc),
        )
        if wait_minutes is None or wait_minutes >= int(
            _MISSING_TASK_MANAGER_GRACE.total_seconds() // 60
        ):
            rows.append(
                _exception(
                    base,
                    "warning",
                    "missing_active_task",
                    "Clocked-in worker still has no confirmed ClickUp task after the worker-only grace period.",
                    {
                        "stage": stage,
                        "waiting_minutes": wait_minutes,
                        "manager_grace_minutes": int(
                            _MISSING_TASK_MANAGER_GRACE.total_seconds() // 60
                        ),
                        "recommended_action": (
                            "Assign an existing task, classify the work as overhead, or ask the worker "
                            "which concrete result should be recorded."
                        ),
                    },
                )
            )
    current_days = person.get("days")
    current_day = current_days[0] if isinstance(current_days, list) and current_days else {}
    current_seconds = int(
        current_day.get("clocked_in_total_seconds") or 0
        if isinstance(current_day, dict)
        else 0
    )
    if current_seconds > 10 * 60 * 60:
        rows.append(
            _exception(
                base,
                "warning",
                "long_shift",
                "Recorded shift exceeds 10 hours and should be reviewed.",
                {"clocked_in_seconds": current_seconds},
            )
        )
    return rows


def _deduplicate_exceptions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduplicated: list[dict[str, Any]] = []
    missing_task_rows: dict[str, dict[str, Any]] = {}
    for row in rows:
        category = str(row.get("category") or "")
        user_key = str(row.get("user_key") or "")
        if category not in {"missing_active_task", "slack_update_missing_task"} or not user_key:
            deduplicated.append(row)
            continue
        target = missing_task_rows.get(user_key)
        if target is None:
            missing_task_rows[user_key] = row
            deduplicated.append(row)
            continue
        target_history_item = _missing_task_history_item(target)
        row_history_item = _missing_task_history_item(row)
        sources: list[str] = []
        for value in (target.get("source"), row.get("source")):
            for part in str(value or "").split("+"):
                if part and part not in sources:
                    sources.append(part)
        target["source"] = "+".join(sources)
        target["last_seen_at"] = max(
            str(target.get("last_seen_at") or ""),
            str(row.get("last_seen_at") or ""),
        )
        target["occurrence_count"] = int(target.get("occurrence_count") or 1) + int(
            row.get("occurrence_count") or 1
        )
        details = target.get("details")
        details = dict(details) if isinstance(details, dict) else {}
        history = details.get("history")
        history = list(history) if isinstance(history, list) else []
        if not history:
            history.append(target_history_item)
        history.append(row_history_item)
        unique_history: dict[tuple[str, str, str], dict[str, Any]] = {}
        for item in history:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("session_date") or ""),
                str(item.get("source") or ""),
                str(item.get("summary") or ""),
            )
            unique_history[key] = item
        details["history"] = sorted(
            unique_history.values(),
            key=lambda item: (
                str(item.get("session_date") or ""),
                str(item.get("last_seen_at") or ""),
            ),
            reverse=True,
        )
        row_details = row.get("details")
        if category == "slack_update_missing_task" and isinstance(row_details, dict):
            details["worker_prompted"] = bool(
                details.get("worker_prompted") or row_details.get("worker_prompted")
            )
        target["session_date"] = max(
            str(target.get("session_date") or ""),
            str(row.get("session_date") or ""),
        )
        target["details"] = details
    return deduplicated


def _minutes_since_last_worker_message(
    person: dict[str, Any],
    reference_now: datetime,
) -> int | None:
    raw = str(person.get("last_user_message_at") or "").strip()
    if not raw:
        return None
    try:
        last_message = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if last_message.tzinfo is None:
        last_message = last_message.replace(tzinfo=timezone.utc)
    if reference_now.tzinfo is None:
        reference_now = reference_now.replace(tzinfo=timezone.utc)
    return max(0, int((reference_now - last_message).total_seconds() // 60))


def _missing_task_history_item(row: dict[str, Any]) -> dict[str, Any]:
    details = row.get("details")
    details = details if isinstance(details, dict) else {}
    return {
        "session_date": str(row.get("session_date") or ""),
        "source": str(row.get("source") or ""),
        "summary": str(row.get("summary") or ""),
        "stage": str(details.get("stage") or ""),
        "worker_prompted": bool(details.get("worker_prompted")),
        "last_seen_at": str(row.get("last_seen_at") or ""),
        "occurrence_count": int(row.get("occurrence_count") or 1),
    }


def _exception(
    base: dict[str, Any],
    severity: str,
    category: str,
    summary: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        **base,
        "id": f"{category}:{base['user_key']}:{base['session_date']}",
        "severity": severity,
        "category": category,
        "summary": summary,
        "details": details,
    }


def _nav() -> str:
    links = [
        ("/time", "Time Tracking"),
        ("/work", "Work Dashboard"),
        ("/payroll", "Payroll"),
        ("/exceptions", "Manager Queue"),
        ("/health", "System Health"),
    ]
    return "<nav>" + "".join(
        f'<a class="{"active" if href == "/exceptions" else ""}" href="{href}">{html.escape(label)}</a>'
        for href, label in links
    ) + "</nav>"
