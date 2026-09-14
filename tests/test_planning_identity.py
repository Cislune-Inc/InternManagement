import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from agent.planning_identity import dp_identity_resolver


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_validated_slack_identity_and_fresh_grants(self):
        grants={'projects':['p'],'sources':['slack:T:C']}
        async def projects(sid):self.assertEqual(sid,'U');return grants['projects']
        async def sources(sid):self.assertEqual(sid,'U');return grants['sources']
        resolver=dp_identity_resolver(object(),workspace='T',project_grants=projects,source_grants=sources)
        request=SimpleNamespace(headers={'Authorization':'Bearer existing-valid-token'},cookies={})
        with patch.dict(sys.modules,{'agent.worker_portal':SimpleNamespace(validate_worker_portal_token=lambda runtime,token:'U')}):
            principal=await resolver(request)
            self.assertEqual(principal.person_ref,'slack:T:U')
            self.assertEqual(principal.projects,frozenset({'p'}))
            grants['sources']=[]
            self.assertEqual((await resolver(request)).source_scopes,frozenset())

    async def test_admin_basic_and_query_parameters_do_not_become_owner(self):
        resolver=dp_identity_resolver(object(),workspace='T',project_grants=lambda _:['p'],source_grants=lambda _:[])
        request=SimpleNamespace(headers={'Authorization':'Basic arbitrary-admin-key'},cookies={},query={'actor':'OWNER'})
        with patch.dict(sys.modules,{'agent.worker_portal':SimpleNamespace(validate_worker_portal_token=lambda *_:self.fail('Basic auth must not reach token validation'))}):
            self.assertIsNone(await resolver(request))

    async def test_expired_or_disabled_worker_tokens_are_denied(self):
        resolver=dp_identity_resolver(object(),workspace='T',project_grants=lambda _:['p'],source_grants=lambda _:[])
        def reject(*_):raise ValueError('expired or no longer enrolled')
        with patch.dict(sys.modules,{'agent.worker_portal':SimpleNamespace(validate_worker_portal_token=reject)}):
            self.assertIsNone(await resolver(SimpleNamespace(headers={},cookies={'dp_planner_session':'old-token'})))


if __name__=='__main__':unittest.main()
