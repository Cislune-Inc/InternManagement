from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_TIME_TRACKING_REPORT_RELATIVE_PATH = Path("dashboard") / "time_tracking" / "time_tracking.csv"
_TIME_TRACKING_DASHBOARD_RELATIVE_PATH = Path("dashboard") / "time_tracking" / "time_tracking_dashboard.html"
_RETRO_AUDIT_RELATIVE_PATH = Path("dashboard") / "time_tracking" / "retro_backfill"
_LIVE_DASHBOARD_URL = "http://127.0.0.1:8765/"
_LIVE_WORK_DASHBOARD_URL = "http://127.0.0.1:8765/work"
_EDITOR_AUTO_REFRESH_INTERVAL_MS = 10 * 60 * 1000

logger = logging.getLogger(__name__)


def _raise_csv_field_limit() -> None:
    # The dashboard embeds JSON details in CSV columns; real backfill rows can exceed Python's small default limit.
    limit = 1024 * 1024
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 2


def write_time_tracking_dashboard(storage_root: Path | None) -> Path | None:
    if storage_root is None:
        return None
    report_path = storage_root / _TIME_TRACKING_DASHBOARD_RELATIVE_PATH
    try:
        payload = build_time_tracking_dashboard_payload(storage_root)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            render_time_tracking_dashboard_html(payload),
            encoding="utf-8",
        )
    except OSError:
        logger.exception("Failed to write time tracking dashboard to %s.", report_path)
        return None
    return report_path.resolve()


def build_time_tracking_dashboard_payload(
    storage_root: Path,
    *,
    runtime: Any | None = None,
    reference_now: datetime | None = None,
    editor_mode: bool = False,
) -> dict[str, Any]:
    hours_rows = _load_hours_rows(storage_root / _TIME_TRACKING_REPORT_RELATIVE_PATH)
    sessions_by_key = {
        (str(row.get("user_key") or ""), str(row.get("session_date") or "")): row
        for row in hours_rows
    }
    audit_runs = _load_audit_runs(storage_root / _RETRO_AUDIT_RELATIVE_PATH, sessions_by_key)
    generated_at = (reference_now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    if editor_mode and runtime is not None:
        _augment_hours_rows_for_editor(
            hours_rows,
            runtime=runtime,
            reference_now=reference_now or datetime.now(timezone.utc),
        )
    _augment_hours_rows_with_latest_audit(hours_rows, audit_runs)
    interns = _collect_interns(hours_rows, audit_runs)
    session_dates = sorted(
        {
            str(row.get("session_date") or "")
            for row in hours_rows
            if str(row.get("session_date") or "")
        }
        | {
            str(row.get("session_date") or "")
            for run in audit_runs
            for row in run.get("rows", [])
            if str(row.get("session_date") or "")
        }
    )
    return {
        "generated_at": generated_at,
        "hours_rows": hours_rows,
        "audit_runs": audit_runs,
        "interns": interns,
        "default_audit_run_id": str(audit_runs[0].get("run_id") or "") if audit_runs else "",
        "date_bounds": {
            "min": session_dates[0] if session_dates else "",
            "max": session_dates[-1] if session_dates else "",
        },
        "editor_mode": editor_mode,
        "editor_launch_command": ".venv/bin/python -m agent.hours_editor",
        "live_dashboard_url": _LIVE_DASHBOARD_URL,
        "live_work_dashboard_url": _LIVE_WORK_DASHBOARD_URL,
        "auto_refresh_interval_ms": _EDITOR_AUTO_REFRESH_INTERVAL_MS if editor_mode else 0,
        "editor_api_base": "",
    }


def render_time_tracking_dashboard_html(
    payload: dict[str, Any],
    *,
    editor_mode: bool | None = None,
) -> str:
    resolved_editor_mode = bool(payload.get("editor_mode")) if editor_mode is None else editor_mode
    payload_for_render = dict(payload)
    payload_for_render["editor_mode"] = resolved_editor_mode
    payload_json = json.dumps(payload_for_render, sort_keys=True, separators=(",", ":")).replace("</", "<\\/")
    default_audit_run_id = str(payload_for_render.get("default_audit_run_id") or "")
    editor_hint = str(payload_for_render.get("editor_launch_command") or ".venv/bin/python -m agent.hours_editor")
    live_dashboard_url = str(payload_for_render.get("live_dashboard_url") or _LIVE_DASHBOARD_URL)
    live_work_dashboard_url = str(payload_for_render.get("live_work_dashboard_url") or _LIVE_WORK_DASHBOARD_URL)
    notice_markup = (
        "<div class=\"notice-banner notice-live\">"
        "<strong>Local editor server mode.</strong> "
        f"Open this live app at <a class=\"path-link\" href=\"{_escape_html(live_dashboard_url)}\">{_escape_html(live_dashboard_url)}</a>. "
        f"Work dashboard: <a class=\"path-link\" href=\"{_escape_html(live_work_dashboard_url)}\">{_escape_html(live_work_dashboard_url)}</a>. "
        "Past workdays can be corrected here and saved back into SQLite, archived sessions, and hours reports."
        "</div>"
        if resolved_editor_mode
        else (
            "<div class=\"notice-banner notice-readonly\">"
            "<strong>Read-only snapshot.</strong> "
            f"To edit past workdays or see a live auto-refreshing view, launch <code>{_escape_html(editor_hint)}</code> "
            f"and open <a class=\"path-link\" href=\"{_escape_html(live_dashboard_url)}\">{_escape_html(live_dashboard_url)}</a>. "
            f"The live work dashboard is at <a class=\"path-link\" href=\"{_escape_html(live_work_dashboard_url)}\">{_escape_html(live_work_dashboard_url)}</a>."
            "</div>"
        )
    )
    editor_panel = _editor_panel_markup() if resolved_editor_mode else ""
    html = _dashboard_template()
    initial_live_refresh_text = (
        f"Live auto-refresh every {max(1, round(_EDITOR_AUTO_REFRESH_INTERVAL_MS / 60000))} minutes"
        if resolved_editor_mode
        else "Static snapshot mode"
    )
    return (
        html.replace("__DEFAULT_AUDIT_RUN_ID__", _escape_html(default_audit_run_id))
        .replace("__NOTICE_MARKUP__", notice_markup)
        .replace("__EDITOR_PANEL__", editor_panel)
        .replace("__LIVE_REFRESH_PILL__", _escape_html(initial_live_refresh_text))
        .replace("__PAYLOAD_JSON__", payload_json)
    )


def _editor_panel_markup() -> str:
    return """
    <section class="panel stack hidden" id="editor-panel">
      <div class="section-heading">
        <div>
          <h2>Manual Hours Editor</h2>
          <p class="muted" id="editor-session-summary">Pick a past workday from Hours Rollup to begin editing.</p>
        </div>
      </div>
      <div class="editor-grid">
        <div class="control-group">
          <label for="editor-edited-by">Edited by</label>
          <input id="editor-edited-by" type="text" placeholder="Required">
        </div>
        <div class="control-group editor-wide">
          <label for="editor-reason">Reason</label>
          <textarea id="editor-reason" rows="3" placeholder="Required"></textarea>
        </div>
      </div>
      <div class="stack" id="editor-segments"></div>
      <div class="editor-actions">
        <button type="button" id="editor-add-segment">Add segment</button>
        <button type="button" id="editor-preview">Preview changes</button>
        <button type="button" id="editor-apply" class="primary-button">Save changes</button>
        <button type="button" id="editor-cancel" class="secondary-button">Close editor</button>
      </div>
      <div id="editor-feedback" class="editor-feedback muted"></div>
      <div id="editor-preview-output" class="stack"></div>
    </section>
    """


def _dashboard_template() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Time Tracking Dashboard</title>
  <style>
    :root {
      --bg: #f4efe6;
      --panel: rgba(255, 251, 245, 0.92);
      --panel-strong: #fffaf1;
      --ink: #1f2a2d;
      --muted: #5d6a70;
      --accent: #0b7a75;
      --accent-soft: rgba(11, 122, 117, 0.12);
      --border: rgba(31, 42, 45, 0.12);
      --danger: #a6472b;
      --warning: #a66a00;
      --shadow: 0 18px 50px rgba(24, 34, 36, 0.12);
      --radius: 18px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: "Avenir Next", "Segoe UI", "Helvetica Neue", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(11, 122, 117, 0.18), transparent 28%),
        linear-gradient(180deg, #fbf7ef 0%, var(--bg) 100%);
    }
    .shell {
      width: min(1380px, calc(100vw - 32px));
      margin: 24px auto 40px;
      display: grid;
      gap: 20px;
    }
    .hero, .panel {
      background: var(--panel);
      backdrop-filter: blur(10px);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }
    .hero {
      overflow: hidden;
      position: relative;
      padding: 28px;
    }
    .hero::after {
      content: "";
      position: absolute;
      inset: auto -80px -80px auto;
      width: 240px;
      height: 240px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(11, 122, 117, 0.24), transparent 70%);
      pointer-events: none;
    }
    h1, h2 {
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      letter-spacing: -0.03em;
    }
    h1 {
      margin-bottom: 8px;
      font-size: clamp(2rem, 4vw, 3rem);
    }
    h2 {
      font-size: 1.5rem;
    }
    .hero p {
      margin: 0;
      max-width: 860px;
      color: var(--muted);
      line-height: 1.55;
    }
    .meta-row {
      margin-top: 16px;
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      color: var(--muted);
      font-size: 0.95rem;
    }
    .meta-pill {
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.6);
      border: 1px solid var(--border);
    }
    .notice-banner {
      margin-top: 18px;
      padding: 14px 16px;
      border-radius: 14px;
      border: 1px solid var(--border);
      line-height: 1.5;
      position: relative;
      z-index: 1;
    }
    .notice-readonly {
      background: rgba(255, 255, 255, 0.72);
    }
    .notice-live {
      background: rgba(11, 122, 117, 0.12);
    }
    .panel {
      padding: 20px;
    }
    .stack {
      display: grid;
      gap: 18px;
    }
    .controls-grid {
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 14px;
      align-items: end;
    }
    .control-group {
      display: grid;
      gap: 8px;
    }
    .control-group label {
      font-size: 0.82rem;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--muted);
    }
    select, input, textarea, button {
      width: 100%;
      border: 1px solid var(--border);
      background: var(--panel-strong);
      color: var(--ink);
      border-radius: 12px;
      padding: 12px 14px;
      font: inherit;
    }
    textarea {
      resize: vertical;
    }
    button {
      cursor: pointer;
      font-weight: 600;
      transition: transform 120ms ease, background 120ms ease;
    }
    button:hover {
      transform: translateY(-1px);
    }
    .primary-button {
      background: var(--accent);
      color: #f7fbfa;
    }
    .secondary-button {
      background: rgba(255, 255, 255, 0.7);
    }
    .view-switcher {
      display: inline-flex;
      gap: 8px;
      background: rgba(255, 255, 255, 0.64);
      padding: 6px;
      border: 1px solid var(--border);
      border-radius: 14px;
    }
    .view-switcher button {
      border: 0;
      background: transparent;
      color: var(--muted);
      padding: 10px 16px;
    }
    .view-switcher button.active {
      background: var(--accent);
      color: #f7fbfa;
      box-shadow: 0 10px 20px rgba(11, 122, 117, 0.2);
    }
    .summary-strip {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
    }
    .metric-card {
      padding: 16px;
      border-radius: 16px;
      background: linear-gradient(180deg, rgba(255,255,255,0.9), rgba(255,249,240,0.92));
      border: 1px solid var(--border);
    }
    .metric-label {
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--muted);
    }
    .metric-value {
      margin-top: 8px;
      font-size: 1.45rem;
      font-weight: 700;
      word-break: break-word;
    }
    .table-wrap {
      overflow-x: auto;
      border: 1px solid var(--border);
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.72);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 840px;
    }
    th, td {
      padding: 14px 16px;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
      text-align: left;
    }
    th {
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--muted);
      background: rgba(245, 239, 230, 0.7);
    }
    tbody tr:hover {
      background: rgba(11, 122, 117, 0.05);
    }
    details {
      border-radius: 14px;
    }
    details > summary {
      cursor: pointer;
      list-style: none;
      font-weight: 600;
    }
    details > summary::-webkit-details-marker {
      display: none;
    }
    .detail-toggle > summary {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      background: var(--accent-soft);
      color: var(--accent);
      border-radius: 12px;
    }
    .detail-toggle[open] > summary {
      border-bottom-left-radius: 0;
      border-bottom-right-radius: 0;
    }
    .detail-body {
      margin-top: 0;
      padding: 14px;
      background: rgba(255, 255, 255, 0.82);
      border: 1px solid var(--border);
      border-top: 0;
      border-bottom-left-radius: 14px;
      border-bottom-right-radius: 14px;
      display: grid;
      gap: 12px;
    }
    .day-card {
      border: 1px solid var(--border);
      border-radius: 14px;
      background: rgba(251, 248, 242, 0.92);
      overflow: hidden;
    }
    .day-card > summary {
      padding: 14px;
      background: rgba(255, 255, 255, 0.64);
    }
    .day-card .detail-body {
      border: 0;
      border-top: 1px solid var(--border);
      border-radius: 0;
      padding: 14px;
      background: transparent;
    }
    .key-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
    }
    .key-grid div {
      padding: 10px 12px;
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.75);
      border: 1px solid var(--border);
    }
    .label {
      display: block;
      margin-bottom: 4px;
      font-size: 0.76rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--muted);
    }
    .muted {
      color: var(--muted);
    }
    .danger { color: var(--danger); }
    .warning { color: var(--warning); }
    .chip-list {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .chip {
      padding: 6px 10px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.86);
      border: 1px solid var(--border);
      font-size: 0.88rem;
    }
    .review-badge {
      display: inline-flex;
      align-items: center;
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 0.82rem;
      font-weight: 700;
      letter-spacing: 0.01em;
      border: 1px solid transparent;
    }
    .review-in-progress {
      color: var(--accent);
      background: rgba(11, 122, 117, 0.12);
      border-color: rgba(11, 122, 117, 0.2);
    }
    .review-likely-correct {
      color: #17633f;
      background: rgba(23, 99, 63, 0.12);
      border-color: rgba(23, 99, 63, 0.22);
    }
    .review-needs-review {
      color: var(--warning);
      background: rgba(166, 106, 0, 0.12);
      border-color: rgba(166, 106, 0, 0.2);
    }
    .review-likely-wrong {
      color: var(--danger);
      background: rgba(166, 71, 43, 0.12);
      border-color: rgba(166, 71, 43, 0.2);
    }
    .path-link {
      word-break: break-all;
      color: var(--accent);
    }
    .empty-state {
      padding: 24px;
      border-radius: 16px;
      border: 1px dashed var(--border);
      color: var(--muted);
      background: rgba(255,255,255,0.45);
    }
    .hidden {
      display: none !important;
    }
    .section-heading {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: start;
    }
    .inline-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    .editor-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }
    .editor-wide {
      grid-column: 1 / -1;
    }
    .segment-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 140px;
      gap: 12px;
      padding: 14px;
      border-radius: 14px;
      border: 1px solid var(--border);
      background: rgba(255, 255, 255, 0.7);
      align-items: end;
    }
    .editor-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
    }
    .editor-actions button {
      width: auto;
      min-width: 140px;
    }
    .editor-feedback {
      min-height: 1.25rem;
    }
    @media (max-width: 920px) {
      .controls-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
      .segment-row {
        grid-template-columns: 1fr;
      }
    }
    @media (max-width: 640px) {
      .shell {
        width: min(100vw - 18px, 100%);
        margin: 12px auto 24px;
      }
      .hero, .panel {
        padding: 16px;
      }
      .controls-grid, .editor-grid {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <main class="shell" id="time-tracking-dashboard" data-default-audit-run-id="__DEFAULT_AUDIT_RUN_ID__">
    <section class="hero">
      <div class="meta-row">
        <a class="path-link" href="http://127.0.0.1:8765/time">Time Tracking</a>
        <a class="path-link" href="http://127.0.0.1:8765/work">Work Dashboard</a>
        <a class="path-link" href="http://127.0.0.1:8765/payroll">Payroll</a>
        <a class="path-link" href="http://127.0.0.1:8765/exceptions">Manager Queue</a>
        <a class="path-link" href="http://127.0.0.1:8765/health">System Health</a>
      </div>
      <h1>Time Tracking Dashboard</h1>
      <p>Browse archived hours and retroactive backfill audit details in one local dashboard. Filters update the totals, and nested dropdowns reveal per-day task time, provenance, warnings, chosen timestamps, and manual correction history.</p>
      <div class="meta-row">
        <span class="meta-pill">Source artifacts stay local on disk</span>
        <span class="meta-pill">Single-file HTML, safe for file://</span>
        <span class="meta-pill" id="generated-at-pill">Generated from local storage</span>
        <span class="meta-pill" id="live-refresh-pill">__LIVE_REFRESH_PILL__</span>
      </div>
      __NOTICE_MARKUP__
    </section>
    <section class="panel stack">
      <div class="view-switcher" role="tablist" aria-label="Dashboard views">
        <button type="button" id="view-hours-button" data-view="hours" class="active">Hours Rollup</button>
        <button type="button" id="view-audit-button" data-view="audit">Backfill Audit</button>
      </div>
      <div class="controls-grid">
        <div class="control-group">
          <label for="intern-filter">Intern</label>
          <select id="intern-filter"></select>
        </div>
        <div class="control-group">
          <label for="date-from-filter">From</label>
          <input id="date-from-filter" type="date">
        </div>
        <div class="control-group">
          <label for="date-to-filter">To</label>
          <input id="date-to-filter" type="date">
        </div>
        <div class="control-group" id="audit-run-group">
          <label for="audit-run-filter">Audit Run</label>
          <select id="audit-run-filter"></select>
        </div>
        <div class="control-group" id="confidence-group">
          <label for="confidence-filter">Confidence</label>
          <select id="confidence-filter">
            <option value="all">All confidence levels</option>
            <option value="high">High</option>
            <option value="medium">Medium</option>
            <option value="low">Low</option>
            <option value="unresolved">Unresolved</option>
          </select>
        </div>
        <div class="control-group" id="review-group">
          <label for="review-filter">Review Status</label>
          <select id="review-filter">
            <option value="all">All review states</option>
            <option value="in_progress">In progress</option>
            <option value="likely_correct">Likely correct</option>
            <option value="needs_review">Needs review</option>
            <option value="likely_wrong">Likely wrong</option>
          </select>
        </div>
        <div class="control-group">
          <label for="reset-filters-button">Reset</label>
          <button id="reset-filters-button" type="button">Reset filters</button>
        </div>
      </div>
    </section>
    <section class="panel stack">
      <div id="view-summary" class="summary-strip"></div>
      <div id="view-content"></div>
    </section>
    __EDITOR_PANEL__
  </main>
  <script id="time-tracking-dashboard-data" type="application/json">__PAYLOAD_JSON__</script>
  <script>
    let payload = JSON.parse(document.getElementById('time-tracking-dashboard-data').textContent || '{}');
    let hoursRows = [];
    let auditRuns = [];
    let interns = [];
    let dateBounds = {};
    const state = {
      view: 'hours',
      intern: '',
      from: '',
      to: '',
      auditRunId: '',
      confidence: 'all',
      review: 'all',
    };
    const editorMode = Boolean(payload.editor_mode);
    const editorApiBase = String(payload.editor_api_base || '');
    const autoRefreshIntervalMs = Number(payload.auto_refresh_interval_ms || 0);
    let hoursRowIndex = new Map();
    let activeEditorKey = '';
    let latestEditorPreview = null;
    let autoRefreshTimer = null;
    let autoRefreshInFlight = false;

    const viewSummary = document.getElementById('view-summary');
    const viewContent = document.getElementById('view-content');
    const internFilter = document.getElementById('intern-filter');
    const dateFromFilter = document.getElementById('date-from-filter');
    const dateToFilter = document.getElementById('date-to-filter');
    const auditRunFilter = document.getElementById('audit-run-filter');
    const confidenceFilter = document.getElementById('confidence-filter');
    const reviewFilter = document.getElementById('review-filter');
    const auditRunGroup = document.getElementById('audit-run-group');
    const confidenceGroup = document.getElementById('confidence-group');
    const reviewGroup = document.getElementById('review-group');
    const generatedAtPill = document.getElementById('generated-at-pill');
    const liveRefreshPill = document.getElementById('live-refresh-pill');

    hydratePayload(payload, { preserveFilters: false });
    bindControls();
    bindHoursActions();
    if (editorMode) bindEditorControls();
    startAutoRefreshLoop();
    render();

    function hydratePayload(nextPayload, options) {
      const preserveFilters = Boolean(options && options.preserveFilters);
      payload = nextPayload || {};
      hoursRows = Array.isArray(payload.hours_rows) ? payload.hours_rows : [];
      auditRuns = Array.isArray(payload.audit_runs) ? payload.audit_runs : [];
      interns = Array.isArray(payload.interns) ? payload.interns : [];
      dateBounds = payload.date_bounds || {};
      hoursRows.forEach((row) => {
        row.row_key = row.row_key || `${row.user_key || ''}::${row.session_date || ''}`;
      });
      hoursRowIndex = new Map(hoursRows.map((row) => [row.row_key, row]));
      generatedAtPill.textContent = editorMode
        ? `Updated from disk ${formatDateTime(payload.generated_at)}`
        : `Generated ${formatDateTime(payload.generated_at)}`;
      populateControls();
      if (!preserveFilters) {
        state.intern = '';
        state.from = dateBounds.min || '';
        state.to = dateBounds.max || '';
        state.auditRunId = payload.default_audit_run_id || '';
        state.confidence = 'all';
        state.review = 'all';
      } else {
        if (!state.from) state.from = dateBounds.min || '';
        if (!state.to) state.to = dateBounds.max || '';
        if (!auditRuns.some((run) => run.run_id === state.auditRunId)) {
          state.auditRunId = payload.default_audit_run_id || '';
        }
      }
    }

    function startAutoRefreshLoop() {
      if (!liveRefreshPill) return;
      if (!editorMode || autoRefreshIntervalMs <= 0) {
        liveRefreshPill.textContent = 'Static snapshot mode';
        return;
      }
      const minutes = Math.max(1, Math.round(autoRefreshIntervalMs / 60000));
      liveRefreshPill.textContent = `Live auto-refresh every ${minutes} minutes`;
      if (autoRefreshTimer) {
        clearInterval(autoRefreshTimer);
      }
      autoRefreshTimer = window.setInterval(async () => {
        if (autoRefreshInFlight) {
          return;
        }
        if (shouldPauseAutoRefresh()) {
          liveRefreshPill.textContent = 'Live sync paused while editing';
          return;
        }
        try {
          autoRefreshInFlight = true;
          await refreshDashboardData();
          liveRefreshPill.textContent = `Live sync updated ${formatDateTime(new Date().toISOString())}`;
        } catch (_error) {
          liveRefreshPill.textContent = 'Live sync failed; will retry automatically';
        } finally {
          autoRefreshInFlight = false;
        }
      }, autoRefreshIntervalMs);
    }

    function shouldPauseAutoRefresh() {
      return Boolean(activeEditorKey);
    }

    function bindControls() {
      document.querySelectorAll('[data-view]').forEach((button) => {
        button.addEventListener('click', () => {
          state.view = button.dataset.view || 'hours';
          render();
        });
      });
      internFilter.addEventListener('change', () => { state.intern = internFilter.value; render(); });
      dateFromFilter.addEventListener('change', () => { state.from = dateFromFilter.value; render(); });
      dateToFilter.addEventListener('change', () => { state.to = dateToFilter.value; render(); });
      auditRunFilter.addEventListener('change', () => { state.auditRunId = auditRunFilter.value; render(); });
      confidenceFilter.addEventListener('change', () => { state.confidence = confidenceFilter.value; render(); });
      reviewFilter.addEventListener('change', () => { state.review = reviewFilter.value; render(); });
      document.getElementById('reset-filters-button').addEventListener('click', () => {
        state.intern = '';
        state.from = dateBounds.min || '';
        state.to = dateBounds.max || '';
        state.auditRunId = payload.default_audit_run_id || '';
        state.confidence = 'all';
        state.review = 'all';
        render();
      });
    }

    function bindHoursActions() {
      viewContent.addEventListener('click', (event) => {
        const button = event.target.closest('[data-open-editor]');
        if (!button || !editorMode) return;
        openEditorForRow(String(button.getAttribute('data-open-editor') || ''));
      });
    }

    function bindEditorControls() {
      const editorPanel = document.getElementById('editor-panel');
      if (!editorPanel) return;
      document.getElementById('editor-add-segment').addEventListener('click', () => {
        const segments = currentEditorSegments();
        segments.push({ start_local: '', end_local: '' });
        writeEditorSegments(segments);
      });
      document.getElementById('editor-preview').addEventListener('click', async () => {
        await previewEditorChanges();
      });
      document.getElementById('editor-apply').addEventListener('click', async () => {
        await applyEditorChanges();
      });
      document.getElementById('editor-cancel').addEventListener('click', () => {
        activeEditorKey = '';
        latestEditorPreview = null;
        editorPanel.classList.add('hidden');
        setEditorFeedback('');
      });
      document.getElementById('editor-segments').addEventListener('click', (event) => {
        const removeButton = event.target.closest('[data-remove-segment]');
        if (!removeButton) return;
        const index = Number(removeButton.getAttribute('data-remove-segment') || '-1');
        const segments = currentEditorSegments();
        if (index >= 0 && index < segments.length) {
          segments.splice(index, 1);
          writeEditorSegments(segments);
        }
      });
    }

    function populateControls() {
      internFilter.innerHTML = '<option value="">All interns</option>' + interns.map((intern) => `<option value="${escapeHtml(intern.user_key)}">${escapeHtml(intern.display_name)}</option>`).join('');
      auditRunFilter.innerHTML = auditRuns.length
        ? auditRuns.map((run) => `<option value="${escapeHtml(run.run_id)}">${escapeHtml(run.label)}</option>`).join('')
        : '<option value="">No audit runs found</option>';
      dateFromFilter.min = dateBounds.min || '';
      dateFromFilter.max = dateBounds.max || '';
      dateToFilter.min = dateBounds.min || '';
      dateToFilter.max = dateBounds.max || '';
    }

    function render() {
      document.querySelectorAll('[data-view]').forEach((button) => {
        button.classList.toggle('active', button.dataset.view === state.view);
      });
      internFilter.value = state.intern;
      dateFromFilter.value = state.from;
      dateToFilter.value = state.to;
      auditRunFilter.value = state.auditRunId;
      confidenceFilter.value = state.confidence;
      reviewFilter.value = state.review;
      const showingAudit = state.view === 'audit';
      auditRunGroup.classList.toggle('hidden', !showingAudit);
      confidenceGroup.classList.toggle('hidden', !showingAudit);
      reviewGroup.classList.toggle('hidden', showingAudit);
      if (showingAudit) {
        renderAuditView();
      } else {
        renderHoursView();
      }
    }

    function renderHoursView() {
      const rows = filterByDateAndIntern(hoursRows);
      const grouped = groupHoursRows(rows);
      const reviewCounts = rows.reduce((acc, row) => {
        const status = String(row.review_status || 'likely_correct');
        acc[status] = (acc[status] || 0) + 1;
        return acc;
      }, { in_progress: 0, likely_correct: 0, needs_review: 0, likely_wrong: 0 });
      const totals = rows.reduce((acc, row) => {
        acc.clocked += Number(row.clocked_in_total_seconds || 0);
        acc.task += Number(row.task_tracked_total_seconds || 0);
        return acc;
      }, { clocked: 0, task: 0 });
      viewSummary.innerHTML = renderSummaryCards([
        { label: 'Visible days', value: String(rows.length) },
        { label: 'Likely correct', value: String(reviewCounts.likely_correct || 0) },
        { label: 'Needs review', value: String(reviewCounts.needs_review || 0) },
        { label: 'Likely wrong', value: String(reviewCounts.likely_wrong || 0) },
        { label: 'In progress', value: String(reviewCounts.in_progress || 0) },
        { label: 'Clocked-in total', value: formatSeconds(totals.clocked) },
        { label: 'Task-tracked total', value: formatSeconds(totals.task) },
      ]);
      if (!grouped.length) {
        viewContent.innerHTML = '<div class="empty-state">No archived hour rows match the current filters.</div>';
        return;
      }
      viewContent.innerHTML = `<div class="table-wrap"><table><thead><tr><th>Intern</th><th>Days</th><th>Clocked-in</th><th>Task-tracked</th><th>Review</th><th>Details</th></tr></thead><tbody>${grouped.map(renderHoursGroupRow).join('')}</tbody></table></div>`;
    }

    function renderAuditView() {
      if (!auditRuns.length) {
        viewSummary.innerHTML = renderSummaryCards([{ label: 'Audit runs', value: '0' }]);
        viewContent.innerHTML = '<div class="empty-state">No retro backfill audit runs have been generated yet.</div>';
        return;
      }
      const run = auditRuns.find((item) => item.run_id === state.auditRunId) || auditRuns[0];
      if (!state.auditRunId) {
        state.auditRunId = run.run_id;
        auditRunFilter.value = run.run_id;
      }
      const filteredRows = filterAuditRows(run.rows || []);
      const grouped = groupAuditRows(filteredRows);
      const totals = filteredRows.reduce((acc, row) => {
        acc.beforeClocked += Number(row.clocked_in_before_seconds || 0);
        acc.afterClocked += Number(row.clocked_in_after_seconds || 0);
        acc.beforeTask += Number(row.task_tracked_before_seconds || 0);
        acc.afterTask += Number(row.task_tracked_after_seconds || 0);
        acc.changed += row.changed ? 1 : 0;
        acc.unresolved += row.confidence === 'unresolved' ? 1 : 0;
        return acc;
      }, { beforeClocked: 0, afterClocked: 0, beforeTask: 0, afterTask: 0, changed: 0, unresolved: 0 });
      viewSummary.innerHTML = renderSummaryCards([
        { label: 'Selected run', value: escapeHtml(run.run_id) },
        { label: 'Visible days', value: String(filteredRows.length) },
        { label: 'Clocked delta', value: formatDeltaSeconds(totals.afterClocked - totals.beforeClocked) },
        { label: 'Task delta', value: formatDeltaSeconds(totals.afterTask - totals.beforeTask) },
        { label: 'Changed days', value: String(totals.changed) },
        { label: 'Unresolved days', value: String(totals.unresolved) },
      ]);
      const runMeta = `<div class="key-grid"><div><span class="label">Run mode</span>${run.apply ? 'Apply' : 'Dry run'}</div><div><span class="label">Processed days</span>${String(run.processed_days || 0)}</div><div><span class="label">Changed days</span>${String(run.changed_days || 0)}</div><div><span class="label">Warnings</span>${String(run.warnings_count || 0)}</div></div>`;
      if (!grouped.length) {
        viewContent.innerHTML = runMeta + '<div class="empty-state">No audit rows match the current filters for this run.</div>';
        return;
      }
      viewContent.innerHTML = runMeta + `<div class="table-wrap"><table><thead><tr><th>Intern</th><th>Before</th><th>After</th><th>Delta</th><th>Changed days</th><th>Unresolved</th><th>Details</th></tr></thead><tbody>${grouped.map(renderAuditGroupRow).join('')}</tbody></table></div>`;
    }

    function filterByDateAndIntern(rows) {
      const bounds = normalizedBounds();
      return rows.filter((row) => {
        if (state.intern && row.user_key !== state.intern) return false;
        if (state.review !== 'all' && row.review_status !== state.review) return false;
        return withinBounds(row.session_date, bounds.from, bounds.to);
      });
    }

    function filterAuditRows(rows) {
      const bounds = normalizedBounds();
      return rows.filter((row) => {
        if (state.intern && row.user_key !== state.intern) return false;
        if (state.confidence !== 'all' && row.confidence !== state.confidence) return false;
        return withinBounds(row.session_date, bounds.from, bounds.to);
      });
    }

    function groupHoursRows(rows) {
      const byUser = new Map();
      rows.forEach((row) => {
        const key = row.user_key;
        if (!byUser.has(key)) {
          byUser.set(key, {
            user_key: key,
            display_name: row.display_name || key,
            rows: [],
            clocked: 0,
            task: 0,
            reviewCounts: { in_progress: 0, likely_correct: 0, needs_review: 0, likely_wrong: 0 },
          });
        }
        const bucket = byUser.get(key);
        bucket.rows.push(row);
        bucket.clocked += Number(row.clocked_in_total_seconds || 0);
        bucket.task += Number(row.task_tracked_total_seconds || 0);
        const status = String(row.review_status || 'likely_correct');
        bucket.reviewCounts[status] = (bucket.reviewCounts[status] || 0) + 1;
      });
      return Array.from(byUser.values()).sort((a, b) => a.display_name.localeCompare(b.display_name));
    }

    function groupAuditRows(rows) {
      const byUser = new Map();
      rows.forEach((row) => {
        const key = row.user_key;
        if (!byUser.has(key)) {
          byUser.set(key, { user_key: key, display_name: row.display_name || key, rows: [], beforeClocked: 0, afterClocked: 0, beforeTask: 0, afterTask: 0, changed: 0, unresolved: 0 });
        }
        const bucket = byUser.get(key);
        bucket.rows.push(row);
        bucket.beforeClocked += Number(row.clocked_in_before_seconds || 0);
        bucket.afterClocked += Number(row.clocked_in_after_seconds || 0);
        bucket.beforeTask += Number(row.task_tracked_before_seconds || 0);
        bucket.afterTask += Number(row.task_tracked_after_seconds || 0);
        bucket.changed += row.changed ? 1 : 0;
        bucket.unresolved += row.confidence === 'unresolved' ? 1 : 0;
      });
      return Array.from(byUser.values()).sort((a, b) => a.display_name.localeCompare(b.display_name));
    }

    function renderHoursGroupRow(group) {
      const rows = [...group.rows].sort((a, b) => (a.session_date < b.session_date ? 1 : -1));
      const details = `<details class="detail-toggle"><summary>Daily breakdown</summary><div class="detail-body">${rows.map(renderHoursDayDetail).join('')}</div></details>`;
      return `<tr><td><strong>${escapeHtml(group.display_name)}</strong><br><span class="muted">${escapeHtml(group.user_key)}</span></td><td>${rows.length}</td><td>${formatSeconds(group.clocked)}</td><td>${formatSeconds(group.task)}</td><td>${renderReviewCounts(group.reviewCounts)}</td><td>${details}</td></tr>`;
    }

    function renderHoursDayDetail(row) {
      const reviewStatus = String(row.review_status || 'likely_correct');
      const tasks = Array.isArray(row.time_by_task) && row.time_by_task.length
        ? `<div class="chip-list">${row.time_by_task.map((task) => `<span class="chip">${escapeHtml(task.task_name || task.task_id || 'Unknown task')}: ${formatSeconds(Number(task.seconds || 0))}</span>`).join('')}</div>`
        : '<span class="muted">No per-task breakdown recorded for this day.</span>';
      const segments = Array.isArray(row.work_segments) && row.work_segments.length
        ? `<div class="chip-list">${row.work_segments.map((segment) => `<span class="chip">${escapeHtml(segment.start_local || '')} -> ${escapeHtml(segment.end_local || '')}</span>`).join('')}</div>`
        : '<span class="muted">No complete work segments stored.</span>';
      const sessionPath = row.session_uri
        ? `<a class="path-link" href="${escapeHtml(row.session_uri)}">${escapeHtml(row.session_path || '')}</a>`
        : '<span class="muted">Not available</span>';
      const manualEdit = row.latest_manual_edit
        ? `<div class="key-grid"><div><span class="label">Last edited by</span>${escapeHtml(row.latest_manual_edit.edited_by || '')}</div><div><span class="label">Edited at</span>${formatDateTime(row.latest_manual_edit.edited_at)}</div><div><span class="label">Edit count</span>${String(row.manual_edit_count || 0)}</div><div><span class="label">Reason</span>${escapeHtml(row.latest_manual_edit.reason || '')}</div></div>`
        : '<span class="muted">No manual edits recorded for this day.</span>';
      const reviewNotes = Array.isArray(row.review_reasons) && row.review_reasons.length
        ? `<div class="chip-list">${row.review_reasons.map((reason) => `<span class="chip">${escapeHtml(reason)}</span>`).join('')}</div>`
        : '<span class="muted">No review notes for this day.</span>';
      const manualEditLog = row.manual_edit_log_uri
        ? `<a class="path-link" href="${escapeHtml(row.manual_edit_log_uri)}">${escapeHtml(row.manual_edit_log_path || '')}</a>`
        : '<span class="muted">Not available</span>';
      let editorControls = '';
      if (editorMode) {
        if (row.editable) {
          editorControls = `<div class="inline-actions"><button type="button" class="secondary-button" data-open-editor="${escapeHtml(row.row_key)}">Edit past workday</button></div>`;
        } else {
          editorControls = `<div class="muted">Not editable: ${escapeHtml(row.edit_block_reason || 'Unknown reason.')}</div>`;
        }
      }
      return `<details class="day-card"><summary>${escapeHtml(row.session_date)} | ${renderReviewBadge(reviewStatus)} | ${formatSeconds(Number(row.clocked_in_total_seconds || 0))} clocked-in | ${formatSeconds(Number(row.task_tracked_total_seconds || 0))} task</summary><div class="detail-body"><div class="key-grid"><div><span class="label">Review status</span>${renderReviewBadge(reviewStatus)}</div><div><span class="label">Review summary</span>${escapeHtml(row.review_summary || 'No review summary.')}</div><div><span class="label">Clocked vs task gap</span>${escapeHtml(row.review_gap_human || '0m')}</div><div><span class="label">Latest audit confidence</span>${escapeHtml(row.latest_audit_confidence || row.retro_backfill_confidence || 'Not available')}</div><div><span class="label">Manual edits</span>${String(row.manual_edit_count || 0)}</div><div><span class="label">Latest audit warnings</span>${String(row.latest_audit_warning_count || row.retro_backfill_warning_count || 0)}</div><div><span class="label">Timezone</span>${escapeHtml(row.timezone || '')}</div><div><span class="label">Clocked-in</span>${formatSeconds(Number(row.clocked_in_total_seconds || 0))}</div><div><span class="label">Work segments</span>${String(row.work_segment_count || 0)}</div><div><span class="label">Open segment</span>${row.has_open_work_segment ? 'Yes' : 'No'}</div><div><span class="label">Task timer running</span>${row.active_task_timer_running ? 'Yes' : 'No'}</div></div><div><span class="label">Review notes</span>${reviewNotes}</div><div><span class="label">Segment windows</span>${segments}</div><div><span class="label">Task breakdown</span>${tasks}</div><div><span class="label">Latest manual edit</span>${manualEdit}</div><div><span class="label">Manual edit log</span>${manualEditLog}</div><div><span class="label">Session file</span>${sessionPath}</div>${editorControls}</div></details>`;
    }

    function renderAuditGroupRow(group) {
      const deltaClocked = group.afterClocked - group.beforeClocked;
      const rows = [...group.rows].sort((a, b) => (a.session_date < b.session_date ? 1 : -1));
      const details = `<details class="detail-toggle"><summary>Audit details</summary><div class="detail-body">${rows.map(renderAuditDayDetail).join('')}</div></details>`;
      return `<tr><td><strong>${escapeHtml(group.display_name)}</strong><br><span class="muted">${escapeHtml(group.user_key)}</span></td><td>${formatSeconds(group.beforeClocked)}</td><td>${formatSeconds(group.afterClocked)}</td><td>${formatDeltaSeconds(deltaClocked)}</td><td>${group.changed}</td><td>${group.unresolved}</td><td>${details}</td></tr>`;
    }

    function renderAuditDayDetail(row) {
      const warningMarkup = Array.isArray(row.warnings) && row.warnings.length ? `<div class="chip-list">${row.warnings.map((warning) => `<span class="chip warning">${escapeHtml(warning)}</span>`).join('')}</div>` : '<span class="muted">No warnings.</span>';
      const sourcesMarkup = `<div class="chip-list"><span class="chip">Clock in: ${escapeHtml(row.clock_in_source || 'unknown')}</span><span class="chip">Clock out: ${escapeHtml(row.clock_out_source || 'unknown')}</span><span class="chip">Task time: ${escapeHtml(row.task_time_source || 'unknown')}</span></div>`;
      const taskWindows = Array.isArray(row.task_windows) && row.task_windows.length ? `<div class="chip-list">${row.task_windows.map((window) => `<span class="chip">${escapeHtml(window.task_name || window.task_id || 'Task')} | ${formatDateTime(window.started_at)} -> ${formatDateTime(window.ended_at)} | ${formatSeconds(Number(window.duration_seconds || 0))}</span>`).join('')}</div>` : '<span class="muted">No explicit task windows recorded.</span>';
      const lunchWindows = Array.isArray(row.lunch_windows) && row.lunch_windows.length ? `<div class="chip-list">${row.lunch_windows.map((window) => `<span class="chip">${formatDateTime(window.started_at)} -> ${formatDateTime(window.ended_at)} (${escapeHtml(window.source || 'source unknown')})</span>`).join('')}</div>` : '<span class="muted">No lunch windows recorded.</span>';
      const explanationPath = row.explanation_uri ? `<a class="path-link" href="${escapeHtml(row.explanation_uri)}">${escapeHtml(row.explanation_path || '')}</a>` : '<span class="muted">Not available</span>';
      const sessionPath = row.session_uri ? `<a class="path-link" href="${escapeHtml(row.session_uri)}">${escapeHtml(row.session_path || '')}</a>` : '<span class="muted">Not available</span>';
      const detailError = row.detail_load_error ? `<div class="danger"><strong>Detail warning:</strong> ${escapeHtml(row.detail_load_error)}</div>` : '';
      return `<details class="day-card"><summary>${escapeHtml(row.session_date)} | ${escapeHtml(row.confidence || 'unknown')} | ${formatDeltaSeconds(Number(row.clocked_in_after_seconds || 0) - Number(row.clocked_in_before_seconds || 0))} clocked delta</summary><div class="detail-body">${detailError}<div class="key-grid"><div><span class="label">Before clocked-in</span>${formatSeconds(Number(row.clocked_in_before_seconds || 0))}</div><div><span class="label">After clocked-in</span>${formatSeconds(Number(row.clocked_in_after_seconds || 0))}</div><div><span class="label">Before task time</span>${formatSeconds(Number(row.task_tracked_before_seconds || 0))}</div><div><span class="label">After task time</span>${formatSeconds(Number(row.task_tracked_after_seconds || 0))}</div><div><span class="label">Confidence</span>${escapeHtml(row.confidence || 'unknown')}</div><div><span class="label">Changed</span>${row.changed ? 'Yes' : 'No'}</div><div><span class="label">Clock in at</span>${formatDateTime(row.clock_in_at)}</div><div><span class="label">Clock out at</span>${formatDateTime(row.clock_out_at)}</div></div><div><span class="label">Sources</span>${sourcesMarkup}</div><div><span class="label">Explanation</span>${row.explanation ? escapeHtml(row.explanation) : '<span class="muted">No explanation text found.</span>'}</div><div><span class="label">Warnings</span>${warningMarkup}</div><div><span class="label">Lunch windows</span>${lunchWindows}</div><div><span class="label">Task windows</span>${taskWindows}</div><div class="key-grid"><div><span class="label">Explanation file</span>${explanationPath}</div><div><span class="label">Session file</span>${sessionPath}</div></div></div></details>`;
    }

    function renderSummaryCards(cards) {
      return cards.map((card) => `<article class="metric-card"><div class="metric-label">${card.label}</div><div class="metric-value">${card.value}</div></article>`).join('');
    }

    function normalizedBounds() {
      let from = state.from || '';
      let to = state.to || '';
      if (from && to && from > to) {
        const swap = from;
        from = to;
        to = swap;
      }
      return { from, to };
    }

    function withinBounds(value, from, to) {
      if (!value) return false;
      if (from && value < from) return false;
      if (to && value > to) return false;
      return true;
    }

    function formatSeconds(totalSeconds) {
      const total = Math.max(0, Number(totalSeconds || 0));
      const hours = Math.floor(total / 3600);
      const minutes = Math.floor((total % 3600) / 60);
      if (hours && minutes) return `${hours}h ${minutes}m`;
      if (hours) return `${hours}h`;
      return `${minutes}m`;
    }

    function formatDeltaSeconds(totalSeconds) {
      const total = Number(totalSeconds || 0);
      if (total === 0) return '0m';
      const sign = total > 0 ? '+' : '-';
      return `${sign}${formatSeconds(Math.abs(total))}`;
    }

    function reviewStatusLabel(status) {
      switch (String(status || 'likely_correct')) {
        case 'in_progress':
          return 'In progress';
        case 'needs_review':
          return 'Needs review';
        case 'likely_wrong':
          return 'Likely wrong';
        default:
          return 'Likely correct';
      }
    }

    function reviewStatusClass(status) {
      switch (String(status || 'likely_correct')) {
        case 'in_progress':
          return 'review-in-progress';
        case 'needs_review':
          return 'review-needs-review';
        case 'likely_wrong':
          return 'review-likely-wrong';
        default:
          return 'review-likely-correct';
      }
    }

    function renderReviewBadge(status) {
      return `<span class="review-badge ${reviewStatusClass(status)}">${escapeHtml(reviewStatusLabel(status))}</span>`;
    }

    function renderReviewCounts(counts) {
      const entries = [
        ['likely_wrong', Number((counts || {}).likely_wrong || 0)],
        ['needs_review', Number((counts || {}).needs_review || 0)],
        ['in_progress', Number((counts || {}).in_progress || 0)],
        ['likely_correct', Number((counts || {}).likely_correct || 0)],
      ].filter((entry) => entry[1] > 0);
      if (!entries.length) {
        return '<span class="muted">No review signals</span>';
      }
      return `<div class="chip-list">${entries.map((entry) => `<span class="chip">${escapeHtml(reviewStatusLabel(entry[0]))}: ${entry[1]}</span>`).join('')}</div>`;
    }

    function formatDateTime(value) {
      if (!value) return 'Not available';
      const parsed = new Date(value);
      if (Number.isNaN(parsed.getTime())) return escapeHtml(String(value));
      return parsed.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
    }

    function escapeHtml(value) {
      return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\\"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function openEditorForRow(rowKey) {
      const row = hoursRowIndex.get(rowKey);
      const editorPanel = document.getElementById('editor-panel');
      if (!row || !editorPanel) return;
      activeEditorKey = rowKey;
      latestEditorPreview = null;
      document.getElementById('editor-session-summary').textContent = `${row.display_name || row.user_key} · ${row.session_date} · ${row.timezone || 'timezone unavailable'}`;
      document.getElementById('editor-edited-by').value = '';
      document.getElementById('editor-reason').value = '';
      document.getElementById('editor-preview-output').innerHTML = '';
      writeEditorSegments(Array.isArray(row.work_segments) && row.work_segments.length ? row.work_segments.map((segment) => ({ start_local: segment.start_local || '', end_local: segment.end_local || '' })) : [{ start_local: '', end_local: '' }]);
      setEditorFeedback(row.editable ? '' : (row.edit_block_reason || 'This day is not editable.'));
      editorPanel.classList.remove('hidden');
      editorPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function writeEditorSegments(segments) {
      const container = document.getElementById('editor-segments');
      if (!container) return;
      container.innerHTML = segments.map((segment, index) => `<div class="segment-row"><div class="control-group"><label for="segment-start-${index}">Clocked in</label><input id="segment-start-${index}" type="datetime-local" value="${escapeHtml(segment.start_local || '')}" data-segment-start="${index}"></div><div class="control-group"><label for="segment-end-${index}">Clocked out</label><input id="segment-end-${index}" type="datetime-local" value="${escapeHtml(segment.end_local || '')}" data-segment-end="${index}"></div><div class="control-group"><label>&nbsp;</label><button type="button" class="secondary-button" data-remove-segment="${index}">Remove</button></div></div>`).join('');
    }

    function currentEditorSegments() {
      const starts = Array.from(document.querySelectorAll('[data-segment-start]'));
      return starts.map((input) => {
        const index = input.getAttribute('data-segment-start');
        const endInput = document.querySelector(`[data-segment-end="${index}"]`);
        return {
          start_local: String(input.value || ''),
          end_local: String(endInput ? endInput.value : ''),
        };
      });
    }

    function currentEditorRow() {
      return hoursRowIndex.get(activeEditorKey) || null;
    }

    function buildEditorRequestPayload() {
      const row = currentEditorRow();
      if (!row) {
        throw new Error('Pick a workday to edit first.');
      }
      return {
        user_key: row.user_key,
        session_date: row.session_date,
        edited_by: document.getElementById('editor-edited-by').value || '',
        reason: document.getElementById('editor-reason').value || '',
        segments: currentEditorSegments(),
      };
    }

    async function previewEditorChanges() {
      try {
        const result = await postEditorJson('/api/edit-preview', buildEditorRequestPayload());
        latestEditorPreview = result;
        renderEditorPreview(result, false);
        setEditorFeedback(result.warnings && result.warnings.length ? result.warnings.join(' ') : 'Preview ready.');
      } catch (error) {
        setEditorFeedback(error.message || 'Preview failed.', true);
      }
    }

    async function applyEditorChanges() {
      try {
        const result = await postEditorJson('/api/edit-apply', buildEditorRequestPayload());
        latestEditorPreview = result;
        renderEditorPreview(result, true);
        setEditorFeedback('Saved manual time edit and rebuilt the local hours reports.');
        await refreshDashboardData();
        const refreshedRow = hoursRowIndex.get(activeEditorKey);
        if (refreshedRow) {
          openEditorForRow(activeEditorKey);
          const latest = refreshedRow.latest_manual_edit;
          if (latest) {
            setEditorFeedback(`Saved. Latest edit by ${latest.edited_by || 'unknown'} at ${formatDateTime(latest.edited_at)}.`);
          }
        }
      } catch (error) {
        setEditorFeedback(error.message || 'Save failed.', true);
      }
    }

    async function refreshDashboardData() {
      if (!editorMode) return;
      const response = await fetch(`${editorApiBase}/api/dashboard-data`, { cache: 'no-store' });
      if (!response.ok) {
        throw new Error(`Failed to refresh dashboard data (${response.status}).`);
      }
      const nextPayload = await response.json();
      hydratePayload(nextPayload, { preserveFilters: true });
      render();
    }

    async function postEditorJson(path, body) {
      const response = await fetch(`${editorApiBase}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(String(payload.error || `Request failed with status ${response.status}.`));
      }
      return payload;
    }

    function renderEditorPreview(result, saved) {
      const target = document.getElementById('editor-preview-output');
      if (!target) return;
      const warnings = Array.isArray(result.warnings) && result.warnings.length
        ? `<div><span class="label">Warnings</span><div class="chip-list">${result.warnings.map((warning) => `<span class="chip warning">${escapeHtml(warning)}</span>`).join('')}</div></div>`
        : '';
      target.innerHTML = `<div class="key-grid"><div><span class="label">${saved ? 'Saved clocked-in total' : 'Preview clocked-in total'}</span>${escapeHtml(result.after.clocked_in_total_human || '0m')}</div><div><span class="label">${saved ? 'Saved task-tracked total' : 'Preview task-tracked total'}</span>${escapeHtml(result.after.task_tracked_total_human || '0m')}</div><div><span class="label">Before clocked-in total</span>${escapeHtml(result.before.clocked_in_total_human || '0m')}</div><div><span class="label">Before task-tracked total</span>${escapeHtml(result.before.task_tracked_total_human || '0m')}</div></div>${warnings}`;
    }

    function setEditorFeedback(message, isError) {
      const target = document.getElementById('editor-feedback');
      if (!target) return;
      target.textContent = String(message || '');
      target.classList.toggle('danger', Boolean(isError));
      if (!isError) {
        target.classList.remove('warning');
      }
    }
  </script>
</body>
</html>
"""


def _augment_hours_rows_for_editor(
    hours_rows: list[dict[str, Any]],
    *,
    runtime: Any,
    reference_now: datetime,
) -> None:
    for row in hours_rows:
        user_key = str(row.get("user_key") or "").strip()
        session_date = str(row.get("session_date") or "").strip()
        row["row_key"] = f"{user_key}::{session_date}"
        row["editable"] = False
        row["edit_block_reason"] = "User profile is unavailable for this row."
        row["work_segments"] = []
        row["manual_edit_count"] = 0
        row["latest_manual_edit"] = None
        row["manual_edit_log_path"] = ""
        row["manual_edit_log_uri"] = ""
        if not user_key or not session_date:
            continue
        user = runtime.resolve_user_profile(user_key)
        if user is None:
            continue
        timezone_name = runtime.resolve_user_timezone_name(user)
        row["timezone"] = str(row.get("timezone") or timezone_name)
        session = runtime._load_session_for_manual_time_edit(user, session_date)
        row["work_segments"] = runtime._serialize_work_segments_for_editor(
            session.work_segments,
            timezone_name=timezone_name,
        )
        current_workday = runtime.resolve_user_workday_date(user, reference_now)
        if session_date < current_workday:
            row["editable"] = True
            row["edit_block_reason"] = ""
        else:
            row["edit_block_reason"] = (
                f"Only past workdays can be edited. The current effective workday is {current_workday}."
            )
        manual_edits = session.metadata.get("manual_time_edits")
        if isinstance(manual_edits, list):
            row["manual_edit_count"] = len(manual_edits)
        latest_manual_edit = session.metadata.get("latest_manual_time_edit")
        if isinstance(latest_manual_edit, dict):
            row["latest_manual_edit"] = latest_manual_edit
        manual_log_path = runtime._manual_time_edits_path(user, session_date)
        if manual_log_path is not None:
            row["manual_edit_log_path"] = str(manual_log_path.resolve())
            row["manual_edit_log_uri"] = _to_file_uri(manual_log_path)


def _collect_interns(hours_rows: list[dict[str, Any]], audit_runs: list[dict[str, Any]]) -> list[dict[str, str]]:
    interns: dict[str, str] = {}
    for row in hours_rows:
        user_key = str(row.get("user_key") or "").strip()
        if user_key:
            interns[user_key] = str(row.get("display_name") or user_key)
    for run in audit_runs:
        for row in run.get("rows", []):
            user_key = str(row.get("user_key") or "").strip()
            if user_key and user_key not in interns:
                interns[user_key] = str(row.get("display_name") or user_key)
    return [
        {"user_key": user_key, "display_name": interns[user_key]}
        for user_key in sorted(interns, key=lambda key: interns[key].lower())
    ]


def _augment_hours_rows_with_latest_audit(
    hours_rows: list[dict[str, Any]],
    audit_runs: list[dict[str, Any]],
) -> None:
    latest_audit_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for run in audit_runs:
        run_id = str(run.get("run_id") or "")
        for row in run.get("rows", []):
            user_key = str(row.get("user_key") or "").strip()
            session_date = str(row.get("session_date") or "").strip()
            if not user_key or not session_date:
                continue
            latest_audit_by_key.setdefault(
                (user_key, session_date),
                {
                    "run_id": run_id,
                    "confidence": str(row.get("confidence") or ""),
                    "changed": bool(row.get("changed")),
                    "warning_count": len(row.get("warnings") or []),
                },
            )
    for row in hours_rows:
        user_key = str(row.get("user_key") or "").strip()
        session_date = str(row.get("session_date") or "").strip()
        latest_audit = latest_audit_by_key.get((user_key, session_date))
        row["latest_audit_run_id"] = ""
        row["latest_audit_confidence"] = ""
        row["latest_audit_warning_count"] = 0
        row["latest_audit_changed"] = False
        if latest_audit is None:
            continue
        row["latest_audit_run_id"] = str(latest_audit.get("run_id") or "")
        row["latest_audit_confidence"] = str(latest_audit.get("confidence") or "")
        row["latest_audit_warning_count"] = int(latest_audit.get("warning_count") or 0)
        row["latest_audit_changed"] = bool(latest_audit.get("changed"))
        _merge_review_with_latest_audit(row, latest_audit)


def _merge_review_with_latest_audit(row: dict[str, Any], latest_audit: dict[str, Any]) -> None:
    reasons = [str(item) for item in row.get("review_reasons") or [] if str(item).strip()]
    review_status = str(row.get("review_status") or "likely_correct")
    run_id = str(latest_audit.get("run_id") or "")
    confidence = str(latest_audit.get("confidence") or "")
    warning_count = int(latest_audit.get("warning_count") or 0)

    def add_reason(message: str) -> None:
        if message not in reasons:
            reasons.append(message)

    if confidence == "unresolved":
        review_status = _promote_review_status(review_status, "likely_wrong")
        add_reason(f"Latest backfill audit{f' ({run_id})' if run_id else ''} could not resolve reliable times for this day.")
    elif confidence == "low":
        review_status = _promote_review_status(review_status, "needs_review")
        add_reason(f"Latest backfill audit{f' ({run_id})' if run_id else ''} marked this day low confidence.")
    elif confidence == "medium":
        add_reason(f"Latest backfill audit{f' ({run_id})' if run_id else ''} marked this day medium confidence.")
    elif confidence == "high":
        add_reason(f"Latest backfill audit{f' ({run_id})' if run_id else ''} marked this day high confidence.")

    if warning_count > 0:
        review_status = _promote_review_status(review_status, "needs_review")
        add_reason(
            f"Latest backfill audit{f' ({run_id})' if run_id else ''} recorded {warning_count} warning(s)."
        )

    row["review_status"] = review_status
    row["review_reasons"] = reasons
    if reasons:
        row["review_summary"] = reasons[0]


def _promote_review_status(current: str, target: str) -> str:
    order = {
        "likely_correct": 0,
        "needs_review": 1,
        "likely_wrong": 2,
    }
    if current == "in_progress":
        return current
    return target if order.get(target, 0) > order.get(current, 0) else current


def _load_hours_rows(report_path: Path) -> list[dict[str, Any]]:
    if not report_path.exists():
        return []
    _raise_csv_field_limit()
    rows: list[dict[str, Any]] = []
    with report_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            clocked_seconds = _to_int(row.get("clocked_in_total_seconds"))
            unpaid_lunch_seconds = _to_int(row.get("unpaid_lunch_deducted_seconds"))
            gross_seconds = _to_int(row.get("gross_clocked_in_total_seconds")) or clocked_seconds + unpaid_lunch_seconds
            loaded_row = {
                "user_key": str(row.get("user_key") or ""),
                "display_name": str(row.get("display_name") or row.get("user_key") or ""),
                "timezone": str(row.get("timezone") or ""),
                "session_date": str(row.get("session_date") or ""),
                "gross_clocked_in_total_seconds": gross_seconds,
                "gross_clocked_in_total_human": str(row.get("gross_clocked_in_total_human") or _format_duration(gross_seconds)),
                "unpaid_lunch_deducted_seconds": unpaid_lunch_seconds,
                "unpaid_lunch_deducted_human": str(row.get("unpaid_lunch_deducted_human") or _format_duration(unpaid_lunch_seconds)),
                "clocked_in_total_seconds": clocked_seconds,
                "clocked_in_total_human": str(row.get("clocked_in_total_human") or "0m"),
                "task_tracked_total_seconds": _to_int(row.get("task_tracked_total_seconds")),
                "task_tracked_total_human": str(row.get("task_tracked_total_human") or "0m"),
                "work_segment_count": _to_int(row.get("work_segment_count")),
                "has_open_work_segment": _to_bool(row.get("has_open_work_segment")),
                "active_task_timer_running": _to_bool(row.get("active_task_timer_running")),
                "time_by_task": _load_json_value(row.get("time_by_task_json"), []),
                "session_path": str(row.get("session_path") or ""),
                "session_uri": _to_file_uri(row.get("session_path")),
                "manual_edit_count": _to_int(row.get("manual_edit_count")),
                "latest_manual_edit": _load_json_value(row.get("latest_manual_edit_json"), None),
                "retro_backfill_confidence": str(row.get("retro_backfill_confidence") or ""),
                "retro_backfill_warning_count": _to_int(row.get("retro_backfill_warning_count")),
            }
            loaded_row.update(_load_review_fields(row, loaded_row))
            if not isinstance(loaded_row.get("latest_manual_edit"), dict):
                loaded_row["latest_manual_edit"] = None
            rows.append(loaded_row)
    return rows


def _load_review_fields(raw_row: dict[str, Any], loaded_row: dict[str, Any]) -> dict[str, Any]:
    fallback = _fallback_review_fields(loaded_row)
    review_status = str(raw_row.get("review_status") or fallback["review_status"] or "likely_correct")
    review_reasons = _load_json_value(raw_row.get("review_reasons_json"), fallback["review_reasons"])
    if not isinstance(review_reasons, list):
        review_reasons = list(fallback["review_reasons"])
    review_summary = str(raw_row.get("review_summary") or (review_reasons[0] if review_reasons else fallback["review_summary"]))
    review_gap_seconds = _to_int(raw_row.get("review_gap_seconds")) or int(fallback["review_gap_seconds"])
    review_gap_human = str(raw_row.get("review_gap_human") or _format_duration(review_gap_seconds))
    return {
        "review_status": review_status,
        "review_summary": review_summary,
        "review_reasons": [str(item) for item in review_reasons if str(item).strip()],
        "review_gap_seconds": review_gap_seconds,
        "review_gap_human": review_gap_human,
    }


def _fallback_review_fields(row: dict[str, Any]) -> dict[str, Any]:
    clocked_seconds = _to_int(row.get("clocked_in_total_seconds"))
    task_seconds = _to_int(row.get("task_tracked_total_seconds"))
    work_segment_count = _to_int(row.get("work_segment_count"))
    has_open_work_segment = _to_bool(row.get("has_open_work_segment"))
    active_task_timer_running = _to_bool(row.get("active_task_timer_running"))
    retro_confidence = str(row.get("retro_backfill_confidence") or "")
    retro_warning_count = _to_int(row.get("retro_backfill_warning_count"))
    manual_edit_count = _to_int(row.get("manual_edit_count"))
    gap_seconds = abs(clocked_seconds - task_seconds)
    severity = 0
    reasons: list[str] = []

    def add_reason(level: int, message: str) -> None:
        nonlocal severity
        severity = max(severity, level)
        if message not in reasons:
            reasons.append(message)

    if has_open_work_segment:
        add_reason(2, "This day still shows an open work segment.")
    if active_task_timer_running:
        add_reason(2, "This day still shows an active task timer.")
    if clocked_seconds > 0 and work_segment_count <= 0:
        add_reason(2, "Clocked-in time exists, but no work segments were stored.")
    if task_seconds - clocked_seconds > 5 * 60:
        add_reason(2, f"Task-tracked time exceeds clocked-in time by {_format_duration(task_seconds - clocked_seconds)}.")
    elif clocked_seconds >= 4 * 60 * 60 and task_seconds == 0:
        add_reason(1, f"No task-tracked time was recorded for a {_format_duration(clocked_seconds)} day.")
    elif clocked_seconds - task_seconds >= 2 * 60 * 60 and task_seconds > 0:
        add_reason(1, f"Clocked-in time exceeds task-tracked time by {_format_duration(clocked_seconds - task_seconds)}.")

    if retro_confidence == "unresolved":
        add_reason(2, "Retro backfill confidence is unresolved for this day.")
    elif retro_confidence == "low":
        add_reason(1, "Retro backfill confidence is low for this day.")
    if retro_warning_count > 0:
        add_reason(1, f"Retro backfill recorded {retro_warning_count} warning(s) for this day.")
    if manual_edit_count > 1:
        add_reason(1, f"This day has been manually corrected {manual_edit_count} times.")

    review_status = "likely_correct"
    if severity >= 2:
        review_status = "likely_wrong"
    elif severity == 1:
        review_status = "needs_review"

    summary = reasons[0] if reasons else "No suspicious timing issues were detected for this day."
    if not reasons and manual_edit_count == 1:
        reasons.append("This day has one manual correction on record.")
        summary = reasons[0]
    return {
        "review_status": review_status,
        "review_summary": summary,
        "review_reasons": reasons,
        "review_gap_seconds": gap_seconds,
    }


def _format_duration(total_seconds: int) -> str:
    if total_seconds <= 0:
        return "0m"
    minutes = total_seconds // 60
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _load_audit_runs(
    retro_root: Path,
    sessions_by_key: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    if not retro_root.exists():
        return []
    runs: list[dict[str, Any]] = []
    for run_dir in sorted((path for path in retro_root.iterdir() if path.is_dir()), key=lambda path: path.name, reverse=True):
        audit_path = run_dir / "audit.csv"
        if not audit_path.exists():
            continue
        summary = _load_json_file(run_dir / "summary.json")
        rows: list[dict[str, Any]] = []
        _raise_csv_field_limit()
        with audit_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                user_key = str(row.get("user_key") or "")
                session_date = str(row.get("session_date") or "")
                explanation_path = str(row.get("explanation_path") or "")
                explanation = _load_json_file(Path(explanation_path)) if explanation_path else None
                session_row = sessions_by_key.get((user_key, session_date), {})
                display_name = str(
                    (explanation or {}).get("display_name")
                    or session_row.get("display_name")
                    or user_key
                )
                session_path = str(session_row.get("session_path") or "")
                rows.append(
                    {
                        "user_key": user_key,
                        "display_name": display_name,
                        "session_date": session_date,
                        "changed": _to_bool(row.get("changed")),
                        "confidence": str(row.get("confidence") or ""),
                        "clocked_in_before_seconds": _to_int(row.get("clocked_in_before_seconds")),
                        "clocked_in_after_seconds": _to_int(row.get("clocked_in_after_seconds")),
                        "task_tracked_before_seconds": _to_int(row.get("task_tracked_before_seconds")),
                        "task_tracked_after_seconds": _to_int(row.get("task_tracked_after_seconds")),
                        "clock_in_source": str(row.get("clock_in_source") or ""),
                        "clock_out_source": str(row.get("clock_out_source") or ""),
                        "task_time_source": str(row.get("task_time_source") or ""),
                        "explanation_path": explanation_path,
                        "explanation_uri": _to_file_uri(explanation_path),
                        "session_path": session_path,
                        "session_uri": _to_file_uri(session_path),
                        "explanation": str((explanation or {}).get("explanation") or ""),
                        "warnings": _coerce_list((explanation or {}).get("warnings")),
                        "clock_in_at": (explanation or {}).get("clock_in_at"),
                        "clock_out_at": (explanation or {}).get("clock_out_at"),
                        "lunch_windows": _coerce_list((explanation or {}).get("lunch_windows")),
                        "task_windows": _coerce_list((explanation or {}).get("task_windows")),
                        "sources_used": _coerce_list((explanation or {}).get("sources_used")),
                        "detail_load_error": "" if explanation is not None else "Explanation file is missing or unreadable.",
                    }
                )
        run_summary = summary or {}
        run_id = str(run_summary.get("run_id") or run_dir.name)
        apply = bool(run_summary.get("apply")) if "apply" in run_summary else False
        runs.append(
            {
                "run_id": run_id,
                "label": f"{run_id} - {'Apply' if apply else 'Dry run'}",
                "apply": apply,
                "processed_days": _to_int(run_summary.get("processed_days")),
                "changed_days": _to_int(run_summary.get("changed_days")),
                "unchanged_days": _to_int(run_summary.get("unchanged_days")),
                "unresolved_days": _to_int(run_summary.get("unresolved_days")),
                "warnings_count": _to_int(run_summary.get("warnings_count")),
                "audit_root": str(run_summary.get("audit_root") or run_dir.resolve()),
                "audit_csv_path": str(run_summary.get("audit_csv_path") or audit_path.resolve()),
                "rows": rows,
            }
        )
    return runs


def _load_json_file(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _load_json_value(raw: Any, fallback: Any) -> Any:
    if not isinstance(raw, str) or not raw.strip():
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


def _coerce_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes"}


def _to_file_uri(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return Path(text).resolve().as_uri()
    except ValueError:
        return ""


def _escape_html(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )
