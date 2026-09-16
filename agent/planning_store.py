"""Versioned coordination state. No imports from clock, payroll or Slack runtime.

Principals and source grants MUST come from the host's verified identity provider.
This store never accepts a role, audience grant or accepted revision from a client.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from .planning_source_groups import group_visible_sources


class PlanningError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Principal:
    person_ref: str
    projects: frozenset[str]
    source_scopes: frozenset[str] = frozenset()


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def text(value, limit=4000, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise PlanningError('Please supply a valid, bounded text value.')
    return value.strip()


def identifier(value):
    value = text(value, 160)
    if not re.fullmatch(r'[A-Za-z0-9_.:@/-]+', value):
        raise PlanningError('Invalid reference.')
    return value


TASK_FIELDS = {'title', 'deps', 'resources', 'minimum', 'likely', 'downside',
               'release', 'priority', 'status', 'blocker', 'done_when',
               'availability_confirmed', 'owner_ref', 'estimate_basis'}
PROJECT_FIELDS = {'name', 'milestone', 'note'}


def validate_plan(plan):
    if not isinstance(plan, dict) or plan.get('schema_version') != 1:
        raise PlanningError('Expected planning schema version 1.')
    projects, tasks = plan.get('projects'), plan.get('tasks')
    if not isinstance(projects, list) or not isinstance(tasks, list) or len(tasks) > 200:
        raise PlanningError('Use a project list and at most 200 work packets.')
    if any(not isinstance(p, dict) for p in projects + tasks):
        raise PlanningError('Invalid project or packet.')
    pids = [identifier(p.get('id')) for p in projects]
    ids = [identifier(t.get('id')) for t in tasks]
    if len(set(pids)) != len(pids) or len(set(ids)) != len(ids):
        raise PlanningError('Project and packet IDs must be unique.')
    for p in projects:
        text(p.get('name'), 300)
        for key in ('milestone', 'note'):
            if key in p:
                text(p[key], required=False)
    for t in tasks:
        if t.get('project') not in pids:
            raise PlanningError('Unknown project.')
        text(t.get('title'), 300)
        for key in ('blocker', 'done_when', 'estimate_basis'):
            if key in t:
                text(t[key], required=False)
        if t.get('owner_ref'):
            identifier(t['owner_ref'])
        for key in ('deps', 'resources'):
            values = t.get(key)
            if not isinstance(values, list) or len(values) > 200 or any(not isinstance(v, str) for v in values):
                raise PlanningError('Dependencies and resources must be lists.')
            if len(set(values)) != len(values):
                raise PlanningError('Duplicate dependency or resource.')
            for v in values:
                text(v, 160)
        if any(d not in ids or d == t['id'] for d in t['deps']):
            raise PlanningError('Unknown or self dependency.')
        for key in ('minimum', 'likely', 'downside', 'release', 'priority'):
            v = t.get(key, 100 if key == 'priority' else None)
            if v is None and key in ('minimum', 'likely', 'downside'):
                continue
            if type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1000:
                raise PlanningError('Durations and offsets must be finite, nonnegative workdays.')
        values = [t.get(k) for k in ('minimum', 'likely', 'downside') if t.get(k) is not None]
        if values != sorted(values):
            raise PlanningError('Use minimum ≤ likely ≤ downside.')
        if t.get('status') not in ('proposed', 'in_progress', 'done'):
            raise PlanningError('Invalid packet status.')
        if type(t.get('availability_confirmed', False)) is not bool:
            raise PlanningError('Availability must be explicitly true or false.')
    visited = set()
    while len(visited) < len(tasks):
        ready = {t['id'] for t in tasks if t['id'] not in visited and set(t['deps']) <= visited}
        if not ready:
            raise PlanningError('Dependency cycle: revise predecessor links.')
        visited.update(ready)


class PlanningStore:
    def __init__(self, path: Path, *, owner_ref: str):
        self.path = Path(path)
        self.owner_ref = identifier(owner_ref)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS planning_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS planning_versions(
              revision INTEGER PRIMARY KEY,plan TEXT NOT NULL,actor TEXT NOT NULL,
              reason TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS planning_proposals(
              id TEXT PRIMARY KEY,project TEXT NOT NULL,entity TEXT NOT NULL,target TEXT NOT NULL,
              patch TEXT NOT NULL,before_json TEXT NOT NULL,reason TEXT NOT NULL,author TEXT NOT NULL,
              base_revision INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'open',
              discussion_revision INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,
              decided_by TEXT,decision_reason TEXT,decided_at TEXT,accepted_revision INTEGER);
            CREATE TABLE IF NOT EXISTS planning_discussion(
              id TEXT PRIMARY KEY,proposal_id TEXT NOT NULL,author TEXT NOT NULL,
              kind TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL,
              resolved_by TEXT,resolution TEXT,resolved_at TEXT);
            CREATE TABLE IF NOT EXISTS planning_sources(
              source_ref TEXT PRIMARY KEY,version TEXT NOT NULL,project TEXT NOT NULL,
              scope TEXT NOT NULL,payload TEXT NOT NULL,deleted INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS planning_source_versions(
              source_ref TEXT NOT NULL,version TEXT NOT NULL,payload TEXT NOT NULL,
              PRIMARY KEY(source_ref,version));
            CREATE TABLE IF NOT EXISTS planning_links(
              source_ref TEXT NOT NULL,packet_id TEXT NOT NULL,actor TEXT NOT NULL,created_at TEXT NOT NULL,
              PRIMARY KEY(source_ref,packet_id));
            CREATE TABLE IF NOT EXISTS planning_recaps(
              id TEXT PRIMARY KEY,author TEXT NOT NULL,project TEXT NOT NULL,packet_id TEXT NOT NULL,
              plan_revision INTEGER NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS planning_receipts(
              actor TEXT NOT NULL,request_id TEXT NOT NULL,operation TEXT NOT NULL,
              fingerprint TEXT NOT NULL,response TEXT NOT NULL,PRIMARY KEY(actor,request_id));
            ''')
            columns={r[1] for r in db.execute('PRAGMA table_info(planning_proposals)')}
            if 'evidence' not in columns:
                db.execute("ALTER TABLE planning_proposals ADD COLUMN evidence TEXT NOT NULL DEFAULT '[]'")
            existing = db.execute("SELECT value FROM planning_meta WHERE key='owner_ref'").fetchone()
            if existing and existing[0] != self.owner_ref:
                raise PlanningError('Changing the acceptance owner requires an explicit migration.', 409)
            db.execute("INSERT OR IGNORE INTO planning_meta VALUES ('owner_ref',?)", (self.owner_ref,))
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self, plan: dict):
        """Operator-only, one-time import. An imported snapshot is not an accepted plan."""
        plan = json.loads(encode(plan))
        validate_plan(plan)
        for row in plan['projects'] + plan['tasks']:
            row['plan_state'] = 'imported'
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM planning_versions LIMIT 1').fetchone():
                db.execute('INSERT INTO planning_versions VALUES (1,?,?,?,?)',
                           (encode(plan), 'operator:import', 'Unaccepted starting snapshot', now()))

    @staticmethod
    def _plan(db):
        row = db.execute('SELECT revision,plan FROM planning_versions ORDER BY revision DESC LIMIT 1').fetchone()
        if not row:
            raise PlanningError('No starting snapshot loaded.', 503)
        return row['revision'], json.loads(row['plan'])

    def _allow(self, actor, project):
        if not isinstance(actor, Principal) or not actor.person_ref or project not in actor.projects:
            raise PlanningError('This project is not available to this identity.', 403)

    def _owner(self, actor):
        if actor.person_ref != self.owner_ref:
            raise PlanningError('Only the designated owner can accept or reject plan changes.', 403)

    @staticmethod
    def _visible(row,actor):
        return set(row.get('required_source_scopes',[]))<=actor.source_scopes

    def _mutate(self, actor, request_id, operation, payload, action):
        identifier(request_id)
        fingerprint = hashlib.sha256(encode(payload).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM planning_receipts WHERE actor=? AND request_id=?',
                             (actor.person_ref, request_id)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint or old['operation'] != operation:
                    raise PlanningError('This request ID was already used for a different change.', 409)
                return json.loads(old['response'])
            result = action(db)
            db.execute('INSERT INTO planning_receipts VALUES (?,?,?,?,?)',
                       (actor.person_ref, request_id, operation, fingerprint, encode(result)))
            return result

    def proposal(self, actor, *, request_id, base_revision, entity, target, project, patch, reason, evidence):
        self._allow(actor, project)
        if type(base_revision) is not int or entity not in ('task', 'task_create', 'project'):
            raise PlanningError('Select a packet or milestone and its reviewed revision.')
        if not isinstance(patch, dict) or not patch or set(patch) - (PROJECT_FIELDS if entity == 'project' else TASK_FIELDS):
            raise PlanningError('Unsupported plan change.')
        identifier(target)
        reason = text(reason)
        if not isinstance(evidence,list) or len(evidence)>20 or any(not isinstance(e,dict) or set(e)!={'source_ref','version'} for e in evidence):
            raise PlanningError('Evidence must contain source references and reviewed versions.')
        payload = dict(base_revision=base_revision, entity=entity, target=target, project=project, patch=patch, reason=reason,evidence=evidence)
        def apply(db):
            revision, plan = self._plan(db)
            if revision != base_revision:
                raise PlanningError('The plan changed. Refresh and review your proposal against the new revision.', 409)
            self._check_evidence(db,actor,project,evidence)
            project_row=next((r for r in plan['projects'] if r['id']==project),None)
            if not project_row or not self._visible(project_row,actor):
                raise PlanningError('Project details require their original source access.',403)
            rows = plan['projects' if entity == 'project' else 'tasks']
            row = next((r for r in rows if r['id'] == target), None)
            if entity=='task_create':
                if row is not None:
                    raise PlanningError('Packet ID already exists.',409)
                row=dict(id=target,project=project,title='New packet',deps=[],resources=[],minimum=None,likely=None,downside=None,
                         release=0,priority=100,status='proposed',availability_confirmed=False,plan_state='proposed')
                rows.append(row)
            if row is None or (row['id'] if entity == 'project' else row['project']) != project:
                raise PlanningError('Packet or project not found.', 404)
            if not self._visible(row,actor):
                raise PlanningError('Packet details require their original source access.',403)
            if entity != 'project' and 'deps' in patch:
                allowed_projects={p['id'] for p in plan['projects'] if p['id'] in actor.projects and self._visible(p,actor)}
                visible_ids = {t['id'] for t in plan['tasks'] if t['project'] in allowed_projects and self._visible(t,actor)}
                if not isinstance(patch['deps'], list) or any(d not in visible_ids for d in patch['deps']):
                    raise PlanningError('A dependency requires access to its project.', 403)
            before = {} if entity=='task_create' else {k: row.get(k) for k in patch}
            if before == patch:
                raise PlanningError('No change to propose.')
            row.update(patch)
            validate_plan(plan)
            pid = uuid4().hex
            db.execute('''INSERT INTO planning_proposals
                (id,project,entity,target,patch,before_json,reason,author,base_revision,created_at,evidence)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (pid,project,entity,target,encode(patch),encode(before),reason,actor.person_ref,base_revision,now(),encode(evidence)))
            return {'id': pid, 'status': 'open'}
        return self._mutate(actor, request_id, 'proposal', payload, apply)

    def _check_evidence(self,db,actor,project,evidence):
        for e in evidence:
            row=db.execute('SELECT * FROM planning_sources WHERE source_ref=?',(identifier(e['source_ref']),)).fetchone()
            if not row or row['scope'] not in actor.source_scopes or row['project']!=project:
                raise PlanningError('You cannot review one of the cited sources.',403)
            if row['deleted'] or row['version']!=e['version']:
                raise PlanningError('Cited evidence changed or was deleted. Review the current source before proposing again.',409)

    def _proposal_access(self, actor, proposal_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM planning_proposals WHERE id=?', (proposal_id,)).fetchone()
        if not row:
            raise PlanningError('Proposal not found.', 404)
        self._allow(actor, row['project'])
        with self.connect() as db:
            _,plan=self._plan(db)
            project_row=next((p for p in plan['projects'] if p['id']==row['project']),None)
            task_row=next((t for t in plan['tasks'] if t['id']==row['target']),None)
            if not project_row or not self._visible(project_row,actor) or (task_row and not self._visible(task_row,actor)):
                raise PlanningError('This proposal follows its current plan audience.',403)
            for e in json.loads(row['evidence']):
                source=db.execute('SELECT scope FROM planning_sources WHERE source_ref=?',(e['source_ref'],)).fetchone()
                if not source or source['scope'] not in actor.source_scopes:
                    raise PlanningError('This proposal follows its original evidence audience.',403)
        return row

    def discuss(self, actor, *, request_id, proposal_id, kind, body):
        self._proposal_access(actor, proposal_id)
        if kind not in ('note', 'question', 'disagreement'):
            raise PlanningError('Choose note, question or disagreement.')
        body = text(body)
        def apply(db):
            row = db.execute('SELECT status FROM planning_proposals WHERE id=?', (proposal_id,)).fetchone()
            if row['status'] != 'open':
                raise PlanningError('This proposal is closed. Make a new proposal for further changes.', 409)
            cid = uuid4().hex
            db.execute('INSERT INTO planning_discussion (id,proposal_id,author,kind,body,created_at) VALUES (?,?,?,?,?,?)',
                       (cid, proposal_id, actor.person_ref, kind, body, now()))
            db.execute('UPDATE planning_proposals SET discussion_revision=discussion_revision+1 WHERE id=?', (proposal_id,))
            return {'id': cid}
        return self._mutate(actor,request_id,'discuss',dict(proposal_id=proposal_id,kind=kind,body=body),apply)

    def resolve(self, actor, *, request_id, comment_id, resolution):
        with self.connect() as db:
            row = db.execute('SELECT * FROM planning_discussion WHERE id=?', (comment_id,)).fetchone()
        if not row:
            raise PlanningError('Discussion item not found.', 404)
        self._proposal_access(actor, row['proposal_id'])
        if actor.person_ref not in (row['author'], self.owner_ref):
            raise PlanningError('Only the author or acceptance owner can resolve this question.', 403)
        resolution = text(resolution)
        def apply(db):
            comment = db.execute('SELECT * FROM planning_discussion WHERE id=?', (comment_id,)).fetchone()
            if comment['kind'] == 'note' or comment['resolved_at']:
                raise PlanningError('This item is not an unresolved question.', 409)
            db.execute('UPDATE planning_discussion SET resolved_by=?,resolution=?,resolved_at=? WHERE id=?',
                       (actor.person_ref,resolution,now(),comment_id))
            db.execute('UPDATE planning_proposals SET discussion_revision=discussion_revision+1 WHERE id=?', (row['proposal_id'],))
            return {'resolved': True}
        return self._mutate(actor,request_id,'resolve',dict(comment_id=comment_id,resolution=resolution),apply)

    def decide(self, actor, *, request_id, proposal_id, discussion_revision, decision, reason):
        self._owner(actor)
        self._proposal_access(actor, proposal_id)
        if decision not in ('accepted', 'rejected') or type(discussion_revision) is not int:
            raise PlanningError('Review the discussion before making a decision.')
        reason = text(reason)
        def apply(db):
            p = db.execute('SELECT * FROM planning_proposals WHERE id=?', (proposal_id,)).fetchone()
            if p['status'] != 'open' or p['discussion_revision'] != discussion_revision:
                raise PlanningError('The proposal or discussion changed. Refresh before deciding.', 409)
            accepted_revision = None
            if decision == 'accepted':
                if db.execute("SELECT 1 FROM planning_discussion WHERE proposal_id=? AND kind!='note' AND resolved_at IS NULL", (proposal_id,)).fetchone():
                    raise PlanningError('Resolve the open questions and disagreements before accepting.', 409)
                revision, plan = self._plan(db)
                self._check_evidence(db,actor,p['project'],json.loads(p['evidence']))
                if revision != p['base_revision']:
                    raise PlanningError('This proposal is stale. Propose it again against the current plan.', 409)
                rows = plan['projects' if p['entity'] == 'project' else 'tasks']
                if p['entity']=='task_create':
                    target=dict(id=p['target'],project=p['project'],deps=[],resources=[],minimum=None,likely=None,downside=None,
                                release=0,priority=100,status='proposed',availability_confirmed=False,plan_state='accepted')
                    rows.append(target)
                else:
                    target = next(r for r in rows if r['id'] == p['target'])
                target.update(json.loads(p['patch']))
                inherited=set(target.get('required_source_scopes',[]))
                for e in json.loads(p['evidence']):
                    inherited.add(db.execute('SELECT scope FROM planning_sources WHERE source_ref=?',(e['source_ref'],)).fetchone()[0])
                target['required_source_scopes']=sorted(inherited)
                # Acceptance applies to these changed fields, not unrelated imported assignments.
                target.setdefault('accepted_fields', {}).update({k:revision+1 for k in json.loads(p['patch'])})
                validate_plan(plan)
                accepted_revision = revision + 1
                db.execute('INSERT INTO planning_versions VALUES (?,?,?,?,?)',
                           (accepted_revision,encode(plan),actor.person_ref,reason,now()))
            db.execute('''UPDATE planning_proposals SET status=?,decided_by=?,decision_reason=?,decided_at=?,accepted_revision=? WHERE id=?''',
                       (decision,actor.person_ref,reason,now(),accepted_revision,proposal_id))
            return {'status': decision, 'revision': accepted_revision}
        return self._mutate(actor,request_id,'decide',dict(proposal_id=proposal_id,discussion_revision=discussion_revision,decision=decision,reason=reason),apply)

    def ingest_source(self, source):
        """Trusted, read-only-adapter input. Not exposed as a worker HTTP mutation."""
        source = json.loads(encode(source))
        for key in ('source_ref', 'project', 'scope'):
            identifier(source.get(key))
        version = text(source.get('version'), 40)
        try:
            numeric = Decimal(version)
            if not numeric.is_finite() or numeric < 0:
                raise InvalidOperation
        except InvalidOperation as exc:
            raise PlanningError('Source revision must be an ordered numeric value.') from exc
        text(source.get('text', ''), 6000, required=False)
        if type(source.get('deleted', False)) is not bool:
            raise PlanningError('Invalid source tombstone.')
        if source.get('deleted'):
            source['text'], source['files'] = '', []
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM planning_sources WHERE source_ref=?', (source['source_ref'],)).fetchone()
            if old and (old['deleted'] or Decimal(old['version']) >= numeric):
                return False
            if old and (old['scope'] != source['scope'] or old['project'] != source['project']):
                raise PlanningError('Source routing/audience changed; explicit reconciliation required.', 409)
            db.execute('INSERT INTO planning_source_versions VALUES (?,?,?)', (source['source_ref'],version,encode(source)))
            db.execute('''INSERT INTO planning_sources VALUES (?,?,?,?,?,?) ON CONFLICT(source_ref) DO UPDATE SET
                version=excluded.version,payload=excluded.payload,deleted=excluded.deleted''',
                (source['source_ref'],version,source['project'],source['scope'],encode(source),int(source.get('deleted',False))))
        return True

    def link_source(self, actor, *, request_id, source_ref, packet_id):
        self._owner(actor)
        def check(db):
            _, plan = self._plan(db)
            task = next((t for t in plan['tasks'] if t['id'] == packet_id), None)
            source = db.execute('SELECT * FROM planning_sources WHERE source_ref=?', (source_ref,)).fetchone()
            if not task or not source:
                raise PlanningError('Packet or evidence not found.', 404)
            self._allow(actor, task['project'])
            if not self._visible(task,actor):
                raise PlanningError('This packet requires its original source access.',403)
            if source['scope'] not in actor.source_scopes or source['project'] != task['project'] or source['deleted']:
                raise PlanningError('This source cannot be linked to this packet.', 403)
        with self.connect() as db:
            check(db)
        def apply(db):
            check(db)
            db.execute('INSERT OR IGNORE INTO planning_links VALUES (?,?,?,?)', (source_ref,packet_id,actor.person_ref,now()))
            return {'linked': True}
        return self._mutate(actor,request_id,'link',dict(source_ref=source_ref,packet_id=packet_id),apply)

    def recap(self, actor, *, request_id, project, packet_id, plan_revision, result, blocker, next_step, availability):
        self._allow(actor, project)
        payload = {k:text(v,required=False) for k,v in dict(result=result,blocker=blocker,next_step=next_step,availability=availability).items()}
        if not any(payload.values()):
            raise PlanningError('Add a result, blocker or next step before saving.')
        if type(plan_revision) is not int:
            raise PlanningError('Review the current plan first.')
        def apply(db):
            revision, plan = self._plan(db)
            if revision != plan_revision:
                raise PlanningError('The plan changed. Review the latest next step before saving.', 409)
            if not any(t['id'] == packet_id and t['project'] == project and self._visible(t,actor) for t in plan['tasks']):
                raise PlanningError('Packet not found.', 404)
            rid = uuid4().hex
            db.execute('INSERT INTO planning_recaps VALUES (?,?,?,?,?,?,?)',
                       (rid,actor.person_ref,project,packet_id,revision,encode(payload),now()))
            return {'id': rid, 'saved': True, 'clock_changed': False}
        return self._mutate(actor,request_id,'recap',dict(project=project,packet_id=packet_id,plan_revision=plan_revision,**payload),apply)

    def view(self, actor):
        """Project grants and source scopes are re-evaluated on EVERY read."""
        with self.connect() as db:
            db.execute('BEGIN')
            revision, plan = self._plan(db)
            plan['projects'] = [{k:v for k,v in p.items() if k in PROJECT_FIELDS|{'id','category','plan_state','accepted_fields','source'}}
                                for p in plan['projects'] if p['id'] in actor.projects and self._visible(p,actor)]
            visible_projects={p['id'] for p in plan['projects']}
            visible = [{k:v for k,v in t.items() if k in TASK_FIELDS|{'id','project','plan_state','accepted_fields','source'}}
                       for t in plan['tasks'] if t['project'] in visible_projects and self._visible(t,actor)]
            visible_ids = {t['id'] for t in visible}
            hidden = set(d for t in visible for d in t['deps'] if d not in visible_ids)
            aliases = {d:'restricted-'+hashlib.sha256(d.encode()).hexdigest()[:16] for d in hidden}
            for t in visible:
                t['deps'] = [aliases.get(d,d) for d in t['deps']]
            if hidden:
                plan['projects'].append({'id':'restricted-prerequisites','name':'Restricted prerequisites'})
                visible.extend(dict(id=alias,project='restricted-prerequisites',title='External prerequisite — details restricted',
                    deps=[],resources=[],minimum=None,likely=None,downside=None,release=0,status='proposed',
                    blocker='An authorized reviewer must confirm this dependency.',availability_confirmed=False)
                    for alias in aliases.values())
            plan['tasks'] = visible
            # Top-level snapshot metadata is allowlisted; no arbitrary private import fields.
            plan = {k:v for k,v in plan.items() if k in ('schema_version','projects','tasks','as_of','scenario_start')}
            plan['revision'] = str(revision)
            proposals = []
            for row in db.execute('SELECT * FROM planning_proposals ORDER BY created_at DESC'):
                if row['project'] not in actor.projects:
                    continue
                evidence_scopes=[db.execute('SELECT scope FROM planning_sources WHERE source_ref=?',(e['source_ref'],)).fetchone() for e in json.loads(row['evidence'])]
                if row['project'] not in visible_projects or any(not r or r[0] not in actor.source_scopes for r in evidence_scopes):
                    continue
                if row['entity']!='project' and row['entity']!='task_create' and row['target'] not in visible_ids:
                    continue
                p = dict(row)
                p['patch'], p['before'] = json.loads(p.pop('patch')), json.loads(p.pop('before_json'))
                p['evidence']=json.loads(p['evidence'])
                shown=[];p['evidence_changed']=False
                for e in p['evidence']:
                    source=db.execute('SELECT * FROM planning_sources WHERE source_ref=?',(e['source_ref'],)).fetchone()
                    if source and source['scope'] in actor.source_scopes:
                        shown.append(e)
                        p['evidence_changed'] |= bool(source['deleted'] or source['version']!=e['version'])
                    else:
                        shown.append({'restricted':True})
                p['evidence']=shown
                # A changed dependency list can include a project no longer in this viewer's grants.
                for field in ('patch','before'):
                    if 'deps' in p[field]:
                        p[field]['deps'] = [d if d in visible_ids else 'restricted-prerequisite' for d in p[field]['deps']]
                p['stale'] = p['status'] == 'open' and p['base_revision'] != revision
                p['discussion'] = [dict(c) for c in db.execute('SELECT * FROM planning_discussion WHERE proposal_id=? ORDER BY created_at', (p['id'],))]
                proposals.append(p)
            sources = []
            for row in db.execute('SELECT * FROM planning_sources'):
                if row['project'] in actor.projects and row['scope'] in actor.source_scopes:
                    source = json.loads(row['payload'])
                    source['packets'] = [r[0] for r in db.execute('SELECT packet_id FROM planning_links WHERE source_ref=?', (row['source_ref'],)) if r[0] in visible_ids]
                    sources.append(source)
            group_visible_sources(sources)
            recaps = [];review_recaps=[]
            for row in db.execute('SELECT * FROM planning_recaps ORDER BY created_at DESC'):
                if row['project'] in actor.projects and (row['author'] == actor.person_ref or actor.person_ref==self.owner_ref):
                    item = dict(row); item['content'] = json.loads(item.pop('payload'))
                    (recaps if row['author']==actor.person_ref else review_recaps).append(item)
            return dict(revision=revision,plan=plan,proposals=proposals,sources=sorted(sources,key=lambda s:s.get('posted_at',''),reverse=True),
                        recaps=recaps,review_recaps=review_recaps,person_ref=actor.person_ref,can_accept=actor.person_ref==self.owner_ref,
                        accepted_by=self.owner_ref,source_coverage='Only explicitly connected sources visible to your current grants.')
