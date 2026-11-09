"""Default-off read-only browser -> pinned loop status gateway, no bootstrap.

The host must supply independent authentication AND canonical conversation
authorization. A shared relay secret alone cannot create TrustedStatusContext.
This gateway supports one explicitly verified upstream AccessScope, not a
browser-selected principal or a multi-tenant internal-token forwarding service.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .g3_projection import _valid_id
from .g3_projection_contract import AccessScope
from .g3_projection_feed import _binding
from .session_lifecycle import LifecycleError, _session_id
from .session_lifecycle_coordination import parse
from .session_window_activation_http import PATH as INTERNAL_PATH, FIELDS
from .session_window_attempts import _hex
from .usage_ledger_proxy import loop_origin

PATH = '/app/sessions/{conversation_id}/lifecycle/activation/status'
MAX_BYTES = 4096
TIMEOUT_SECONDS = 10
HEADERS = {'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer',
           'X-Content-Type-Options': 'nosniff'}
STATUS_FIELDS = frozenset({'ok', 'state', 'historical', 'revision_applied',
    'window_confirmed_at_apply', 'current_window_confirmed', 'reply_completed', 'retry_authorized'})


@dataclass(frozen=True, slots=True, repr=False)
class TrustedStatusContext:
    """Host-authenticated scope/epochs, never constructed from browser JSON.

    auth_epoch must change for login, credential or grant changes; context_version
    for relevant conversation/session/version changes. The host must re-read both
    on every callback, including after upstream observation. These are not tokens.
    """
    access: AccessScope
    conversation_id: str
    auth_epoch: str
    context_version: str


def _response(status, category='status_unavailable', body=None):
    return JSONResponse(body if body is not None else {'ok': False, 'error': category},
                        status_code=status, headers=HEADERS)


def _valid_origin(value):
    try:
        url = urlsplit(value)
        return (type(value) is str and url.scheme in {'http', 'https'} and url.hostname
            and (url.scheme == 'https' or url.hostname in {'127.0.0.1', '::1', 'localhost'})
            and not url.username and not url.password and not url.query and not url.fragment
            and not url.path and url.geturl() == value and url.port != 0)
    except (TypeError, ValueError, AttributeError):
        return False


def _context(value, conversation_id, expected_binding):
    if type(value) is not TrustedStatusContext or type(value.access) is not AccessScope:
        raise HTTPException(503)
    try:
        binding = _binding(value.access)
    except ValueError:
        raise HTTPException(503) from None
    if binding != expected_binding or value.conversation_id != conversation_id:
        raise HTTPException(403)
    if any(type(text) is not str or not 0 < len(text) <= 256
           or any(ord(char) < 33 or ord(char) > 126 for char in text)
           for text in (value.auth_epoch, value.context_version)):
        raise HTTPException(503)
    return (binding, value.conversation_id, value.auth_epoch, value.context_version)


def _valid_status(body):
    if type(body) is not dict or set(body) != STATUS_FIELDS or body['ok'] is not True:
        return False
    if any(body[key] is not False for key in ('current_window_confirmed', 'reply_completed', 'retry_authorized')):
        return False
    if type(body['state']) is not str:
        return False
    confirmed = body['state'] == 'historical_confirmed'
    return (body['state'] in {'attempt_missing', 'unconfirmed', 'historical_confirmed'}
        and body['historical'] is confirmed and body['window_confirmed_at_apply'] is confirmed
        and (type(body['revision_applied']) is int and 0 < body['revision_applied'] <= 2**53-1
             if confirmed else body['revision_applied'] is None))


def install(app, *, enabled=False, authenticate=None, expected_access=None,
            expected_origin=None, base_url=None, internal_token=None,
            client_factory=httpx.AsyncClient):
    if enabled is not True:
        return False
    if (not isinstance(app, FastAPI) or app.middleware_stack is not None
            or not callable(authenticate) or not callable(client_factory)
            or type(expected_access) is not AccessScope or not _valid_origin(expected_origin)
            or type(internal_token) is not str or not 16 <= len(internal_token) <= 512
            or any(ord(char) < 33 or ord(char) > 126 for char in internal_token)):
        raise ValueError('lifecycle_status_proxy_binding_invalid')
    try:
        origin = loop_origin(base_url)
        expected_binding = _binding(expected_access)
    except ValueError:
        raise ValueError('lifecycle_status_proxy_binding_invalid') from None
    if any(getattr(route, 'path', None) == PATH for route in app.routes):
        raise ValueError('lifecycle_status_proxy_route_conflict')

    async def authorize(request, cid):
        context = authenticate(request, cid)
        if inspect.isawaitable(context):
            context = await context
        return _context(context, cid, expected_binding)

    async def status(request: Request, conversation_id: str):
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                # Auth precedes body parsing, dependency construction or forwarding.
                first = await authorize(request, conversation_id)
                if request.headers.getlist('origin') != [expected_origin]:
                    return _response(403, 'access_denied')
                if len(request.headers.getlist('authorization')) > 1:
                    return _response(401, 'auth_required')
                if (request.scope.get('query_string', b'') or not _session_id(conversation_id)
                        or request.headers.getlist('x-api-loop-internal-token')):
                    return _response(400, 'invalid_request')
                if (request.headers.getlist('content-type') != ['application/json']
                        or request.headers.getlist('content-encoding') not in ([], ['identity'])):
                    return _response(415, 'invalid_request')
                raw = bytearray()
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > MAX_BYTES:
                        return _response(413, 'invalid_request')
                    raw.extend(chunk)
                body = parse(bytes(raw))
                if (set(body) != FIELDS or body['conversation_id'] != conversation_id
                        or not _valid_id(body['activation_request_id'])
                        or not _valid_id(body['attempt_request_id']) or not _hex(body['packet_sha256'])):
                    return _response(400, 'invalid_request')
                if await authorize(request, conversation_id) != first:
                    return _response(403, 'access_denied')
                # Never forward browser credentials, headers, origin, URL or scope.
                async with client_factory(timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=2),
                                          trust_env=False, follow_redirects=False) as client:
                    async with client.stream('POST', origin + INTERNAL_PATH,
                            headers={'X-API-Loop-Internal-Token': internal_token,
                                     'Content-Type': 'application/json'}, json=body) as upstream:
                        code = upstream.status_code
                        if code == 200:
                            if (upstream.headers.get_list('content-type') != ['application/json']
                                    or upstream.headers.get_list('content-encoding') not in ([], ['identity'])):
                                return _response(502)
                            lengths = upstream.headers.get_list('content-length')
                            if lengths and (len(lengths) != 1 or not lengths[0].isascii()
                                    or not lengths[0].isdecimal() or len(lengths[0]) > 8
                                    or int(lengths[0]) > MAX_BYTES):
                                return _response(502)
                            data = bytearray()
                            async for chunk in upstream.aiter_bytes(chunk_size=1024):
                                if len(data) + len(chunk) > MAX_BYTES:
                                    return _response(502)
                                data.extend(chunk)
                            try:
                                result = parse(bytes(data))
                            except LifecycleError:
                                return _response(502)
                            if not _valid_status(result):
                                return _response(502)
                        else:
                            result = None
                if await authorize(request, conversation_id) != first:
                    return _response(403, 'access_denied')
                if code == 200:
                    return _response(200, body=result)
                # An internal 401 is a server-credential failure, not a browser logout.
                return _response({403: 403, 409: 409, 503: 503}.get(code, 502),
                                 'access_denied' if code == 403 else 'status_unavailable')
        except HTTPException as error:
            code = error.status_code if error.status_code in {401, 403} else 503
            return _response(code, 'auth_required' if code == 401 else
                             'access_denied' if code == 403 else 'status_unavailable')
        except LifecycleError:
            return _response(400, 'invalid_request')
        except Exception:
            return _response(503)
    app.add_api_route(PATH, status, methods=['POST'], response_class=JSONResponse)
    return True
