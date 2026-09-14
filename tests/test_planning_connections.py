from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from agent.models import UserProfile
from agent.planning_connections import DPPlanningConnections
from agent.planning_store import Principal


class ConnectionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.policy={'p':{'members':['U','V'],'channels':['C']},'private':{'members':['W'],'channels':['SECRET']}}
        self.check=AsyncMock(return_value=True)
        self.runtime=SimpleNamespace(slack=SimpleNamespace(can_share_work=self.check),
            roster_by_slack_id={s:UserProfile(user_key=s,display_name=s,slack_user_id=s) for s in ['U','V','W']})
        self.connections=DPPlanningConnections(self.runtime,workspace='T',policy_reader=lambda:self.policy)
    async def test_fresh_grants_and_membership_revocation(self):
        self.assertEqual(await self.connections.grants('U'),{'projects':['p'],'source_scopes':['slack:T:C']})
        self.check.return_value=False
        self.assertEqual((await self.connections.grants('U'))['source_scopes'],[])
        self.policy['p']['members']=[]
        self.assertEqual((await self.connections.grants('U'))['projects'],[])
    async def test_membership_error_never_reuses_previous_success(self):
        await self.connections.grants('U')
        self.check.side_effect=RuntimeError('Slack unavailable')
        self.assertEqual((await self.connections.grants('U'))['source_scopes'],[])
    async def test_roster_filtered_by_shared_project_and_current_active_state(self):
        actor=Principal('slack:T:U',frozenset({'p'}))
        self.assertEqual([r['name'] for r in await self.connections.people(actor)],['U','V'])
        self.runtime.roster_by_slack_id['V'].active=False
        self.assertEqual([r['name'] for r in await self.connections.people(actor)],['U'])
        self.policy['p']['members']=[]
        self.assertEqual(await self.connections.people(actor),[])
    async def test_missing_membership_capability_grants_no_sources(self):
        self.runtime.slack=SimpleNamespace()
        self.assertEqual((await self.connections.grants('U'))['source_scopes'],[])
