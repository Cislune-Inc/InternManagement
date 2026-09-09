"""LAN-only technical preview. No imports or access to the production runtime."""
from __future__ import annotations
import argparse
import ipaddress
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from .portfolio import render_portfolio

PROJECT_FIELDS = {'id','name','category','milestone','note','source'}
TASK_FIELDS = {'id','project','title','deps','resources','minimum','likely','downside','release','priority','status','approval','availability_confirmed','blocker','done_when','source','estimate_basis'}

def team_snapshot(source: dict) -> dict:
    if source.get('schema_version') != 1:
        raise ValueError('Unsupported snapshot')
    return { 'schema_version':1, 'revision':str(source['revision']), 'as_of':str(source['as_of']),
        'scenario_start':str(source['scenario_start']), 'onsite_preview':True,
        'projects':[{k:v for k,v in p.items() if k in PROJECT_FIELDS} for p in source['projects']],
        'tasks':[{k:v for k,v in t.items() if k in TASK_FIELDS} for t in source['tasks']] }

class FeedbackStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS feedback (id TEXT PRIMARY KEY, created_at TEXT, name TEXT, project TEXT, message TEXT, revision TEXT)')
        path.chmod(0o600)

    def save(self, payload: dict, revision: str) -> str:
        values = {}
        for name, limit in [('id',80),('name',80),('project',120),('message',4000)]:
            value = payload.get(name,'')
            if not isinstance(value,str) or len(value)>limit:
                raise ValueError('Invalid feedback field')
            values[name]=value.strip()
        if not values['id'] or not values['message']:
            raise ValueError('Please enter feedback.')
        with sqlite3.connect(self.path) as db:
            if db.execute('SELECT count(*) FROM feedback').fetchone()[0]>=10000:
                raise ValueError('Feedback queue is full; tell Erik directly.')
            db.execute('INSERT OR IGNORE INTO feedback VALUES (?,?,?,?,?,?)',
                       (values['id'],datetime.now(timezone.utc).isoformat(),values['name'],values['project'],values['message'],revision))
        return values['id']

def make_handler(plan: dict, store: FeedbackStore, *, host: str, port: int, network: str):
    net = ipaddress.ip_network(network)
    allowed_hosts = {f'{host}:{port}', f'cmacmini.local:{port}'}
    csrf = secrets.token_urlsafe(32)
    page = render_portfolio(plan).replace('Owner preview · company overhead','On-site team test · company overhead')
    page = page.replace('Planning workbench<br>', 'Planning workbench<br>')
    page = page.replace('<div class="notice">', '<div class="notice"><strong>Team preview — September 8 snapshot.</strong> ')
    panel = '''<section class="panel" style="margin-top:20px"><h2>Tell us what would make this useful</h2><p>Try a project, its dependency view, and an estimate change. What is confusing, missing, or wrong? Feedback goes to Erik for review; your name is optional and self-reported.</p><form id="feedback-form"><label>Your name (optional) <input name="name" maxlength="80" autocomplete="name"></label><label> Project <input name="project" maxlength="120" placeholder="e.g. Bagworm"></label><label style="display:block;margin:10px 0">Feedback<textarea name="message" maxlength="4000" required style="display:block;width:100%;min-height:90px" placeholder="What did you try, and what should work differently?"></textarea></label><button type="submit">Send feedback</button> <span id="feedback-status" role="status"></span></form></section>'''
    page = page.replace('<footer id="footer">', panel+'<footer id="footer">')
    script = '''<script>(()=>{let pendingId=null;const f=document.getElementById('feedback-form'),status=document.getElementById('feedback-status');f.addEventListener('input',()=>{pendingId=null;});f.onsubmit=async(e)=>{e.preventDefault();const b=f.querySelector('button');b.disabled=true;const fields=Object.fromEntries(new FormData(f));pendingId ||= Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join('');try{const r=await fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json','X-Preview-CSRF':__CSRF__},body:JSON.stringify({...fields,id:pendingId})});const p=await r.json();if(!r.ok)throw Error(p.error||'Feedback could not be saved.');status.textContent='Saved on the office Mini. Thank you.';f.reset();pendingId=null;}catch(e){status.textContent=e.message+' Your text is still here; retry when connected.';}finally{b.disabled=false;}};})();</script>'''.replace('__CSRF__',json.dumps(csrf))
    page = page.replace('</body>',script+'</body>').encode()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # No source bodies, personal text or IP activity log.
        def respond(self, code, payload, content_type='application/json'):
            body=payload if isinstance(payload,bytes) else json.dumps(payload).encode()
            self.send_response(code)
            for k,v in {'Content-Type':content_type,'Content-Length':str(len(body)),'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}.items():
                self.send_header(k,v)
            self.end_headers();self.wfile.write(body)
        def allowed(self):
            if ipaddress.ip_address(self.client_address[0]) not in net or self.headers.get('Host','').lower() not in allowed_hosts:
                self.respond(403,{'error':'Use the office network and the planner address.'});return False
            return True
        def do_GET(self):
            if not self.allowed():return
            if self.path in ('/','/planner.html'):
                self.respond(200,page,'text/html; charset=utf-8')
            elif self.path=='/livez':self.respond(200,{'ready':True,'revision':plan['revision']})
            else:self.respond(404,{'error':'Not found'})
        def do_POST(self):
            if not self.allowed():return
            if self.path!='/api/feedback':self.respond(404,{'error':'Not found'});return
            origin=self.headers.get('Origin','')
            if origin!='http://'+self.headers.get('Host','') or not secrets.compare_digest(self.headers.get('X-Preview-CSRF',''),csrf):
                self.respond(403,{'error':'Reload this preview before sending feedback.'});return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=18000 or self.headers.get_content_type()!='application/json' or self.headers.get('Transfer-Encoding'):
                    raise ValueError('Invalid feedback request')
                self.connection.settimeout(5)
                payload=json.loads(self.rfile.read(length))
                if not isinstance(payload,dict):raise ValueError('Invalid feedback request')
                record=store.save(payload,plan['revision'])
            except (ValueError,TypeError,TimeoutError):self.respond(400,{'error':'Please use a feedback message under 4,000 characters.'});return
            except sqlite3.Error:self.respond(503,{'error':'Feedback storage unavailable; keep your text and retry.'});return
            self.respond(200,{'saved':True,'id':record})
    return Handler

def main():
    p=argparse.ArgumentParser();p.add_argument('--host',required=True);p.add_argument('--port',type=int,default=8876);p.add_argument('--network',required=True);p.add_argument('--snapshot',type=Path,required=True);p.add_argument('--feedback-db',type=Path,required=True);a=p.parse_args()
    if not ipaddress.ip_address(a.host).is_private or a.host=='0.0.0.0':raise ValueError('Bind a specific private office interface')
    plan=team_snapshot(json.loads(a.snapshot.read_text()))
    ThreadingHTTPServer((a.host,a.port),make_handler(plan,FeedbackStore(a.feedback_db),host=a.host,port=a.port,network=a.network)).serve_forever()

if __name__=='__main__':main()
