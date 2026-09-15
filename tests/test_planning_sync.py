import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent.planning_store import PlanningError, PlanningStore
from agent.planning_sync import sync_sources


class SourceSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source = root / 'source.sqlite'
        self.checkpoint = root / 'checkpoint.sqlite'
        self.store = PlanningStore(root / 'planner.sqlite', owner_ref='slack:T:OWNER')
        with sqlite3.connect(self.source) as db:
            db.execute('CREATE TABLE channel_work_updates(channel,message_ts,actor,project_key,text,'
                       'files_json,posted_at,captured_at,meaningful,permalink,source_version,deleted)')
            for i in range(1, 4):
                db.execute('INSERT INTO channel_work_updates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                           ('C', str(i)+'.0', 'U', 'p', 'Evidence', '[]', '2026-09-14',
                            '2026-09-14', 1, '', str(i)+'.0', 0))

    def tearDown(self):
        self.tmp.cleanup()

    def run_sync(self, **kwargs):
        options = dict(stream='capture', workspace='T', channels=['C'], limit=2)
        options.update(kwargs)
        return sync_sources(self.store, self.source, self.checkpoint, **options)

    def test_bounded_resume_and_replay(self):
        first = self.run_sync()
        self.assertEqual((first['read'], first['changed'], first['has_more']), (2, 2, True))
        second = self.run_sync()
        self.assertEqual((second['read'], second['changed'], second['has_more']), (1, 1, False))
        self.assertEqual(self.run_sync()['read'], 0)
        replay = self.run_sync(reconcile=True, max_batches=2)
        self.assertEqual((replay['read'], replay['changed']), (3, 0))

    def test_partial_failure_does_not_advance_checkpoint(self):
        ingest = self.store.ingest_source
        calls = 0
        def fail_second(event):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('interrupted')
            return ingest(event)
        with patch.object(self.store, 'ingest_source', side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.run_sync()
        retry = self.run_sync()
        self.assertEqual((retry['read'], retry['changed']), (2, 1))
        self.assertEqual(self.run_sync()['read'], 1)

    def test_grants_and_routing_changes_require_new_stream(self):
        self.run_sync()
        for change in ({'channels': ['D']}, {'project_map': {'p': 'q'}}, {'workspace': 'OTHER'}):
            with self.assertRaises(PlanningError):
                self.run_sync(**change)
        self.assertEqual(self.run_sync(stream='new', channels=['D'])['read'], 0)

    def test_missing_capture_is_coverage_gap(self):
        with sqlite3.connect(self.source) as db:
            db.execute('DROP TABLE channel_work_updates')
        result = self.run_sync()
        self.assertEqual(result['coverage'], 'capture_table_unavailable')
        self.assertEqual(result['next_cursor'], ['', '', ''])

    def test_published_stream_excludes_held_private_input(self):
        with sqlite3.connect(self.source) as db:
            db.execute('CREATE TABLE dm_work_updates(actor,channel,message_ts,payload,status)')
            for status, ts, text in [('sent', '100.0', 'Shared excerpt'),
                                     ('held', '101.0', 'Private original')]:
                db.execute('INSERT INTO dm_work_updates VALUES (?,?,?,?,?)',
                           ('U', 'C', ts, json.dumps(dict(project_key='p', text=text)), status))
        result = self.run_sync(kind='published')
        self.assertEqual(result['read'], 1)
        self.assertEqual(result['coverage'], 'published_excerpts_only_not_live_reconciliation')
        self.assertNotIn(b'Private original', self.store.path.read_bytes())

    def test_reconciliation_finds_late_tombstone(self):
        self.run_sync(max_batches=2)
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE channel_work_updates SET captured_at='2026-09-13',"
                       "source_version='10.0',deleted=1 WHERE message_ts='1.0'")
        self.assertEqual(self.run_sync()['read'], 0)
        self.assertEqual(self.run_sync(reconcile=True, max_batches=2)['changed'], 1)

    def test_no_source_writes_or_private_text_in_checkpoint(self):
        before = self.source.read_bytes()
        self.run_sync()
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(self.checkpoint.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(self.checkpoint) as db:
            summary = json.loads(db.execute('SELECT summary FROM source_checkpoints').fetchone()[0])
        self.assertNotIn('Evidence', json.dumps(summary))

    def test_rejects_unbounded_or_ungranted_reads_and_aliases(self):
        for options in ({'channels': []}, {'limit': 201}, {'max_batches': 11}, {'kind': 'private'}):
            with self.assertRaises(PlanningError):
                self.run_sync(**options)
        with self.assertRaises(PlanningError):
            sync_sources(self.store, self.source, self.source, stream='x', workspace='T', channels=['C'])
