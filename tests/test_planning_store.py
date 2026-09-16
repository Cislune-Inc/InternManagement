import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from agent.planning_store import PlanningStore, PlanningError, Principal, validate_plan
from agent.planning_adapter import read_channel_batch, ingest_channel_batch, roster_bridge,read_published_excerpts,read_work_events


@contextmanager
def database(path):
    db=sqlite3.connect(path)
    try:
        with db:yield db
    finally:db.close()


def fixture():
    return dict(schema_version=1,revision='fixture',as_of='2026-09-14',scenario_start='2026-09-14',
        private_top='secret',projects=[dict(id='p',name='Example project',milestone='First useful result',private_budget='secret'),dict(id='q',name='Restricted project')],
        tasks=[dict(id='a',project='p',title='Prepare fixture',deps=[],resources=['bench'],minimum=1,likely=2,downside=3,release=0,status='proposed',private_note='secret'),
               dict(id='b',project='p',title='Validate result',deps=['a'],resources=['bench'],minimum=1,likely=2,downside=3,release=0,status='proposed'),
               dict(id='hidden',project='q',title='Restricted secret title',deps=[],resources=[],minimum=1,likely=2,downside=3,release=0,status='proposed')])


def source(version='1',**extra):
    return dict(source_ref='slack:T:C:1.0',version=version,scope='slack:T:C',project='p',
                text='Test fixture was checked.',person_ref='slack:T:U',posted_at='2026-09-14T12:00:00Z',files=[],**extra)


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'planning.sqlite'
        self.owner=Principal('slack:T:OWNER',frozenset({'p','q'}),frozenset({'slack:T:C'}))
        self.worker=Principal('slack:T:WORKER',frozenset({'p'}),frozenset({'slack:T:C'}))
        self.store=PlanningStore(self.path,owner_ref=self.owner.person_ref)
        self.store.initialize(fixture())

    def tearDown(self):self.tmp.cleanup()

    def proposal(self,request_id='p1',actor=None,**kw):
        data=dict(request_id=request_id,base_revision=1,entity='task',target='a',project='p',patch={'likely':2.5},reason='Bench availability changed.',evidence=[])
        data.update(kw)
        return self.store.proposal(actor or self.worker,**data)

    def decide(self,pid,actor=None,**kw):
        data=dict(request_id='decision-'+pid,proposal_id=pid,discussion_revision=0,decision='accepted',reason='Reviewed the evidence and next result.')
        data.update(kw)
        return self.store.decide(actor or self.owner,**data)

    def test_import_is_not_acceptance_and_fields_projected(self):
        state=self.store.view(self.worker)
        self.assertNotIn('secret',json.dumps(state))
        self.assertEqual(state['plan']['tasks'][0]['plan_state'],'imported')
        self.assertFalse(state['can_accept'])

    def test_workers_propose_owner_accepts_and_original_retained(self):
        pid=self.proposal()['id']
        self.assertEqual(self.store.view(self.owner)['revision'],1)
        with self.assertRaises(PlanningError):self.decide(pid,self.worker)
        self.assertEqual(self.decide(pid)['revision'],2)
        after=self.store.view(self.owner)
        self.assertEqual(after['plan']['tasks'][0]['likely'],2.5)
        self.assertEqual(after['plan']['tasks'][0]['accepted_fields'],{'likely':2})
        with self.store.connect() as db:
            original=json.loads(db.execute('SELECT plan FROM planning_versions WHERE revision=1').fetchone()[0])
        self.assertEqual(original['tasks'][0]['likely'],2)

    def test_change_of_acceptance_owner_requires_migration(self):
        with self.assertRaises(PlanningError):PlanningStore(self.path,owner_ref=self.worker.person_ref)

    def test_no_silent_reinitialization(self):
        altered=fixture();altered['tasks'][0]['title']='overwrite'
        self.store.initialize(altered)
        self.assertNotEqual(self.store.view(self.owner)['plan']['tasks'][0]['title'],'overwrite')

    def test_retries_are_idempotent_and_changed_payload_rejected(self):
        one=self.proposal();two=self.proposal()
        self.assertEqual(one,two)
        with self.assertRaises(PlanningError):self.proposal(patch={'likely':2.75})
        self.assertEqual(self.decide(one['id']),self.decide(one['id']))

    def test_competing_acceptances_have_one_winner(self):
        ids=[self.proposal(request_id=str(i),patch={'likely':v})['id'] for i,v in enumerate((2.25,2.5))]
        def attempt(pid):
            try:return self.decide(pid)['status']
            except PlanningError:return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,ids))
        self.assertCountEqual(results,['accepted','conflict'])
        self.assertEqual(self.store.view(self.owner)['revision'],2)

    def test_stale_proposal_cannot_be_accepted(self):
        first=self.proposal()['id'];second=self.proposal('p2',patch={'title':'Updated title'})['id']
        self.decide(first)
        with self.assertRaises(PlanningError):self.decide(second)
        self.assertTrue(next(p for p in self.store.view(self.owner)['proposals'] if p['id']==second)['stale'])

    def test_disagreement_resolution_and_reviewed_discussion_revision(self):
        pid=self.proposal()['id']
        cid=self.store.discuss(self.worker,request_id='comment',proposal_id=pid,kind='disagreement',body='Could this displace the test?')['id']
        with self.assertRaises(PlanningError):self.decide(pid)
        with self.assertRaises(PlanningError):self.decide(pid,discussion_revision=1)
        other=Principal('slack:T:OTHER',self.worker.projects)
        with self.assertRaises(PlanningError):self.store.resolve(other,request_id='resolve',comment_id=cid,resolution='Ignore it.')
        self.store.resolve(self.worker,request_id='resolve',comment_id=cid,resolution='Resolved: fixture is available in parallel.')
        self.assertEqual(self.decide(pid,discussion_revision=2)['status'],'accepted')

    def test_new_note_after_review_invalidates_acceptance(self):
        pid=self.proposal()['id']
        self.store.discuss(self.worker,request_id='late',proposal_id=pid,kind='note',body='New information for review.')
        with self.assertRaises(PlanningError):self.decide(pid)

    def test_project_access_is_server_enforced_even_for_owner(self):
        restricted=Principal(self.owner.person_ref,frozenset())
        with self.assertRaises(PlanningError):self.proposal(actor=restricted)
        self.assertEqual(self.store.view(restricted)['plan']['tasks'],[])

    def test_hidden_dependencies_do_not_leak_titles_or_become_ready(self):
        p=self.proposal(actor=self.owner,patch={'deps':['hidden']})['id'];self.decide(p)
        state=self.store.view(self.worker)
        self.assertNotIn('Restricted secret title',json.dumps(state))
        self.assertNotIn('"hidden"',json.dumps(state))
        hidden=next(t for t in state['plan']['tasks'] if t['id'].startswith('restricted-'))
        self.assertIsNone(hidden['likely']);self.assertTrue(hidden['blocker'])

    def test_dependencies_cycle_unknown_and_duration_limits(self):
        for patch in ({'deps':['b']},{'deps':['missing']},{'minimum':4},{'likely':float('inf')},{'likely':True}):
            with self.subTest(patch=patch),self.assertRaises((PlanningError,ValueError)):
                self.proposal(request_id=str(len(str(patch))),patch=patch)

    def test_new_packet_requires_review_and_creates_accepted_packet(self):
        pid=self.proposal(entity='task_create',target='new',patch={'title':'Follow-up','deps':['b']})['id']
        self.assertFalse(any(t['id']=='new' for t in self.store.view(self.owner)['plan']['tasks']))
        self.decide(pid)
        packet=next(t for t in self.store.view(self.owner)['plan']['tasks'] if t['id']=='new')
        self.assertEqual(packet['plan_state'],'accepted');self.assertIsNone(packet['likely'])

    def test_milestone_change_is_reviewed(self):
        pid=self.proposal(entity='project',target='p',patch={'milestone':'Measured result ready'})['id']
        self.decide(pid)
        self.assertEqual(self.store.view(self.owner)['plan']['projects'][0]['milestone'],'Measured result ready')

    def test_source_edits_duplicates_tombstones_and_old_versions(self):
        self.assertTrue(self.store.ingest_source(source()))
        self.assertFalse(self.store.ingest_source(source()))
        edited=source('2');edited['text']='Corrected measurement.'
        self.assertTrue(self.store.ingest_source(edited))
        self.assertFalse(self.store.ingest_source(source('1.5')))
        self.store.ingest_source(source('3',deleted=True))
        self.assertFalse(self.store.ingest_source(source('4')))
        visible=self.store.view(self.worker)['sources'][0]
        self.assertEqual(visible['text'],'');self.assertTrue(visible['deleted'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM planning_source_versions').fetchone()[0],3)

    def test_source_scope_does_not_widen_through_linking_or_owner_role(self):
        self.store.ingest_source(source())
        self.store.link_source(self.owner,request_id='link',source_ref=source()['source_ref'],packet_id='a')
        revoked=Principal(self.owner.person_ref,self.owner.projects)
        self.assertEqual(self.store.view(revoked)['sources'],[])
        with self.assertRaises(PlanningError):self.store.link_source(revoked,request_id='link',source_ref=source()['source_ref'],packet_id='a')

    def test_edited_cited_evidence_blocks_acceptance(self):
        self.store.ingest_source(source())
        pid=self.proposal(evidence=[{'source_ref':source()['source_ref'],'version':'1'}])['id']
        self.store.ingest_source(source('2'))
        with self.assertRaises(PlanningError):self.decide(pid)
        self.assertTrue(self.store.view(self.owner)['proposals'][0]['evidence_changed'])

    def test_recaps_private_append_only_and_do_not_mutate_plan(self):
        data=dict(request_id='recap',project='p',packet_id='a',plan_revision=1,result='Fixture checked.',blocker='',next_step='Run the test.',availability='Tuesday')
        result=self.store.recap(self.worker,**data)
        self.assertFalse(result['clock_changed'])
        self.assertEqual(self.store.recap(self.worker,**data),result)
        self.assertEqual(len(self.store.view(self.worker)['recaps']),1)
        stranger=Principal('slack:T:OTHER',self.worker.projects)
        self.assertEqual(self.store.view(stranger)['recaps'],[])
        self.assertEqual(len(self.store.view(self.owner)['review_recaps']),1)
        self.assertEqual(self.store.view(self.owner)['revision'],1)

    def test_restricted_evidence_cannot_leak_through_proposal_or_accepted_fields(self):
        self.store.ingest_source(source())
        p=self.proposal(patch={'title':'Private evidence-derived result'},reason='Private evidence explanation',
                        evidence=[{'source_ref':source()['source_ref'],'version':'1'}])['id']
        outsider=Principal('slack:T:OTHER',frozenset({'p'}))
        self.assertEqual(self.store.view(outsider)['proposals'],[])
        with self.assertRaises(PlanningError):self.store.discuss(outsider,request_id='hidden-comment',proposal_id=p,kind='note',body='Trying guessed ID')
        self.decide(p)
        state=self.store.view(outsider)
        self.assertNotIn('Private evidence',json.dumps(state))
        self.assertTrue(any(t['id'].startswith('restricted-') for t in state['plan']['tasks']))
        with self.assertRaises(PlanningError):self.proposal(actor=outsider,request_id='hidden-patch',base_revision=2,patch={'title':'Guess'})
        validate_plan(state['plan'])

    def test_restricted_project_milestone_does_not_leave_orphaned_packets(self):
        self.store.ingest_source(source())
        p=self.proposal(entity='project',target='p',patch={'milestone':'Private milestone'},
                        evidence=[{'source_ref':source()['source_ref'],'version':'1'}])['id']
        self.decide(p)
        outsider=Principal('slack:T:OTHER',frozenset({'p','q'}))
        state=self.store.view(outsider)
        self.assertNotIn('Private milestone',json.dumps(state))
        validate_plan(state['plan'])


class AdapterTests(unittest.TestCase):
    def test_readonly_capture_cursor_edits_and_no_clock_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'dp.sqlite'
            with database(path) as db:
                db.execute('''CREATE TABLE channel_work_updates(channel,message_ts,actor,project_key,text,files_json,posted_at,captured_at,meaningful,permalink,source_version,deleted)''')
                db.execute('CREATE TABLE sessions(id,clock_state)');db.execute("INSERT INTO sessions VALUES (1,'clocked_in')")
                for i in (1,2):db.execute('INSERT INTO channel_work_updates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',('C',f'{i}.0','U','p','Checked fixture','[]','2026-09-14','2026-09-14T12:00:00',1,'',f'{i}.0',0))
            before=path.read_bytes()
            first=read_channel_batch(path,workspace='T',channels=['C'],limit=1)
            self.assertTrue(first['has_more']);self.assertEqual(len(first['events']),1)
            second=read_channel_batch(path,workspace='T',channels=['C'],limit=1,cursor=first['next_cursor'])
            self.assertFalse(second['has_more']);self.assertEqual(second['events'][0]['source_ref'],'slack:T:C:2.0')
            self.assertEqual(path.read_bytes(),before)
            with database(path) as db:
                db.execute("UPDATE channel_work_updates SET source_version='3.0',captured_at='2026-09-14T13:00:00',deleted=1 WHERE message_ts='1.0'")
            delta=read_channel_batch(path,workspace='T',channels=['C'],cursor=second['next_cursor'])
            self.assertTrue(delta['events'][0]['deleted'])

    def test_missing_capture_table_is_explicit_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'dp.sqlite'
            sqlite3.connect(path).close()
            self.assertEqual(read_channel_batch(path,workspace='T',channels=['C'])['coverage'],'capture_table_unavailable')
            with self.assertRaises(sqlite3.OperationalError):read_channel_batch(Path(directory)/'absent.sqlite',workspace='T',channels=['C'])
            self.assertFalse((Path(directory)/'absent.sqlite').exists())

    def test_published_excerpts_do_not_expose_raw_or_held_dms(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'dp.sqlite'
            with database(path) as db:
                db.execute('CREATE TABLE dm_work_updates(actor,channel,message_ts,payload,status,raw_dm,created_at)')
                for status,ch,ts in [('sent','C','1789400000.000001'),('held','C','1789400000.000002'),('reminded','C','1789400000.000004'),('sent','D','1789400000.000003')]:
                    db.execute('INSERT INTO dm_work_updates VALUES (?,?,?,?,?,?,?)',('U',ch,ts,json.dumps({'text':status+' published result','project_key':'dp'}),status,'secret original DM','private original timestamp'))
            result=read_published_excerpts(path,workspace='T',channels=['C'],project_map={'dp':'don-pollo'})
            self.assertEqual(len(result['events']),1)
            encoded=json.dumps(result)
            self.assertNotIn('secret',encoded);self.assertNotIn('private original',encoded);self.assertNotIn('held',encoded)
            self.assertEqual(result['events'][0]['person_ref'],'slack:T:U')
            self.assertEqual(result['events'][0]['project'],'don-pollo')
            self.assertEqual(read_published_excerpts(path,workspace='T',channels=['C'],cursor=result['next_cursor'])['events'],[])

    def test_channel_adapter_requires_grants_before_opening_source(self):
        self.assertEqual(read_channel_batch(Path('/not/a/real/database'),workspace='T')['coverage'],'source_grants_required')

    def test_work_events_keep_external_ids_and_do_not_relabel_old_work(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'dp.sqlite'
            with database(path) as db:
                db.execute('CREATE TABLE work_intake_items(id,owner_id,project_key)')
                db.execute("INSERT INTO work_intake_items VALUES ('DP-EXAMPLE','U','changed_today')")
                db.execute('CREATE TABLE work_intake_events(id,item_id,actor_id,kind,text,created_at)')
                db.execute("INSERT INTO work_intake_events VALUES (1,'DP-EXAMPLE','U','update','Original report','2026-09-14T12:00:00Z')")
            result=read_work_events(path,workspace='T')
            event=result['events'][0]
            self.assertEqual(event['project'],'unmapped')
            self.assertEqual(event['scope'],'person:slack:T:U')
            self.assertEqual(event['external_item_ref'],'DP-EXAMPLE')
            self.assertNotIn('changed_today',json.dumps(result))


if __name__=='__main__':unittest.main()
