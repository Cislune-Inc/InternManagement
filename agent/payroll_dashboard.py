from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import Any


_LATEST_PAYROLL_ROOT = Path("dashboard") / "payroll" / "latest"
_PAYROLL_FILES = {
    "payroll_review.csv",
    "project_labor.csv",
    "nasa_project_labor.csv",
    "project_summary.csv",
    "compliance_events.csv",
    "gusto_time_sheets.json",
    "summary.json",
    "workforce_identity_candidates.csv",
}


def build_payroll_dashboard_payload(storage_root: Path) -> dict[str, Any]:
    root = storage_root / _LATEST_PAYROLL_ROOT
    return {
        "summary": _read_json(root / "summary.json"),
        "payroll_rows": _read_csv(root / "payroll_review.csv"),
        "project_rows": _read_csv(root / "project_summary.csv"),
        "compliance_rows": _read_csv(root / "compliance_events.csv"),
        "available_files": sorted(
            filename
            for filename in _PAYROLL_FILES
            if resolve_payroll_download(storage_root, filename) is not None
        ),
    }


def resolve_payroll_download(storage_root: Path, filename: str) -> Path | None:
    if filename not in _PAYROLL_FILES:
        return None
    if filename == "workforce_identity_candidates.csv":
        path = storage_root / "dashboard" / "payroll" / filename
    else:
        path = storage_root / _LATEST_PAYROLL_ROOT / filename
    return path if path.exists() and path.is_file() else None


def render_payroll_dashboard_html(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    payroll_rows = payload.get("payroll_rows") if isinstance(payload.get("payroll_rows"), list) else []
    project_rows = payload.get("project_rows") if isinstance(payload.get("project_rows"), list) else []
    compliance_rows = (
        payload.get("compliance_rows")
        if isinstance(payload.get("compliance_rows"), list)
        else []
    )
    files = payload.get("available_files") if isinstance(payload.get("available_files"), list) else []
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Payroll and Project Labor Review</title>
  <style>
    :root {{ --ink:#1d2928; --paper:#f7f1e7; --card:#fffdf8; --line:#d8d0c3; --accent:#0d746e; --warn:#a9472b; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--ink); background:radial-gradient(circle at 10% 0,#dcece6 0,transparent 34%),var(--paper); font:15px/1.45 "Avenir Next","Gill Sans",sans-serif; }}
    main {{ width:min(1180px,94vw); margin:32px auto 72px; }}
    header {{ padding:28px; border:1px solid var(--line); border-radius:24px; background:rgba(255,253,248,.9); }}
    h1,h2 {{ font-family:"Iowan Old Style","Baskerville",serif; margin:0 0 8px; }}
    nav a,.download {{ color:var(--accent); margin-right:16px; font-weight:700; }}
    .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin:18px 0; }}
    .metric,.panel {{ background:var(--card); border:1px solid var(--line); border-radius:18px; padding:18px; }}
    .metric strong {{ display:block; font-size:1.7rem; }}
    .panel {{ margin-top:16px; overflow:auto; }}
    table {{ width:100%; border-collapse:collapse; min-width:720px; }}
    th,td {{ padding:10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
    th {{ font-size:.75rem; text-transform:uppercase; letter-spacing:.08em; }}
    .review {{ color:var(--warn); font-weight:700; }}
    button {{ border:0; border-radius:999px; padding:9px 14px; background:var(--accent); color:white; font-weight:700; cursor:pointer; }}
    button[disabled] {{ opacity:.45; cursor:not-allowed; }}
    .status {{ font-weight:700; white-space:nowrap; }}
    .status-clear,.status-resolved,.status-auto_resolved {{ color:var(--accent); }}
    .status-needs_review {{ color:var(--warn); }}
    dialog {{ width:min(560px,92vw); border:1px solid var(--line); border-radius:20px; padding:22px; background:var(--card); color:var(--ink); }}
    dialog::backdrop {{ background:rgba(15,30,28,.5); }}
    label {{ display:block; margin:12px 0; font-weight:700; }}
    input,textarea {{ width:100%; border:1px solid var(--line); border-radius:10px; padding:10px; font:inherit; }}
    input[type=checkbox] {{ width:auto; margin-right:8px; }}
    .actions {{ display:flex; gap:10px; justify-content:flex-end; margin-top:16px; }}
    .secondary {{ background:#e8e2d8; color:var(--ink); }}
    #review-result {{ min-height:1.4em; color:var(--warn); }}
    @media(max-width:650px) {{ main{{width:96vw;margin-top:12px}} header{{padding:20px}} }}
  </style>
</head>
<body><main>
  <header>
    <h1>Payroll and Project Labor Review</h1>
    <p>Approval-first weekly timecards, compensation-plan separation, project budget rollups, compliance events, and NASA-ready labor detail.</p>
    <nav><a href="/time">Time tracking</a><a href="/work">Work dashboard</a><a href="/payroll">Payroll</a><a href="/exceptions">Manager queue</a><a href="/health">System health</a></nav>
  </header>
  <section class="metrics">
    {_metric("Week ending", summary.get("week_ending") or "No export yet")}
    {_metric("Tracked hours", summary.get("tracked_hours") or 0)}
    {_metric("Hourly payroll hours", summary.get("hourly_payroll_hours") or 0)}
    {_metric("NASA stipend effort", summary.get("stipend_effort_hours") or 0)}
    {_metric("Unclassified hours", summary.get("unclassified_hours") or 0)}
    {_metric("Worker days", summary.get("worker_days") or 0)}
    {_metric("Needs review", summary.get("requires_review_days") or 0)}
    {_metric("Compliance events", summary.get("compliance_events") or 0)}
  </section>
  <section class="panel"><h2>Project Labor</h2>{_table(project_rows, ["project","labor_code","hours","budget_hours","remaining_budget_hours","estimated_labor_cost"])}</section>
  <section class="panel"><h2>Time and Compensation Review</h2><p>Everyone remains in tracked-time and project-labor reporting. Only workers explicitly classified as Cislune hourly can enter the Gusto bundle; NASA stipend effort remains separate. Resolutions reopen automatically if the underlying hours change.</p>{_payroll_review_table(payroll_rows)}</section>
  <section class="panel"><h2>Compliance</h2>{_table(compliance_rows, ["display_name","session_date","event_type","recorded_at","worked_hours"])}</section>
  <section class="panel"><h2>Downloads</h2><p>{''.join(_download(name) for name in files) or 'Run the Monday payroll export to create the first bundle.'}</p></section>
  <dialog id="review-dialog">
    <form id="review-form">
      <h2>Resolve payroll review</h2>
      <p id="review-day"></p>
      <input id="review-user-key" type="hidden">
      <input id="review-session-date" type="hidden">
      <label>Resolved by<input id="review-resolved-by" autocomplete="name" required></label>
      <label>Review note<textarea id="review-note" rows="4" required placeholder="What evidence did you check, and why is this acceptable?"></textarea></label>
      <label><input id="review-remember" type="checkbox">Remember this task-time variance limit for this person</label>
      <p id="review-learning-note">Learning is only available when task-time variance is the sole review reason.</p>
      <p id="review-result"></p>
      <div class="actions"><button class="secondary" type="button" id="review-cancel">Cancel</button><button type="submit">Resolve review</button></div>
    </form>
  </dialog>
</main>
<script>
const dialog = document.getElementById("review-dialog");
const form = document.getElementById("review-form");
document.querySelectorAll("[data-resolve-review]").forEach((button) => {{
  button.addEventListener("click", () => {{
    document.getElementById("review-user-key").value = button.dataset.userKey;
    document.getElementById("review-session-date").value = button.dataset.sessionDate;
    document.getElementById("review-day").textContent = `${{button.dataset.displayName}} · ${{button.dataset.sessionDate}} · ${{button.dataset.warnings}}`;
    const learn = document.getElementById("review-remember");
    learn.checked = false;
    learn.disabled = button.dataset.learnable !== "true";
    document.getElementById("review-result").textContent = "";
    dialog.showModal();
  }});
}});
document.getElementById("review-cancel").addEventListener("click", () => dialog.close());
form.addEventListener("submit", async (event) => {{
  event.preventDefault();
  const result = document.getElementById("review-result");
  result.textContent = "Saving review...";
  const response = await fetch("/api/payroll-review-resolve", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{
      user_key: document.getElementById("review-user-key").value,
      session_date: document.getElementById("review-session-date").value,
      resolved_by: document.getElementById("review-resolved-by").value,
      note: document.getElementById("review-note").value,
      remember_similar: document.getElementById("review-remember").checked
    }})
  }});
  const payload = await response.json();
  if (!response.ok) {{
    result.textContent = payload.error || "Unable to resolve this review.";
    return;
  }}
  window.location.reload();
}});
</script>
</body></html>"""


def _metric(label: str, value: Any) -> str:
    return (
        '<div class="metric"><span>'
        + html.escape(label)
        + "</span><strong>"
        + html.escape(str(value))
        + "</strong></div>"
    )


def _download(filename: str) -> str:
    safe = html.escape(filename)
    return f'<a class="download" href="/payroll/files/{safe}">{safe}</a>'


def _table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "<p>No rows are available for the latest completed export.</p>"
    header = "".join(f"<th>{html.escape(column.replace('_', ' ').title())}</th>" for column in columns)
    body = []
    for row in rows:
        cells = []
        for column in columns:
            value = str(row.get(column) or "")
            css = ' class="review"' if column == "requires_review" and value.lower() == "true" else ""
            cells.append(f"<td{css}>{html.escape(value)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><thead><tr>{header}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _payroll_review_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p>No rows are available for the latest completed export.</p>"
    columns = [
        "display_name",
        "compensation_plan",
        "session_date",
        "tracked_hours",
        "hourly_payroll_hours",
        "unpaid_meal_hours",
        "task_tracked_hours",
        "overtime_hours",
        "review_status",
        "warnings",
        "integration_notes",
    ]
    header = "".join(
        f"<th>{html.escape(column.replace('_', ' ').title())}</th>" for column in columns
    ) + "<th>Action</th>"
    body: list[str] = []
    for row in rows:
        status = str(row.get("review_status") or "clear")
        cells = []
        for column in columns:
            value = str(row.get(column) or "")
            css = (
                f' class="status status-{html.escape(status)}"'
                if column == "review_status"
                else ""
            )
            cells.append(f"<td{css}>{html.escape(value)}</td>")
        action = ""
        if status == "needs_review":
            codes = _decode_codes(row.get("review_codes"))
            action = (
                '<button type="button" data-resolve-review '
                f'data-user-key="{html.escape(str(row.get("user_key") or ""), quote=True)}" '
                f'data-session-date="{html.escape(str(row.get("session_date") or ""), quote=True)}" '
                f'data-display-name="{html.escape(str(row.get("display_name") or ""), quote=True)}" '
                f'data-warnings="{html.escape(str(row.get("warnings") or ""), quote=True)}" '
                f'data-learnable="{str(codes == ["task_time_variance"]).lower()}">'
                "Resolve</button>"
            )
        body.append("<tr>" + "".join(cells) + f"<td>{action}</td></tr>")
    return f"<table><thead><tr>{header}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _decode_codes(value: Any) -> list[str]:
    if isinstance(value, list):
        return sorted(str(item) for item in value if str(item))
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(decoded, list):
            return sorted(str(item) for item in decoded if str(item))
    return []


def _read_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []
