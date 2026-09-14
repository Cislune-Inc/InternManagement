"""Default-off DP host assembly using existing portal tokens and fresh grants.

The authenticated DP host performs the browser handoff. No token issuance,
enrollment, network listener or background polling is started here.
"""
import inspect
from urllib.parse import urlsplit

from aiohttp import web

from .planning_adapter import person_ref
from .planning_store import Principal, identifier
from .planning_web import create_planning_app


def create_dp_planning_app(runtime, store, *, enabled=False, workspace,
                           allowed_origin, grant_snapshot, people_reader=None,
                           time_reader=None):
    """Return None when disabled; caller owns TLS and listener lifecycle.

    grant_snapshot(slack_id) must return one CURRENT server-owned mapping with
    projects and source_scopes, or None when unavailable/revoked. Never cache a
    successful grant lookup as a fallback. Every request revalidates DP identity.
    """
    if enabled is not True:
        return None
    origin = urlsplit(allowed_origin)
    if (origin.scheme != 'https' or not origin.hostname or origin.username
            or origin.password or origin.path or origin.query or origin.fragment):
        raise ValueError('Worker planning requires an exact HTTPS origin.')
    identifier(workspace)
    owner_prefix = f'slack:{workspace}:'
    if not store.owner_ref.startswith(owner_prefix) or not store.owner_ref[len(owner_prefix):]:
        raise ValueError('Configure the verified acceptance owner in this Slack workspace; do not reuse a sandbox store.')
    if not callable(grant_snapshot):
        raise ValueError('Supply a fresh server-owned grant snapshot resolver.')

    async def principal(token):
        from .worker_portal import validate_worker_portal_token
        if not isinstance(token, str) or not token or len(token) > 4096:
            return None
        try:
            sid = validate_worker_portal_token(runtime, token)
            grants = grant_snapshot(sid)
            if inspect.isawaitable(grants):
                grants = await grants
            if not isinstance(grants, dict):
                return None
            projects, scopes = grants.get('projects'), grants.get('source_scopes')
            if not isinstance(projects, (list, tuple, set, frozenset)) or not isinstance(scopes, (list, tuple, set, frozenset)):
                return None
            projects = frozenset(identifier(p) for p in projects)
            scopes = frozenset(identifier(s) for s in scopes)
            if not projects:
                return None
            return Principal(person_ref(workspace, sid), projects, scopes)
        except (ValueError, TypeError, KeyError):
            return None

    def bearer(request):
        header = request.headers.get('Authorization', '')
        return header[7:] if header.startswith('Bearer ') else ''

    async def authenticate(request):
        # If an Authorization header is supplied, do not fall back to cookies.
        token = bearer(request) if 'Authorization' in request.headers else request.cookies.get('dp_planner_session', '')
        return await principal(token)

    app = create_planning_app(store, authenticate=authenticate,
                              allowed_origin=allowed_origin, people_reader=people_reader,
                              time_reader=time_reader)

    @web.middleware
    async def session_handoff(request, handler):
        if request.path not in ('/planning/session', '/planning/logout'):
            return await handler(request)
        # Exact origin prevents login/logout CSRF. Never trust forwarded headers
        # to choose the destination or put the bearer in a redirect/query URL.
        if (request.method != 'POST' or request.host.lower() != origin.netloc.lower()
                or request.headers.get('Origin') != allowed_origin or request.query_string):
            response = web.json_response({'error': 'Invalid session handoff.'}, status=403)
        elif request.path == '/planning/logout':
            response = web.json_response({'signed_out': True})
            response.del_cookie('dp_planner_session', path='/', secure=True, httponly=True, samesite='Strict')
        else:
            token = bearer(request)
            if await principal(token) is None:
                response = web.json_response({'error': 'A current Don Pollo worker session is required.'}, status=401)
            else:
                response = web.json_response({'ready': True, 'next': '/planning'})
                # Session cookie; the original token expiry/enrollment is still
                # checked on EVERY request. No renewed credential is minted.
                response.set_cookie('dp_planner_session', token, path='/',
                                    secure=True, httponly=True, samesite='Strict')
        response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                                 'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
                                 'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'"})
        return response

    app.middlewares.insert(0, session_handoff)
    return app
