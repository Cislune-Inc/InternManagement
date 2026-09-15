"""Live DP callbacks with explicit planner assignments and verified Slack audiences."""
import inspect
from .planning_adapter import person_ref
from .planning_store import identifier
from .planning_time import dp_time_reader


class DPPlanningConnections:
    def __init__(self,runtime,*,workspace,policy_reader):
        self.runtime=runtime
        self.workspace=identifier(workspace)
        self.policy_reader=policy_reader
        self.time_reader=dp_time_reader(runtime,workspace=workspace)

    async def policy(self):
        value=self.policy_reader()
        if inspect.isawaitable(value):value=await value
        if not isinstance(value,dict) or len(value)>100:
            raise ValueError('Provide current approved planner assignments.')
        result={}
        for project,entry in value.items():
            identifier(project)
            if not isinstance(entry,dict) or set(entry)!={'members','channels'}:
                raise ValueError('Each project requires explicit members and channels.')
            for key in ('members','channels'):
                if not isinstance(entry[key],list) or len(entry[key])>100:
                    raise ValueError('Use bounded explicit assignment lists.')
                for ref in entry[key]:identifier(ref)
            result[project]={'members':frozenset(entry['members']),'channels':frozenset(entry['channels'])}
        return result

    async def grants(self,sid):
        policy=await self.policy()
        projects={p for p,e in policy.items() if sid in e['members']}
        channels=set().union(*(e['channels'] for p,e in policy.items() if p in projects)) if projects else set()
        if len(channels)>10:
            raise ValueError('Pilot identity exceeds ten source channels; narrow the policy.')
        scopes=[]
        checker=getattr(self.runtime.slack,'can_share_work',None)
        if checker:
            for channel in sorted(channels):
                # Current DP verifies bot+person membership and rejects shared,
                # archived or inaccessible channels. No joins, invites or sends.
                try:
                    allowed=await checker(channel,sid)
                except Exception:
                    allowed=False  # Do not fall back to cached positive membership.
                if allowed:scopes.append(f'slack:{self.workspace}:{channel}')
        return {'projects':sorted(projects),'source_scopes':scopes}

    async def people(self,principal):
        policy=await self.policy()
        prefix=f'slack:{self.workspace}:'
        if not principal.person_ref.startswith(prefix):return []
        sid=principal.person_ref[len(prefix):]
        visible={p for p,e in policy.items() if p in principal.projects and sid in e['members']}
        members=set().union(*(policy[p]['members'] for p in visible)) if visible else set()
        rows=[]
        from .worker_portal import resolve_worker_portal_actor
        for member in sorted(members):
            actor=resolve_worker_portal_actor(self.runtime,member)
            if actor is not None:
                rows.append({'person_ref':person_ref(self.workspace,member),
                             'name':getattr(actor,'display_name',None) or getattr(actor,'name',member)})
        return rows
