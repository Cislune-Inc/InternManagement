from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import MessageRecord, SessionState, UserProfile
from .time_utils import localize_datetime

_RECENT_WORK_SECONDS = 12 * 60 * 60
_WORK_DASHBOARD_AUTO_REFRESH_INTERVAL_MS = 10 * 60 * 1000

logger = logging.getLogger(__name__)


async def build_work_dashboard_payload(
    storage_root: Path,
    *,
    runtime: Any,
    reference_now: datetime | None = None,
) -> dict[str, Any]:
    now = reference_now or datetime.now(timezone.utc)
    users = sorted(runtime.roster_by_key.values(), key=lambda user: user.display_name.lower())
    people: list[dict[str, Any]] = []
    for user in users:
        people.append(await _build_person_payload(storage_root, runtime, user, now))
    generated_at = now.astimezone(timezone.utc).isoformat()
    return {
        "generated_at": generated_at,
        "people": people,
        "recent_work_seconds_target": _RECENT_WORK_SECONDS,
        "auto_refresh_interval_ms": _WORK_DASHBOARD_AUTO_REFRESH_INTERVAL_MS,
        "time_dashboard_url": "/time",
        "work_dashboard_url": "/work",
    }


def render_work_dashboard_html(payload: dict[str, Any]) -> str:
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":")).replace("</", "<\\/")
    return _dashboard_template().replace("__PAYLOAD_JSON__", payload_json)


async def _build_person_payload(
    storage_root: Path,
    runtime: Any,
    user: UserProfile,
    now: datetime,
) -> dict[str, Any]:
    timezone_name = runtime.resolve_user_timezone_name(user)
    current_session, _local_now = runtime.get_user_session_for_moment(user, now)
    sessions = _load_archived_sessions(storage_root, user)
    sessions[current_session.session_date] = current_session
    day_rows: list[dict[str, Any]] = []
    accumulated_seconds = 0
    warnings: list[str] = []
    for session_date in sorted(sessions.keys(), reverse=True):
        session = sessions[session_date]
        runtime._normalize_session_state(session, user=user)
        summary_now = runtime._session_time_summary_reference_now(session, user, now)
        runtime._refresh_session_time_summary(session, summary_now)
        messages = _safe_list_messages(runtime, user.user_key, session.session_date)
        day = _summarize_session_day(runtime, user, session, messages, now)
        day_rows.append(day)
        accumulated_seconds += int(day.get("clocked_in_total_seconds") or 0)
        if accumulated_seconds >= _RECENT_WORK_SECONDS:
            break

    current_summary_now = runtime._session_time_summary_reference_now(current_session, user, now)
    runtime._refresh_session_time_summary(current_session, current_summary_now)
    available_tasks, task_warning = await _load_available_clickup_tasks(runtime, user)
    if task_warning:
        warnings.append(task_warning)
    return {
        "user_key": user.user_key,
        "display_name": user.display_name,
        "timezone": timezone_name,
        "current_session_date": current_session.session_date,
        "current_stage": current_session.stage,
        "clock_state": _clock_state(current_session),
        "active_task_id": str(runtime._active_task_id(current_session) or ""),
        "active_task_name": str(current_session.metadata.get("active_clickup_task_name") or ""),
        "active_timer_task_id": _active_timer_task_id(current_session),
        "active_timer_task_name": _active_timer_task_name(current_session),
        "pending_admin_review_count": len(runtime._pending_admin_reviews(current_session)),
        "latest_plan": current_session.latest_plan or "",
        "latest_status": current_session.latest_status or "",
        "latest_blocker": current_session.latest_blocker or "",
        "last_user_message_at": current_session.last_user_message_at or "",
        "last_outbound_at": current_session.last_outbound_at or "",
        "net_clocked_in_seconds_recent": sum(int(day.get("clocked_in_total_seconds") or 0) for day in day_rows),
        "task_tracked_seconds_recent": sum(int(day.get("task_tracked_total_seconds") or 0) for day in day_rows),
        "unpaid_lunch_deducted_seconds_recent": sum(int(day.get("unpaid_lunch_deducted_seconds") or 0) for day in day_rows),
        "days": day_rows,
        "available_tasks": available_tasks,
        "warnings": warnings,
    }


def _load_archived_sessions(storage_root: Path, user: UserProfile) -> dict[str, SessionState]:
    sessions: dict[str, SessionState] = {}
    user_dir = storage_root / "people" / (user.storage_folder_name or user.user_key)
    if not user_dir.exists():
        return sessions
    for session_path in sorted(user_dir.glob("*/session.json")):
        session_date = session_path.parent.name
        try:
            session = SessionState(**json.loads(session_path.read_text(encoding="utf-8")))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            logger.warning("Could not load archived session for work dashboard: %s", session_path)
            continue
        sessions[session_date] = session
    return sessions


def _summarize_session_day(
    runtime: Any,
    user: UserProfile,
    session: SessionState,
    messages: list[MessageRecord],
    now: datetime,
) -> dict[str, Any]:
    timezone_name = runtime.resolve_user_timezone_name(user)
    time_by_task = session.time_summary.get("time_by_task")
    if not isinstance(time_by_task, list):
        time_by_task = []
    return {
        "session_date": session.session_date,
        "stage": session.stage,
        "clock_state": _clock_state(session),
        "active_task_id": str(runtime._active_task_id(session) or ""),
        "active_task_name": str(session.metadata.get("active_clickup_task_name") or ""),
        "latest_plan": session.latest_plan or "",
        "latest_status": session.latest_status or "",
        "latest_blocker": session.latest_blocker or "",
        "clocked_in_total_seconds": int(session.time_summary.get("clocked_in_total_seconds") or 0),
        "clocked_in_total_human": str(session.time_summary.get("clocked_in_total_human") or "0m"),
        "gross_clocked_in_total_seconds": int(session.time_summary.get("gross_clocked_in_total_seconds") or 0),
        "gross_clocked_in_total_human": str(session.time_summary.get("gross_clocked_in_total_human") or "0m"),
        "unpaid_lunch_deducted_seconds": int(session.time_summary.get("unpaid_lunch_deducted_seconds") or 0),
        "unpaid_lunch_deducted_human": str(session.time_summary.get("unpaid_lunch_deducted_human") or "0m"),
        "task_tracked_total_seconds": int(session.time_summary.get("task_tracked_total_seconds") or 0),
        "task_tracked_total_human": str(session.time_summary.get("task_tracked_total_human") or "0m"),
        "time_by_task": time_by_task,
        "recent_messages": _recent_message_payload(messages, timezone_name, now),
    }


def _recent_message_payload(
    messages: list[MessageRecord],
    timezone_name: str,
    now: datetime,
) -> list[dict[str, Any]]:
    recent: list[dict[str, Any]] = []
    for message in reversed(messages):
        if message.direction != "inbound":
            continue
        content = (message.content or "").strip()
        if not content and not message.attachments:
            continue
        created_at = message.created_at
        if created_at.tzinfo is None:
            created_at = localize_datetime(created_at, timezone_name)
        else:
            created_at = localize_datetime(created_at, timezone_name)
        recent.append(
            {
                "created_at": created_at.isoformat(),
                "content": content[:500],
                "attachment_count": len(message.attachments),
            }
        )
        if len(recent) >= 5:
            break
    return list(reversed(recent))


def _safe_list_messages(runtime: Any, user_key: str, session_date: str) -> list[MessageRecord]:
    try:
        return runtime.state_store.list_messages(user_key, session_date)
    except Exception:
        logger.exception("Could not load messages for %s/%s.", user_key, session_date)
        return []


async def _load_available_clickup_tasks(runtime: Any, user: UserProfile) -> tuple[list[dict[str, Any]], str]:
    if not runtime.clickup:
        return [], "ClickUp is not configured, so available tasks cannot be loaded."
    try:
        tasks = await runtime.clickup.list_assigned_tasks(user, limit=35)
    except Exception as exc:
        logger.exception("Could not load ClickUp tasks for %s.", user.user_key)
        return [], f"ClickUp tasks could not be loaded: {exc}"
    available: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = task.get("status") if isinstance(task.get("status"), dict) else {}
        priority = task.get("priority") if isinstance(task.get("priority"), dict) else {}
        list_payload = task.get("list") if isinstance(task.get("list"), dict) else {}
        available.append(
            {
                "id": str(task.get("id") or ""),
                "name": str(task.get("name") or "Untitled task"),
                "status": str(status.get("status") or ""),
                "priority": str(priority.get("priority") or ""),
                "list_name": str(list_payload.get("name") or ""),
                "url": str(task.get("url") or ""),
                "parent": str(task.get("parent") or ""),
            }
        )
    return available, ""


def _clock_state(session: SessionState) -> str:
    if session.clocked_out_at:
        return "clocked out"
    if session.clocked_in_at:
        return "clocked in"
    return "not clocked in"


def _active_timer_task_id(session: SessionState) -> str:
    tracking = session.metadata.get("clickup_time_tracking")
    if not isinstance(tracking, dict) or tracking.get("closed_at"):
        return ""
    return str(tracking.get("task_id") or "")


def _active_timer_task_name(session: SessionState) -> str:
    tracking = session.metadata.get("clickup_time_tracking")
    if not isinstance(tracking, dict) or tracking.get("closed_at"):
        return ""
    return str(tracking.get("task_name") or "")


def _dashboard_template() -> str:
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Work Dashboard</title>
  <style>
    :root {
      --ink: #243032;
      --muted: #657376;
      --paper: #fffaf0;
      --panel: rgba(255, 255, 255, 0.86);
      --line: rgba(36, 48, 50, 0.14);
      --teal: #21867f;
      --teal-dark: #12625e;
      --amber: #f2b84b;
      --warn: #9f5225;
      --shadow: 0 24px 70px rgba(31, 48, 43, 0.14);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at 18% 8%, rgba(242, 184, 75, 0.22), transparent 32rem),
        radial-gradient(circle at 85% 16%, rgba(33, 134, 127, 0.22), transparent 30rem),
        linear-gradient(135deg, #f5efe3 0%, #edf5ef 100%);
    }
    .shell { width: min(1180px, calc(100vw - 32px)); margin: 22px auto 44px; }
    .hero, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 22px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }
    .hero { padding: 28px; margin-bottom: 20px; }
    h1 { margin: 0 0 8px; font-size: clamp(2.1rem, 5vw, 4.2rem); line-height: 0.95; letter-spacing: -0.06em; }
    h2 { margin: 0; font-size: 1.15rem; }
    p { color: var(--muted); line-height: 1.55; }
    .nav-row, .meta-row, .controls, .chip-list { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
    .nav-link, .pill, button, select, input {
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(255,255,255,0.72);
      color: var(--ink);
      padding: 10px 14px;
      text-decoration: none;
      font: inherit;
    }
    .nav-link.primary, button.primary { background: var(--teal); color: white; border-color: transparent; }
    .panel { padding: 20px; margin: 18px 0; }
    .controls { justify-content: space-between; }
    .control-group { display: grid; gap: 6px; }
    label, .label { color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; font-size: 0.75rem; }
    .summary-strip { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-top: 16px; }
    .metric-card, .person-card, .day-card, .task-card {
      border: 1px solid var(--line);
      border-radius: 18px;
      background: rgba(255,255,255,0.72);
    }
    .metric-card { padding: 16px; }
    .metric-value { font-size: 1.65rem; font-weight: 800; letter-spacing: -0.04em; }
    .person-grid { display: grid; gap: 16px; }
    .person-card { padding: 0; overflow: hidden; }
    summary { cursor: pointer; }
    .person-summary {
      display: grid;
      grid-template-columns: minmax(180px, 0.85fr) minmax(280px, 1.7fr) minmax(150px, 0.55fr);
      gap: 14px;
      align-items: start;
      padding: 18px;
    }
    .person-title strong { font-size: 1.08rem; }
    .summary-bullets {
      margin: 0;
      padding-left: 1.1rem;
      display: grid;
      gap: 7px;
      line-height: 1.38;
    }
    .summary-bullets strong { color: var(--teal-dark); }
    .summary-meta {
      display: grid;
      gap: 7px;
      justify-items: end;
      text-align: right;
    }
    .detail-hint {
      color: var(--muted);
      font-size: 0.86rem;
    }
    .person-body { border-top: 1px solid var(--line); padding: 18px; display: grid; gap: 16px; }
    .key-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
    .key-grid > div, .message, .task-card, .day-card { padding: 12px; }
    .muted { color: var(--muted); }
    .warning { color: var(--warn); font-weight: 700; }
    .chip { display: inline-flex; gap: 6px; align-items: center; border-radius: 999px; padding: 7px 10px; background: rgba(33,134,127,0.10); color: var(--teal-dark); }
    .day-grid, .task-grid { display: grid; gap: 10px; }
    .day-card summary { font-weight: 700; }
    .message { border-left: 3px solid var(--teal); background: rgba(33,134,127,0.07); border-radius: 12px; }
    .empty { padding: 18px; color: var(--muted); border: 1px dashed var(--line); border-radius: 18px; }
    a { color: var(--teal-dark); }
    @media (max-width: 860px) {
      .summary-strip, .person-summary, .key-grid { grid-template-columns: 1fr; }
      .shell { width: min(100vw - 18px, 100%); margin: 12px auto 24px; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="nav-row">
        <a class="nav-link" href="/time">Time Tracking</a>
        <a class="nav-link primary" href="/work">Work Dashboard</a>
        <a class="nav-link" href="/payroll">Payroll</a>
        <a class="nav-link" href="/exceptions">Manager Queue</a>
        <a class="nav-link" href="/health">System Health</a>
      </div>
      <h1>Work Dashboard</h1>
      <p>See what each intern has worked on recently, their current active task and status, and the ClickUp tasks currently available for them to choose next.</p>
      <div class="meta-row">
        <span class="pill">Last 12 net working hours per intern</span>
        <span class="pill">Live data from local archive + ClickUp</span>
        <span class="pill" id="generated-at-pill">Generated from local storage</span>
        <span class="pill" id="refresh-pill">Auto-refresh ready</span>
      </div>
    </section>
    <section class="panel">
      <div class="controls">
        <div class="control-group">
          <label for="intern-filter">Intern</label>
          <select id="intern-filter"></select>
        </div>
        <div class="control-group">
          <label for="task-search">Task / status search</label>
          <input id="task-search" type="search" placeholder="firmware, blocked, planning...">
        </div>
        <button type="button" id="reset-button">Reset filters</button>
      </div>
      <div class="summary-strip" id="summary-strip"></div>
    </section>
    <section class="panel">
      <div class="person-grid" id="person-grid"></div>
    </section>
  </main>
  <script id="work-dashboard-data" type="application/json">__PAYLOAD_JSON__</script>
  <script>
    let payload = JSON.parse(document.getElementById('work-dashboard-data').textContent || '{}');
    let people = Array.isArray(payload.people) ? payload.people : [];
    let refreshTimer = null;
    let refreshInFlight = false;
    const state = { intern: '', query: '' };
    const internFilter = document.getElementById('intern-filter');
    const taskSearch = document.getElementById('task-search');
    const resetButton = document.getElementById('reset-button');
    const summaryStrip = document.getElementById('summary-strip');
    const personGrid = document.getElementById('person-grid');
    const generatedAtPill = document.getElementById('generated-at-pill');
    const refreshPill = document.getElementById('refresh-pill');

    function hydrate(nextPayload, preserveFilters) {
      payload = nextPayload || {};
      people = Array.isArray(payload.people) ? payload.people : [];
      if (!preserveFilters) {
        state.intern = '';
        state.query = '';
      }
      populateControls();
      render();
      const generated = payload.generated_at ? new Date(payload.generated_at) : null;
      generatedAtPill.textContent = generated ? `Generated ${generated.toLocaleString()}` : 'Generated from local storage';
      refreshPill.textContent = payload.auto_refresh_interval_ms ? `Refreshes every ${Math.round(Number(payload.auto_refresh_interval_ms) / 60000)} minutes` : 'Static';
    }

    function populateControls() {
      internFilter.innerHTML = '<option value="">All interns</option>' + people.map((person) => `<option value="${escapeHtml(person.user_key)}">${escapeHtml(person.display_name)}</option>`).join('');
      internFilter.value = state.intern;
      taskSearch.value = state.query;
    }

    function filteredPeople() {
      const q = state.query.trim().toLowerCase();
      return people.filter((person) => {
        if (state.intern && person.user_key !== state.intern) return false;
        if (!q) return true;
        const blob = [
          person.display_name, person.current_stage, person.clock_state, person.active_task_name,
          person.latest_plan, person.latest_status, person.latest_blocker,
          ...(person.days || []).flatMap((day) => [day.active_task_name, day.latest_plan, day.latest_status, day.latest_blocker]),
          ...(person.available_tasks || []).flatMap((task) => [task.name, task.id, task.status, task.list_name]),
        ].join(' ').toLowerCase();
        return blob.includes(q);
      });
    }

    function render() {
      const visible = filteredPeople();
      const totals = visible.reduce((acc, person) => {
        acc.hours += Number(person.net_clocked_in_seconds_recent || 0);
        acc.task += Number(person.task_tracked_seconds_recent || 0);
        acc.tasks += Array.isArray(person.available_tasks) ? person.available_tasks.length : 0;
        return acc;
      }, { hours: 0, task: 0, tasks: 0 });
      summaryStrip.innerHTML = [
        metric('Visible interns', visible.length),
        metric('Recent clocked-in hours', formatSeconds(totals.hours)),
        metric('Recent task time', formatSeconds(totals.task)),
        metric('Available ClickUp tasks', totals.tasks),
      ].join('');
      personGrid.innerHTML = visible.length ? visible.map(renderPerson).join('') : '<div class="empty">No interns match these filters.</div>';
    }

    function renderPerson(person) {
      const warnings = Array.isArray(person.warnings) && person.warnings.length
        ? `<div class="chip-list">${person.warnings.map((warning) => `<span class="chip warning">${escapeHtml(warning)}</span>`).join('')}</div>`
        : '';
      const days = Array.isArray(person.days) && person.days.length
        ? `<div class="day-grid">${person.days.map(renderDay).join('')}</div>`
        : '<div class="empty">No recent session data found.</div>';
      const tasks = Array.isArray(person.available_tasks) && person.available_tasks.length
        ? `<div class="task-grid">${person.available_tasks.map(renderTask).join('')}</div>`
        : '<div class="empty">No ClickUp task options loaded for this intern.</div>';
      const correction = Array.isArray(person.available_tasks) && person.available_tasks.length
        ? `<details class="day-card"><summary>Correct active ClickUp task/project</summary><p class="muted">Use this when Don Pollo attached current work to the wrong assigned task. The change applies from now forward and is audited.</p><div class="key-grid"><select data-task-correction-task="${escapeHtml(person.user_key)}">${person.available_tasks.map((task) => `<option value="${escapeHtml(task.id)}" ${task.id === person.active_task_id ? 'selected' : ''}>${escapeHtml(task.name)} | ${escapeHtml(task.list_name || 'unknown list')}</option>`).join('')}</select><input data-task-correction-by="${escapeHtml(person.user_key)}" placeholder="Corrected by"><input data-task-correction-reason="${escapeHtml(person.user_key)}" placeholder="Reason for correction"><button type="button" data-task-correction-apply="${escapeHtml(person.user_key)}" data-session-date="${escapeHtml(person.current_session_date)}">Apply correction</button></div><p data-task-correction-result="${escapeHtml(person.user_key)}"></p></details>`
        : '';
      const bullets = buildOverviewBullets(person);
      return `<details class="person-card"><summary class="person-summary"><div class="person-title"><strong>${escapeHtml(person.display_name)}</strong><br><span class="muted">${escapeHtml(person.user_key)} | ${escapeHtml(person.timezone || '')}</span><br><span class="chip">${escapeHtml(person.clock_state || '')}</span></div><ul class="summary-bullets">${bullets.map((item) => `<li><strong>${escapeHtml(item.label)}:</strong> ${escapeHtml(item.value)}</li>`).join('')}</ul><div class="summary-meta"><div><span class="label">Stage</span><br>${escapeHtml(person.current_stage || '')}</div><div><span class="label">Recent hours</span><br>${formatSeconds(Number(person.net_clocked_in_seconds_recent || 0))}</div><span class="detail-hint">Click for daily log + ClickUp task list</span></div></summary><div class="person-body">${warnings}<div class="key-grid"><div><span class="label">Latest status</span><br>${escapeHtml(person.latest_status || 'No status captured.')}</div><div><span class="label">Latest blocker</span><br>${escapeHtml(person.latest_blocker || 'No blocker captured.')}</div><div><span class="label">Latest plan</span><br>${escapeHtml(person.latest_plan || 'No plan captured.')}</div></div>${correction}<h2>Detailed Recent Workdays</h2>${days}<h2>Available ClickUp Tasks</h2>${tasks}</div></details>`;
    }

    function buildOverviewBullets(person) {
      const recentTasks = collectRecentTaskNames(person);
      const activeTask = person.active_task_name || recentTasks[0] || 'No active task confirmed';
      const currentParts = [activeTask];
      const extraRecentTasks = recentTasks.filter((task) => task && task !== activeTask).slice(0, 2);
      if (extraRecentTasks.length) currentParts.push(`also recently: ${extraRecentTasks.join(', ')}`);
      const doneText = compactText(person.latest_status || latestMessageText(person) || '', 'No recent progress note captured.');
      const upcomingTasks = (Array.isArray(person.available_tasks) ? person.available_tasks : [])
        .filter((task) => String(task.name || '').trim())
        .filter((task) => String(task.name || '') !== person.active_task_name)
        .slice(0, 3)
        .map((task) => String(task.name || '').trim());
      const bullets = [
        { label: 'Working on', value: currentParts.join('; ') },
        { label: 'Done / recent', value: doneText },
        { label: 'Upcoming', value: upcomingTasks.length ? upcomingTasks.join('; ') : 'No assigned upcoming tasks loaded.' },
      ];
      if (person.latest_blocker) {
        bullets.push({ label: 'Needs attention', value: compactText(person.latest_blocker, 'No blocker captured.') });
      }
      return bullets;
    }

    function collectRecentTaskNames(person) {
      const names = [];
      if (person.active_task_name) names.push(String(person.active_task_name));
      (person.days || []).forEach((day) => {
        if (day.active_task_name) names.push(String(day.active_task_name));
        (day.time_by_task || []).forEach((task) => {
          const name = task.task_name || task.task_id || '';
          if (name) names.push(String(name));
        });
      });
      return Array.from(new Set(names.map((name) => name.trim()).filter(Boolean))).slice(0, 5);
    }

    function latestMessageText(person) {
      for (const day of person.days || []) {
        const messages = Array.isArray(day.recent_messages) ? day.recent_messages : [];
        for (let index = messages.length - 1; index >= 0; index -= 1) {
          if (messages[index].content) return messages[index].content;
        }
      }
      return '';
    }

    function compactText(value, fallback) {
      const text = String(value || '').replace(/\\s+/g, ' ').trim();
      if (!text) return fallback;
      return text.length > 150 ? `${text.slice(0, 147).trim()}...` : text;
    }

    function renderDay(day) {
      const tasks = Array.isArray(day.time_by_task) && day.time_by_task.length
        ? `<div class="chip-list">${day.time_by_task.map((task) => `<span class="chip">${escapeHtml(task.task_name || task.task_id || 'Task')}: ${formatSeconds(Number(task.seconds || 0))}</span>`).join('')}</div>`
        : '<span class="muted">No per-task time recorded.</span>';
      const messages = Array.isArray(day.recent_messages) && day.recent_messages.length
        ? day.recent_messages.map((message) => `<div class="message"><span class="label">${formatDateTime(message.created_at)}${message.attachment_count ? ` | ${message.attachment_count} attachment(s)` : ''}</span><br>${escapeHtml(message.content || '[attachment only]')}</div>`).join('')
        : '<span class="muted">No recent inbound messages stored for this day.</span>';
      return `<details class="day-card"><summary>${escapeHtml(day.session_date)} | ${formatSeconds(Number(day.clocked_in_total_seconds || 0))} clocked in | ${formatSeconds(Number(day.task_tracked_total_seconds || 0))} task | ${escapeHtml(day.active_task_name || 'no active task')}</summary><div class="key-grid"><div><span class="label">Clocked-in time</span><br>${formatSeconds(Number(day.clocked_in_total_seconds || 0))}</div><div><span class="label">Stage</span><br>${escapeHtml(day.stage || '')}</div><div><span class="label">Status</span><br>${escapeHtml(day.latest_status || 'No status captured.')}</div><div><span class="label">Blocker</span><br>${escapeHtml(day.latest_blocker || 'No blocker captured.')}</div><div><span class="label">Plan</span><br>${escapeHtml(day.latest_plan || 'No plan captured.')}</div></div><div><span class="label">Task time</span>${tasks}</div><div><span class="label">Recent messages</span>${messages}</div></details>`;
    }

    function renderTask(task) {
      const link = task.url ? `<a href="${escapeHtml(task.url)}" target="_blank" rel="noreferrer">${escapeHtml(task.id || 'open')}</a>` : escapeHtml(task.id || '');
      return `<article class="task-card"><strong>${escapeHtml(task.name || 'Untitled task')}</strong><br><span class="muted">id=${link} | status=${escapeHtml(task.status || 'unknown')} | priority=${escapeHtml(task.priority || 'none')} | list=${escapeHtml(task.list_name || 'unknown')}</span></article>`;
    }

    document.addEventListener('click', async (event) => {
      const button = event.target.closest('[data-task-correction-apply]');
      if (!button) return;
      const userKey = button.dataset.taskCorrectionApply;
      const resultNode = document.querySelector(`[data-task-correction-result="${CSS.escape(userKey)}"]`);
      const task = document.querySelector(`[data-task-correction-task="${CSS.escape(userKey)}"]`);
      const correctedBy = document.querySelector(`[data-task-correction-by="${CSS.escape(userKey)}"]`);
      const reason = document.querySelector(`[data-task-correction-reason="${CSS.escape(userKey)}"]`);
      button.disabled = true;
      resultNode.textContent = 'Applying correction...';
      try {
        const response = await fetch('/api/work-assignment/resolve', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            user_key: userKey,
            session_date: button.dataset.sessionDate,
            task_id: task.value,
            corrected_by: correctedBy.value,
            reason: reason.value,
          }),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || 'Correction failed.');
        resultNode.textContent = payload.warning || `Active task corrected to ${payload.task_name}.`;
        await refreshData();
      } catch (error) {
        resultNode.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });

    function metric(label, value) {
      return `<article class="metric-card"><div class="label">${escapeHtml(label)}</div><div class="metric-value">${escapeHtml(String(value))}</div></article>`;
    }

    function formatSeconds(totalSeconds) {
      const seconds = Math.max(0, Number(totalSeconds || 0));
      const minutesTotal = Math.floor(seconds / 60);
      const hours = Math.floor(minutesTotal / 60);
      const minutes = minutesTotal % 60;
      if (hours && minutes) return `${hours}h ${minutes}m`;
      if (hours) return `${hours}h`;
      return `${minutes}m`;
    }

    function formatDateTime(value) {
      if (!value) return '';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return date.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    }

    function escapeHtml(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    async function refreshData() {
      if (refreshInFlight) return;
      refreshInFlight = true;
      try {
        const response = await fetch('/api/work-dashboard-data', { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const nextPayload = await response.json();
        hydrate(nextPayload, true);
      } catch (error) {
        refreshPill.textContent = `Refresh failed: ${error.message || error}`;
      } finally {
        refreshInFlight = false;
      }
    }

    internFilter.addEventListener('change', () => { state.intern = internFilter.value; render(); });
    taskSearch.addEventListener('input', () => { state.query = taskSearch.value; render(); });
    resetButton.addEventListener('click', () => { state.intern = ''; state.query = ''; populateControls(); render(); });
    hydrate(payload, false);
    if (Number(payload.auto_refresh_interval_ms || 0) > 0) {
      refreshTimer = window.setInterval(refreshData, Number(payload.auto_refresh_interval_ms));
    }
  </script>
</body>
</html>
"""
