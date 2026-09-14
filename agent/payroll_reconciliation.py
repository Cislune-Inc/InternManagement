"""Live, private payroll review. Drafts never mutate attendance or Gusto."""
from __future__ import annotations

import hashlib
import html
import json
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import SessionState
from .slack_timekeeping import paid_seconds, timestamp

ZONE = ZoneInfo("America/Los_Angeles")


def period(value: str, now: datetime) -> tuple[date, date]:
    today = now.astimezone(ZONE).date()
    end = date.fromisoformat(value) if value else today - timedelta(days=today.weekday() + 1)
    if end.weekday() != 6 or end >= today:
        raise ValueError("Choose a completed Sunday; current-day time stays live and outside this review.")
    return end - timedelta(days=6), end


@contextmanager
def _connect(runtime):
    path = runtime._storage_root_path() / "dashboard" / "payroll" / "reconciliation.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS drafts (id INTEGER PRIMARY KEY, week TEXT, user_key TEXT, day TEXT, fingerprint TEXT, body TEXT, saved_at TEXT)")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def build(runtime, week: str = "", now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    start, end = period(week, now)
    week_start = datetime.combine(start, time(), ZONE)
    week_end = datetime.combine(end + timedelta(days=1), time(), ZONE)
    with runtime.state_store._connect() as conn:
        raw = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM sessions ORDER BY user_key,session_date")]
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", ("slack_clock_reports",)).fetchone()
        reports = [dict(r) for r in conn.execute("SELECT id,user_key,reported_at,text,status FROM slack_clock_reports WHERE status=?", ("pending",))] if exists else []
    grouped = {}
    for payload in raw:
        # All ledger identities are considered, including no-longer-active workers.
        grouped.setdefault(payload["user_key"], []).append(payload)
    cohort = getattr(runtime.config.slack, "work_intake_beta_slack_user_ids", [])
    keys = set(grouped) | {r["user_key"] for r in reports} | {u.user_key for u in runtime.roster_by_key.values() if u.slack_user_id in cohort}
    with _connect(runtime) as conn:
        saved = {(r["user_key"], r["day"]): dict(r) for r in conn.execute("SELECT * FROM drafts WHERE week=? ORDER BY id", (end.isoformat(),))}
    workers = []
    for key in sorted(keys):
        user = runtime.roster_by_key.get(key)
        if user is None and hasattr(runtime, "resolve_user_profile"):
            user = runtime.resolve_user_profile(key)
        payloads = grouped.get(key, [])
        relevant = []
        for p in payloads:
            segments = p.get("work_segments") or [{"clocked_in_at": p.get("clocked_in_at"), "clocked_out_at": p.get("clocked_out_at")}]
            intersects = any(timestamp(s.get("clocked_in_at")) and timestamp(s["clocked_in_at"]) < week_end and (timestamp(s.get("clocked_out_at")) or week_end) > week_start and (s.get("clocked_out_at") or timestamp(s["clocked_in_at"]) >= week_start) for s in segments)
            if start.isoformat() <= p["session_date"] <= end.isoformat() or intersects:
                relevant.append(p)
        worker_reports = [r for r in reports if r["user_key"] == key]
        if not relevant and not worker_reports and not (user and user.slack_user_id in cohort):
            continue
        days = []
        for offset in range(7):
            day = start + timedelta(days=offset)
            lo = datetime.combine(day, time(), ZONE)
            hi = lo + timedelta(days=1)
            sessions, evidence, issues = [], [], []
            for p in relevant:
                s = SessionState(**p)
                segs = s.work_segments or [{"clocked_in_at": s.clocked_in_at, "clocked_out_at": s.clocked_out_at}]
                closed = []
                for seg in segs:
                    a, b = timestamp(seg.get("clocked_in_at")), timestamp(seg.get("clocked_out_at"))
                    if a and a < hi and (b or hi) > lo:
                        evidence.append({"kind": "Work", "start": a.isoformat(), "end": b.isoformat() if b else None, "session_date": s.session_date})
                        if b:
                            closed.append(seg)
                        else:
                            issues.append("Open interval: actual end needed; unknown tail excluded from recorded hours")
                if closed:
                    s.work_segments = closed
                    sessions.append(s)
                if any(e["session_date"] == s.session_date for e in evidence):
                    reason = s.metadata.get("slack_clock_stop_reason")
                    if reason and reason not in {"manual", "worker_clock_out", "owner_confirmed_end"}:
                        issues.append("Clock stop: " + str(reason).replace("_", " ") + "; confirm actual end")
                    for field, label in (("lunch_windows", "Lunch"), ("paid_rest_windows", "Paid rest")):
                        for w in s.metadata.get(field) or []:
                            a, b = timestamp(w.get("started_at")), timestamp(w.get("ended_at"))
                            if a and a < hi and (b or hi) > lo:
                                evidence.append({"kind": label + (" — paid pending review" if w.get("paid_pending_review") else ""), "start": a.isoformat(), "end": b.isoformat() if b else None, "session_date": s.session_date})
                                if not b or w.get("paid_pending_review"):
                                    issues.append(label + " needs timing review")
                    if s.metadata.get("slack_clock_legacy_unresolved"):
                        issues.append("Preserved unresolved historical shift")
            seconds = paid_seconds(sessions, now, lo, hi)
            if seconds >= 5 * 3600 and user and user.meal_tracking_required and not any(e["kind"].startswith("Lunch") for e in evidence):
                issues.append("No lunch record for this day; check actual meal timing")
            if seconds >= 3.5 * 3600 and user and user.worker_type != "admin" and not any(e["kind"].startswith("Paid rest") for e in evidence):
                issues.append("No paid-rest record; check whether a break entry is missing")
            if seconds > 8 * 3600:
                issues.append("Over 8 recorded hours; review applicable overtime classification")
            fingerprint = hashlib.sha256(json.dumps({"sources": relevant, "reports": worker_reports, "day": day.isoformat()}, sort_keys=True).encode()).hexdigest()
            draft = saved.get((key, day.isoformat()))
            current = draft and draft["fingerprint"] == fingerprint
            days.append({"day": day.isoformat(), "seconds": seconds, "hours": round(seconds / 3600, 4), "evidence": evidence, "issues": sorted(set(issues)), "fingerprint": fingerprint,
                         "draft": json.loads(draft["body"]) if draft else None, "draft_current": bool(current), "saved_at": draft["saved_at"] if draft else None})
        total = sum(d["seconds"] for d in days)
        workers.append({"user_key": key, "name": user.display_name if user else key, "compensation": user.compensation_plan if user else "needs_review", "mapped": bool(user and user.gusto_entity_uuid), "seconds": total, "hours": round(total / 3600, 4), "days": days, "reports": worker_reports,
                        "issues": (["Weekly recorded hours exceed 40; review classification"] if total > 144000 else []) + (["No DP hours recorded: check earlier Gusto/cutover records"] if not total else [])})
    return {"week_start": start.isoformat(), "week_ending": end.isoformat(), "timezone": ZONE.key, "refreshed_at": now.isoformat(), "workers": workers, "gusto_status": "Not yet verified. Connector returned no timesheets; this does not mean zero hours.", "source": "Live DP SQLite sessions and pending actual-hours reports. Closed work intervals are unioned within each Pacific calendar day; overlapping recorded unpaid meals are deducted; paid rests remain included. Open tails are excluded, not assumed to be zero actual work. Draft reconciliation never changes DP or Gusto."}


def save(runtime, payload: dict) -> dict:
    data = build(runtime, str(payload.get("week") or ""))
    worker = next((w for w in data["workers"] if w["user_key"] == payload.get("user_key")), None)
    row = next((d for d in worker["days"] if d["day"] == payload.get("day")), None) if worker else None
    if not row or row["fingerprint"] != payload.get("fingerprint"):
        raise ValueError("Source time changed or row is unavailable. Refresh and review before saving.")
    body = {}
    for field in ("gusto_hours", "target_hours"):
        value = payload.get(field)
        value = None if value in (None, "") else float(value)
        if value is not None and (not math.isfinite(value) or not 0 <= value <= 24):
            raise ValueError("Daily hours must be between 0 and 24; leave unknown values blank.")
        body[field] = value
    body["note"] = str(payload.get("note") or "").strip()[:4000]
    if not body["note"]:
        raise ValueError("Add the source/evidence and what you resolved.")
    body["status"] = "draft"  # This is not approval, a punch correction, or a payroll transaction.
    with _connect(runtime) as conn:
        conn.execute("INSERT INTO drafts(week,user_key,day,fingerprint,body,saved_at) VALUES(?,?,?,?,?,?)", (data["week_ending"], worker["user_key"], row["day"], row["fingerprint"], json.dumps(body), datetime.now(timezone.utc).isoformat()))
    return {"saved": True, "message": "Reconciliation draft saved. DP punches and Gusto are unchanged."}


def render(data: dict) -> str:
    encoded = json.dumps(data).replace("<", "\\u003c")
    return r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>DP · Payroll reconciliation</title>
<style>*{box-sizing:border-box}body{margin:0;background:#f4f6f7;color:#152c38;font:15px/1.5 system-ui}main{max-width:1280px;margin:24px auto;padding:0 20px}h1{margin:0;font-size:27px}h2{font-size:19px}header,.panel{background:white;padding:20px;border:1px solid #d5dfe3;border-radius:12px;margin-bottom:16px}.toolbar{display:flex;gap:12px;flex-wrap:wrap;align-items:center}button,a{color:#06636a}button{padding:9px 13px;border:1px solid #9eb5bd;border-radius:6px;background:white;cursor:pointer}input,select,textarea{font:inherit;padding:9px;border:1px solid #9eb5bd;border-radius:5px}input[type=number]{width:115px}textarea{width:100%;min-height:80px}label{display:block}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:10px;border-bottom:1px solid #e0e6e9;vertical-align:top}.scroll{overflow:auto}.muted{color:#526875}.warning{background:#fff1d6;color:#654308;padding:10px;border-radius:6px}.metrics{display:flex;gap:28px;flex-wrap:wrap;margin:18px 0}.metrics strong{display:block;font-size:26px}.active{background:#06636a;color:white}.day{margin-top:14px;border-top:1px solid #d5dfe3;padding-top:12px}summary{cursor:pointer;font-weight:650;padding:8px 0}.split{display:grid;grid-template-columns:1fr 1fr;gap:18px}.notice{min-height:24px}a{font-weight:600}@media(max-width:720px){.split{grid-template-columns:1fr}main{padding:0 10px}th,td{padding:7px}.panel{padding:12px}}</style>
<main><header><div class="toolbar"><h1>Payroll reconciliation</h1><a href="/payroll">Legacy export</a><a href="https://app.gusto.com/" target="_blank" rel="noreferrer">Open Gusto ↗</a></div><p id="period"></p><div class="toolbar"><label>Week ending Sunday <input type="date" id="week"></label><button id="load">Load week</button><button id="export">Download review CSV</button></div><p class="muted">Review only · no payroll submission · today’s active shifts are excluded.</p></header>
<section class="panel"><div id="metrics" class="metrics"></div><div class="warning" id="gusto"></div><p>Start with automatic stops and missing intervals. Then compare each day with Gusto, resolving overlaps—not adding both totals. Save the reconciled target and evidence as a draft for transfer.</p><label>Worker <select id="worker"><option value="">All workers</option></select></label><div class="scroll"><table><thead><tr><th>Worker</th><th>DP recorded</th><th>Days to inspect</th><th>Gusto verified</th><th>Draft target</th><th>Classification on file</th></tr></thead><tbody id="overview"></tbody></table></div></section><section id="detail"></section><details class="panel"><summary>Sources, calculation and coverage</summary><p id="method"></p><p id="fresh"></p><p>Gusto figures entered here are manager-observed values, not a live Gusto sync. Blank means unknown. Target minus Gusto is a comparison, not an instruction to add a duplicate shift. Regular/overtime/double-time and any premiums require final payroll review. Pending claims are shown regardless of receipt date because their work date must be established. John Cook and ASA remain in their separate workflows.</p></details></main>
<script>let data=DATA;const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const hours=s=>`${Math.floor(s/3600)}h ${Math.floor(s%3600/60)}m`;const num=v=>v==null?'Not entered':Number(v).toFixed(2)+' h';const local=v=>v?new Date(v).toLocaleString('en-US',{timeZone:data.timezone,month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}):'End unknown';
function draw(){const selected=$('worker').value;const workers=data.workers.filter(w=>!selected||w.user_key===selected);$('period').textContent=`${data.week_start} – ${data.week_ending} · Pacific time`;$('week').value=data.week_ending;$('gusto').textContent=data.gusto_status;$('method').textContent=data.source;$('fresh').textContent='Ledger read: '+local(data.refreshed_at);$('metrics').innerHTML=`<div><strong>${workers.length}</strong>Workers shown</div><div><strong>${hours(workers.reduce((n,w)=>n+w.seconds,0))}</strong>Recorded work + paid rest</div><div><strong>${workers.reduce((n,w)=>n+w.days.filter(d=>d.issues.length).length,0)}</strong>Days with timing flags</div>`;
$('overview').innerHTML=workers.map(w=>{const current=w.days.filter(d=>d.draft_current);const allG=current.filter(d=>d.draft.gusto_hours!=null);const allT=current.filter(d=>d.draft.target_hours!=null);return `<tr><td><button data-person="${esc(w.user_key)}">${esc(w.name)}</button></td><td>${hours(w.seconds)}<br><small>${w.hours.toFixed(4)} decimal</small></td><td>${w.days.filter(d=>d.issues.length).length} flagged · ${w.reports.length} claims</td><td>${allG.length===7?num(allG.reduce((n,d)=>n+d.draft.gusto_hours,0)):allG.length+'/7 days entered'}</td><td>${allT.length===7?num(allT.reduce((n,d)=>n+d.draft.target_hours,0)):allT.length+'/7 days entered'}</td><td>${esc(w.compensation)}${w.mapped?'':' · mapping unverified'}</td></tr>`}).join('');
$('detail').innerHTML=workers.map(w=>`<section class="panel"><h2>${esc(w.name)} · ${hours(w.seconds)}</h2>${w.issues.map(i=>`<p class="warning">${esc(i)}</p>`).join('')}${w.reports.map(r=>`<p class="warning">Pending claim (${esc(r.reported_at)}): ${esc(r.text)}</p>`).join('')}${w.days.map(d=>`<details class="day" ${d.issues.length?'open':''}><summary>${d.day} · ${hours(d.seconds)} · ${d.issues.length?d.issues.length+' timing flags':'No detected timing flags'} ${d.draft?(d.draft_current?'· draft saved':'· source changed—review draft again'):''}</summary><div class="split"><div>${d.issues.map(i=>`<p class="warning">${esc(i)}</p>`).join('')}<table><thead><tr><th>Record</th><th>Start → end (PT)</th></tr></thead><tbody>${d.evidence.map(e=>`<tr><td>${esc(e.kind)}</td><td>${local(e.start)} → ${local(e.end)}</td></tr>`).join('')||'<tr><td colspan="2">No recorded intervals for this day. Check Gusto/cutover before treating as zero.</td></tr>'}</tbody></table></div><form data-key="${esc(w.user_key)}" data-day="${d.day}" data-fingerprint="${d.fingerprint}"><div class="toolbar"><label>Gusto observed hours<input name="gusto_hours" type="number" min="0" max="24" step="0.0001" value="${d.draft?.gusto_hours??''}"></label><label>Reconciled target hours<input name="target_hours" type="number" min="0" max="24" step="0.0001" value="${d.draft?.target_hours??''}"></label></div><label>Evidence / resolution<textarea name="note" required placeholder="Actual times confirmed by worker, Gusto source, duplicate removed from target, or question still pending">${esc(d.draft?.note??'')}</textarea></label><button>Save reconciliation draft</button><p class="notice" role="status"></p></form></div></details>`).join('')}</section>`).join('');
document.querySelectorAll('[data-person]').forEach(b=>b.onclick=()=>{if(!leave())return;$('worker').value=b.dataset.person;draw()});document.querySelectorAll('form[data-day]').forEach(f=>{f.oninput=()=>f.dataset.dirty='yes';f.onsubmit=async e=>{e.preventDefault();const status=f.querySelector('.notice');const button=f.querySelector('button');button.disabled=true;try{const body=Object.fromEntries(new FormData(f));Object.assign(body,{week:data.week_ending,user_key:f.dataset.key,day:f.dataset.day,fingerprint:f.dataset.fingerprint});const r=await fetch('/api/reconciliation/draft',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const result=await r.json();if(!r.ok)throw Error(result.error||'Save failed');const d=data.workers.find(w=>w.user_key===f.dataset.key).days.find(d=>d.day===f.dataset.day);d.draft={note:body.note,gusto_hours:body.gusto_hours===''?null:Number(body.gusto_hours),target_hours:body.target_hours===''?null:Number(body.target_hours)};d.draft_current=true;delete f.dataset.dirty;status.textContent=result.message+' Refresh the week to update the summary; CSV includes this saved draft.';}catch(err){status.textContent=err.message}finally{button.disabled=false}}});}
function dirty(){return !!document.querySelector('[data-dirty="yes"]')}function leave(){return !dirty()||confirm('Leave unsaved reconciliation notes?')}window.addEventListener('beforeunload',e=>{if(dirty()){e.preventDefault();e.returnValue=''}});document.addEventListener('change',e=>{if(e.target.id==='worker'&&!leave()){e.stopImmediatePropagation()}},true);
$('worker').innerHTML+ ='';
for(const w of data.workers){const option=document.createElement('option');option.value=w.user_key;option.textContent=w.name;$('worker').append(option)}$('worker').onchange=draw;$('load').onclick=()=>{location.href='/reconcile?week='+encodeURIComponent($('week').value)};$('export').onclick=()=>{const rows=[['worker','day','DP_recorded_hours','Gusto_observed_hours','draft_target_hours','draft_current','issues','note']];for(const w of data.workers.filter(w=>!$('worker').value||w.user_key===$('worker').value))for(const d of w.days)rows.push([w.name,d.day,d.hours,d.draft?.gusto_hours??'',d.draft?.target_hours??'',d.draft_current,d.issues.join('; '),d.draft?.note??'']);const csv=rows.map(r=>r.map(v=>'"'+String(v).replace(/^[=+@-]/,"'$&").replaceAll('"','""')+'"').join(',')).join('\r\n');const url=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));const a=document.createElement('a');a.href=url;a.download='DP-review-'+data.week_ending+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};draw();</script></html>'''.replace("$('worker').innerHTML+ ='';", "").replace("DATA;", encoded + ";", 1)
