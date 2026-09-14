"""Reuse DP's existing, verified worker identity; no token issuance or enrollment.

The host owns its TLS/session bootstrap and current project/channel grant lookup.
An admin Basic credential is deliberately NOT mapped to the acceptance owner.
"""
from __future__ import annotations
import inspect

from .planning_adapter import person_ref
from .planning_store import Principal


def dp_identity_resolver(runtime, *, workspace, project_grants, source_grants):
    if not callable(project_grants) or not callable(source_grants):
        raise ValueError('Provide server-owned, current project and source grant resolvers.')

    async def resolve(request):
        from .worker_portal import validate_worker_portal_token
        # Session bootstrap belongs to the existing authenticated host. Never take
        # identity, grants, manager flags or new credentials from query parameters.
        header=request.headers.get('Authorization','')
        token=header[7:] if header.startswith('Bearer ') else request.cookies.get('dp_planner_session','')
        if not token or len(token)>4096:
            return None
        try:
            sid=validate_worker_portal_token(runtime,token)
        except (ValueError,TypeError,KeyError):
            return None
        projects=project_grants(sid)
        sources=source_grants(sid)
        if inspect.isawaitable(projects):projects=await projects
        if inspect.isawaitable(sources):sources=await sources
        return Principal(person_ref(workspace,sid),frozenset(projects),frozenset(sources))
    return resolve
