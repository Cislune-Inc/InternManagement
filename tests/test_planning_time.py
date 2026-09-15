from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from agent.models import SessionState, UserProfile
from agent.planning_store import Principal
from agent.planning_time import read_person_time,dp_time_reader


class PlanningTimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'ledger.sqlite'
        self.now=datetime(2026,9,14,20,tzinfo=timezone.utc)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE sessions(user_key,session_date,payload)')
            db.execute('CREATE TABLE slack_clock_reports(user_key,status,text)')
    def tearDown(self):self.tmp.cleanup()
    def add(self,key='worker',start='2026-09-14T16:00:00+00:00',end=None,metadata=None,date='2026-09-14'):
        s=SessionState(user_key=key,session_date=date,clocked_in_at=start,clocked_out_at=end,metadata=metadata or {})
        with sqlite3.connect(self.path) as db:db.execute('INSERT INTO sessions VALUES (?,?,?)',(key,date,json.dumps(asdict(s))))
    def read(self):return read_person_time(self.path,user_key='worker',timezone_name='America/Los_Angeles',now=self.now)
    def test_same_dp_totals_lunch_paid_rest_and_no_mutation(self):
        self.add(metadata={'lunch_windows':[{'started_at':'2026-09-14T18:00:00+00:00','ended_at':'2026-09-14T18:30:00+00:00'}],'slack_clock_rest_started_at':'2026-09-14T19:55:00+00:00'})
        self.add(key='other',start='2026-09-14T08:00:00+00:00')
        before=self.path.read_bytes();result=self.read()
        self.assertEqual(result['recorded_seconds_today'],12600)
        self.assertEqual(result['clock_state'],'On paid rest')
        self.assertEqual(before,self.path.read_bytes())
        self.assertNotIn('other',json.dumps(result))
    def test_overlap_union_and_legacy_unknown(self):
        self.add();self.add(start='2026-09-14T17:00:00+00:00',end='2026-09-14T18:00:00+00:00')
        self.add(start='2026-09-01T00:00:00+00:00',date='2026-09-01',metadata={'slack_clock_legacy_unresolved':True})
        self.assertEqual(self.read()['recorded_seconds_today'],14400)
        self.assertTrue(self.read()['unresolved'])
    def test_midnight_clipping_and_pending_report_content_private(self):
        self.add(start='2026-09-14T06:00:00+00:00',end='2026-09-14T08:00:00+00:00',date='2026-09-13')
        with sqlite3.connect(self.path) as db:db.execute("INSERT INTO slack_clock_reports VALUES ('worker','pending','private narrative')")
        result=self.read();self.assertEqual(result['recorded_seconds_today'],3600)
        self.assertNotIn('private narrative',json.dumps(result))
        self.assertTrue(result['unresolved'])
    async def test_runtime_resolves_only_current_roster_identity(self):
        self.add()
        user=UserProfile(user_key='worker',display_name='Worker',slack_user_id='U')
        runtime=SimpleNamespace(roster_by_slack_id={'U':user},state_store=SimpleNamespace(db_path=self.path),config=SimpleNamespace(timezone='America/Los_Angeles'))
        reader=dp_time_reader(runtime,workspace='T');actor=Principal('slack:T:U',frozenset({'p'}))
        self.assertIsNotNone(await reader(actor))
        user.active=False;self.assertIsNone(await reader(actor))
        self.assertIsNone(await reader(Principal('slack:OTHER:U',frozenset({'p'}))))
