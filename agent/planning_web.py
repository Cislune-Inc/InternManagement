"""Authenticated, audience-projected planning routes for a host-owned identity.

No default production authenticator: the embedding service must supply one that
revalidates identity, project and source grants on every request. The isolated
local review executable is deliberately separate from deployment.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
import secrets
import sqlite3

from aiohttp import web

from .planning_store import PlanningError, Principal

ASSETS=Path(__file__).with_name('portfolio_assets')
PRINCIPAL_KEY=getattr(web,'RequestKey',web.AppKey)('planning_principal',Principal)


def create_planning_app(store, *, authenticate, allowed_origin, sandbox=False, time_reader=None, people_reader=None, mode_label=None):
    if not callable(authenticate):
        raise ValueError('A server-owned identity resolver is required.')
    # Host and origin must be explicitly configured, never derived from forwarded headers.
    from urllib.parse import urlsplit
    origin=urlsplit(allowed_origin)
    if origin.scheme not in ('http','https') or not origin.netloc or origin.path or origin.query or origin.fragment or origin.username:
        raise ValueError('Configure an exact HTTP(S) origin.')
    csrf_secret=secrets.token_bytes(32)

    def csrf(actor):
        import hmac, hashlib
        return hmac.new(csrf_secret,actor.person_ref.encode(),hashlib.sha256).hexdigest()

    @web.middleware
    async def boundary(request, handler):
        try:
            if request.host.lower()!=origin.netloc.lower():
                raise PlanningError('Use the configured planner address.',403)
            actor=authenticate(request)
            if inspect.isawaitable(actor): actor=await actor
            if not isinstance(actor,Principal) or not actor.person_ref:
                raise PlanningError('Sign in through the configured company identity.',401)
            request[PRINCIPAL_KEY]=actor
            if request.method not in ('GET','HEAD'):
                if request.headers.get('Origin')!=allowed_origin or not secrets.compare_digest(request.headers.get('X-Planning-CSRF',''),csrf(actor)):
                    raise PlanningError('Reload this page before saving a change.',403)
                if request.content_type!='application/json':
                    raise PlanningError('Send JSON to this endpoint.',415)
            response=await handler(request)
        except PlanningError as exc:
            response=web.json_response({'error':str(exc)},status=exc.status)
        except (json.JSONDecodeError, UnicodeDecodeError):
            response=web.json_response({'error':'Invalid JSON request.'},status=400)
        except (TypeError, ValueError):
            response=web.json_response({'error':'Invalid planning request values.'},status=400)
        except sqlite3.Error:
            response=web.json_response({'error':'Planning storage is unavailable. Keep your text and retry.'},status=503)
        except web.HTTPException as exc:
            response=web.json_response({'error':exc.reason},status=exc.status)
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
            'X-Frame-Options':'DENY','Referrer-Policy':'no-referrer',
            'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})
        return response

    app=web.Application(middlewares=[boundary],client_max_size=32_000)

    async def page(request):
        return web.Response(text=(ASSETS/'collaboration.html').read_text(),content_type='text/html')

    async def asset(request):
        name=request.match_info['name']
        if name not in ('collaboration.js','collaboration.css','schedule.js'):
            raise web.HTTPNotFound()
        return web.Response(body=(ASSETS/name).read_bytes(),content_type='text/css' if name.endswith('.css') else 'text/javascript')

    async def state(request):
        actor=request[PRINCIPAL_KEY]
        result=store.view(actor)
        result.update(csrf=csrf(actor),sandbox=sandbox,time={'connected':False})
        result['mode_label']=mode_label
        result['people']=await people(actor)
        if time_reader:
            summary=time_reader(actor)
            if inspect.isawaitable(summary): summary=await summary
            if summary:
                # Only the current actor's host-provided, actual ledger summary.
                result['time']={k:v for k,v in summary.items() if k in ('clock_state','as_of','recorded_seconds_today','source','unresolved')}
                result['time']['connected']=True
        return web.json_response(result)

    async def people(actor):
        rows=people_reader(actor) if people_reader else [{'person_ref':actor.person_ref,'name':'You'}]
        if inspect.isawaitable(rows):rows=await rows
        return [{'person_ref':r['person_ref'],'name':r['name']} for r in rows]

    operations={'proposals':store.proposal,'discussion':store.discuss,'resolve':store.resolve,
                'decisions':store.decide,'links':store.link_source,'recaps':store.recap}
    async def mutate(request):
        operation=request.match_info['operation']
        if operation not in operations: raise web.HTTPNotFound()
        data=await request.json()
        if not isinstance(data,dict): raise PlanningError('Expected a JSON object.')
        # Reject missing/extra fields before calling the service, including spoofed role/actor.
        fn=operations[operation]
        expected=set(inspect.signature(fn).parameters)-{'actor'}
        if set(data)!=expected: raise PlanningError('Missing or unsupported request fields.')
        if operation=='proposals' and isinstance(data.get('patch'),dict) and data['patch'].get('owner_ref'):
            if data['patch']['owner_ref'] not in {p['person_ref'] for p in await people(request[PRINCIPAL_KEY])}:
                raise PlanningError('Select a person from the verified roster.',400)
        return web.json_response(fn(request[PRINCIPAL_KEY],**data))

    app.router.add_get('/',page)
    app.router.add_get('/planning',page)
    app.router.add_get('/planning-assets/{name}',asset)
    app.router.add_get('/api/planning/state',state)
    app.router.add_post('/api/planning/{operation}',mutate)
    return app
