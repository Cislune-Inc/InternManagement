import copy
import unittest
from agent.planning_source_groups import group_visible_sources
import test_planning_store as fixtures
from test_planning_store import source


class SourceGroupingTests(unittest.TestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown

    def test_visible_revisions_recompute_group_without_changing_records(self):
        excerpt = source(source_kind='dp_published_work_excerpt')
        excerpt.update(source_ref='published:T:C:1.0', text='Work update\n> Installed the load cell and checked its calibration')
        human = source(source_kind='slack_channel', meaningful=True)
        human.update(source_ref='slack:T:C:2.0',text='Installed the load cell and checked its calibration. Mounting still needs review.')
        self.store.ingest_source(excerpt); self.store.ingest_source(human)
        def rows():return {s['source_ref']:s for s in self.store.view(self.owner)['sources']}
        grouped = rows()[excerpt['source_ref']]
        self.assertEqual(grouped['preferred_source'],human['source_ref'])
        self.assertFalse(grouped['count_as_separate_progress'])
        self.assertEqual(len(rows()),2)
        self.store.ingest_source(dict(human,version='3',text='Calibration is blocked pending replacement hardware.'))
        self.assertNotIn('preferred_source',rows()[excerpt['source_ref']])
        self.store.ingest_source(dict(human,version='4'))
        self.assertIn('preferred_source',rows()[excerpt['source_ref']])
        self.store.ingest_source(dict(human,version='5',deleted=True))
        self.assertNotIn('preferred_source',rows()[excerpt['source_ref']])
        with self.store.connect() as db:
            payload=db.execute('SELECT payload FROM planning_sources WHERE source_ref=?',(excerpt['source_ref'],)).fetchone()[0]
        self.assertNotIn('preferred_source',payload)


class GroupBoundaryTests(unittest.TestCase):
    def test_no_cross_audience_author_project_or_fuzzy_matching(self):
        quote='installed load cell checked calibration bench setup'
        excerpt=dict(source_ref='published:T:C:1',scope='slack:T:C',person_ref='slack:T:U',project='p',
                     source_kind='dp_published_work_excerpt',text='> '+quote,posted_at='2026-09-16T00:00:00Z')
        human=dict(excerpt,source_ref='slack:T:C:2',source_kind='slack_channel',text=quote+' and mounting needs review',meaningful=True)
        for change in ({'scope':'slack:T:D'}, {'person_ref':'slack:T:OTHER'}, {'project':'q'},
                       {'posted_at':'2026-09-18T00:00:00Z'}, {'deleted':True}, {'meaningful':False},
                       {'text':'not installed load cell not checked calibration unresolved bench setup'}):
            rows=group_visible_sources([copy.deepcopy(excerpt),dict(human,**change)])
            self.assertNotIn('preferred_source',rows[0],change)
        rows=group_visible_sources([dict(excerpt,preferred_source='private:old',count_as_separate_progress=False)])
        self.assertNotIn('preferred_source',rows[0])
