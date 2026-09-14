import json
from pathlib import Path
import tempfile
import unittest
from aiohttp.test_utils import TestClient,TestServer
from agent.planning_store import PlanningStore,Principal
from agent.planning_web import create_planning_app
from test_planning_store import fixture,source


class PlanningWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.owner=Principal('slack:T:OWNER',frozenset({'p','q'}),frozenset({'slack:T:C'}))
        self.worker=Principal('slack:T:WORKER',frozenset({'p'}),frozenset({'slack:T:C'}))
        self.actors={'owner':self.owner,'worker':self.worker}
        self.store=PlanningStore(Path(self.tmp.name)/'planning.sqlite',owner_ref=self.owner.person_ref)
        self.store.initialize(fixture())
        def authenticate(request):return self.actors.get(request.headers.get('X-Test-Actor'))
        def time_reader(actor):
            return dict(clock_state='clocked_in',as_of='2026-09-14T12:00:00Z',recorded_seconds_today=3600,
                        source='test ledger',private_salary='never expose',person_ref=actor.person_ref)
        self.app=create_planning_app(self.store,authenticate=authenticate,allowed_origin='http://127.0.0.1:8877',time_reader=time_reader)
        self.client=TestClient(TestServer(self.app));await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close();self.tmp.cleanup()

    def headers(self,actor='worker',**extra):
        return {'Host':'127.0.0.1:8877','X-Test-Actor':actor,**extra}

    async def state(self,actor='worker'):
        response=await self.client.get('/api/planning/state',headers=self.headers(actor))
        self.assertEqual(response.status,200)
        return await response.json()

    async def post(self,path,payload,actor='worker',**headers):
        state=await self.state(actor)
        return await self.client.post('/api/planning/'+path,json=payload,headers=self.headers(actor,Origin='http://127.0.0.1:8877',**{'X-Planning-CSRF':state['csrf'],**headers}))

    def proposal(self,**changes):
        return dict(request_id='test-proposal',base_revision=1,entity='task',target='a',project='p',patch={'likely':2.5},reason='Test new estimate',evidence=[],**changes)

    async def test_no_identity_and_bad_host_are_denied(self):
        r=await self.client.get('/api/planning/state',headers={'Host':'127.0.0.1:8877'})
        self.assertEqual(r.status,401)
        r=await self.client.get('/',headers=self.headers(Host='evil.test'))
        self.assertEqual(r.status,403)

    async def test_origin_and_actor_bound_csrf(self):
        state=await self.state()
        r=await self.client.post('/api/planning/proposals',json=self.proposal(),headers=self.headers())
        self.assertEqual(r.status,403)
        r=await self.client.post('/api/planning/proposals',json=self.proposal(),headers=self.headers('owner',Origin='http://127.0.0.1:8877',**{'X-Planning-CSRF':state['csrf']}))
        self.assertEqual(r.status,403)
        r=await self.client.post('/api/planning/proposals',json=self.proposal(),headers=self.headers(Origin='http://evil.test',**{'X-Planning-CSRF':state['csrf']}))
        self.assertEqual(r.status,403)

    async def test_worker_cannot_spoof_actor_or_acceptance(self):
        r=await self.post('proposals',self.proposal(actor='slack:T:OWNER'))
        self.assertEqual(r.status,400)
        r=await self.post('proposals',self.proposal());pid=(await r.json())['id']
        decision=dict(request_id='d1',proposal_id=pid,discussion_revision=0,decision='accepted',reason='Reviewed')
        r=await self.post('decisions',decision);self.assertEqual(r.status,403)
        r=await self.post('decisions',decision,'owner');self.assertEqual(r.status,200)
        self.assertEqual((await self.state())['revision'],2)

    async def test_source_grants_revalidated_without_new_login(self):
        self.store.ingest_source(source())
        self.assertEqual(len((await self.state())['sources']),1)
        self.actors['worker']=Principal(self.worker.person_ref,self.worker.projects)
        self.assertEqual((await self.state())['sources'],[])

    async def test_time_projection_and_no_privileged_routes(self):
        state=await self.state()
        self.assertEqual(state['time']['recorded_seconds_today'],3600)
        self.assertNotIn('private_salary',json.dumps(state))
        for path in ['/payroll','/api/clock','/data/planning.sqlite','/planning-assets/planning.sqlite']:
            r=await self.client.get(path,headers=self.headers());self.assertEqual(r.status,404)

    async def test_http_retries_and_durable_restart(self):
        first=await (await self.post('proposals',self.proposal())).json()
        second=await (await self.post('proposals',self.proposal())).json()
        self.assertEqual(first,second)
        restored=PlanningStore(self.store.path,owner_ref=self.owner.person_ref)
        self.assertEqual(len(restored.view(self.worker)['proposals']),1)

    async def test_script_markup_stays_data_and_assets_have_csp(self):
        p=self.proposal();p['patch']={'title':'</script><script>alert(1)</script>'}
        r=await self.post('proposals',p);self.assertEqual(r.status,200)
        r=await self.client.get('/',headers=self.headers())
        self.assertNotIn('alert(1)',await r.text())
        self.assertIn("script-src 'self'",r.headers['Content-Security-Policy'])
        self.assertEqual(r.headers['Cache-Control'],'no-store')

    async def test_invalid_and_oversized_payloads(self):
        for p in ([],{'actor':'owner'}):
            r=await self.post('proposals',p);self.assertEqual(r.status,400)
        r=await self.post('proposals',None);self.assertEqual(r.status,415)
        p=self.proposal();p['patch']={'likely':float('nan')}
        r=await self.post('proposals',p);self.assertEqual(r.status,400)
        p=self.proposal();p['reason']='x'*40000
        r=await self.post('proposals',p);self.assertEqual(r.status,413)


if __name__=='__main__':unittest.main()
