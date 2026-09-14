from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlsplit

from aiohttp.test_utils import TestClient, TestServer
from agent.models import AdminProfile, SlackConfig, UserProfile
from agent.state_store import StateStore
from agent.worker_portal import build_worker_portal_link
from agent.planning_host import create_dp_planning_app
from agent.planning_store import PlanningStore
from test_planning_store import fixture


class PlanningHostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.owner = AdminProfile(name='Owner', discord_user_id=1, slack_user_id='OWNER')
        self.worker = UserProfile(user_key='ledger-worker', display_name='Worker', slack_user_id='WORKER')
        self.runtime = SimpleNamespace(state_store=StateStore(root/'dp.sqlite'),
            config=SimpleNamespace(slack=SlackConfig(worker_portal_beta_slack_user_ids=['OWNER','WORKER'])),
            admin_profile_by_slack_user_id=lambda sid: self.owner if sid == 'OWNER' else None,
            roster_by_slack_id={'WORKER': self.worker})
        self.tokens = {actor.slack_user_id: parse_qs(urlsplit(build_worker_portal_link(self.runtime, actor)).query)['token'][0]
                       for actor in (self.owner,self.worker)}
        self.grants = {sid: dict(projects=['p'],source_scopes=['slack:T:C']) for sid in self.tokens}
        self.store = PlanningStore(root/'planning.sqlite',owner_ref='slack:T:OWNER')
        self.store.initialize(fixture())
        self.kwargs = dict(workspace='T',allowed_origin='https://planner.example',grant_snapshot=lambda sid:self.grants.get(sid))
        app = create_dp_planning_app(self.runtime,self.store,enabled=True,**self.kwargs)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    def headers(self,sid='WORKER',**extra):
        return {'Host':'planner.example','Origin':'https://planner.example',
                'Authorization':'Bearer '+self.tokens[sid],**extra}

    async def test_real_token_handoff_cookie_and_fresh_revocation(self):
        r = await self.client.post('/planning/session',headers=self.headers())
        self.assertEqual(r.status,200)
        cookie = r.cookies['dp_planner_session']
        self.assertTrue(cookie['secure']);self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'],'Strict')
        headers={'Host':'planner.example','Cookie':'dp_planner_session='+cookie.value}
        self.assertEqual((await self.client.get('/api/planning/state',headers=headers)).status,200)
        self.grants['WORKER']=None
        self.assertEqual((await self.client.get('/api/planning/state',headers=headers)).status,401)

    async def test_inactive_worker_and_beta_removal_deny_existing_token(self):
        self.worker.active=False
        self.assertEqual((await self.client.post('/planning/session',headers=self.headers())).status,401)
        self.worker.active=True
        self.runtime.config.slack.worker_portal_beta_slack_user_ids=[]
        self.assertEqual((await self.client.get('/api/planning/state',headers=self.headers())).status,401)

    async def test_csrf_host_query_and_basic_rejected(self):
        for path,change in [('/planning/session',{'Origin':'https://evil.example'}),
                            ('/planning/session',{'Host':'evil.example'}),
                            ('/planning/session?token=anything',{}),
                            ('/planning/session',{'Authorization':'Basic shared-manager-password'})]:
            r=await self.client.post(path,headers=self.headers(**change))
            self.assertIn(r.status,(401,403))
        self.assertEqual((await self.client.get('/planning/session',headers=self.headers())).status,403)

    async def test_worker_proposes_only_owner_accepts_using_real_identities(self):
        state=await (await self.client.get('/api/planning/state',headers=self.headers())).json()
        data=dict(request_id='proposal',base_revision=1,entity='task',target='a',project='p',
                  patch={'likely':2.5},reason='Observed effort',evidence=[])
        r=await self.client.post('/api/planning/proposals',json=data,
                                 headers=self.headers(**{'X-Planning-CSRF':state['csrf']}))
        self.assertEqual(r.status,200)
        proposal=await r.json()
        decision=dict(request_id='decision',proposal_id=proposal['id'],discussion_revision=0,
                      decision='accepted',reason='Reviewed')
        r=await self.client.post('/api/planning/decisions',json=decision,headers=self.headers(**{'X-Planning-CSRF':state['csrf']}))
        self.assertEqual(r.status,403)
        owner_state=await (await self.client.get('/api/planning/state',headers=self.headers('OWNER'))).json()
        r=await self.client.post('/api/planning/decisions',json=decision,
                                headers=self.headers('OWNER',**{'X-Planning-CSRF':owner_state['csrf']}))
        self.assertEqual(r.status,200)
        self.assertEqual((await r.json())['revision'],2)

    async def test_logout_and_disabled_default(self):
        r=await self.client.post('/planning/logout',headers={'Host':'planner.example','Origin':'https://planner.example'})
        self.assertEqual(r.status,200)
        self.assertEqual(r.cookies['dp_planner_session']['max-age'],'0')
        self.assertIsNone(create_dp_planning_app(None,None,**self.kwargs))
        with self.assertRaises(ValueError):
            create_dp_planning_app(self.runtime,self.store,enabled=True,
                                   **{**self.kwargs,'allowed_origin':'http://planner.example'})
