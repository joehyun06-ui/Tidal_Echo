"""Explicit internal historical status route; no startup or action endpoint."""
import asyncio
import inspect
import sqlite3

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .g3_projection_contract import AccessScope
from .g3_projection_feed import _binding
from .session_lifecycle import LifecycleError
from .session_lifecycle_coordination import parse
from .session_lifecycle_storage import SQLiteLifecycleRepository
from .session_window_activation_status import inspect_recorded_activation
from .session_window_attempts import WindowAttemptJournal

PATH = '/loop/lifecycle/activation/status'
HEADERS = {'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer'}
FIELDS = {'conversation_id', 'activation_request_id', 'attempt_request_id', 'packet_sha256'}


def install(app, journal, *, authenticate, enabled=False):
    if enabled is not True:
        return False
    if (not isinstance(app, FastAPI) or type(journal) is not WindowAttemptJournal
            or not journal.enabled or type(journal.repository) is not SQLiteLifecycleRepository
            or not journal.repository.enabled or not callable(authenticate)
            or inspect.iscoroutinefunction(authenticate) or app.middleware_stack is not None):
        raise ValueError('lifecycle_status_http_binding_invalid')
    if any(getattr(route, 'path', None) == PATH for route in app.routes):
        raise ValueError('lifecycle_status_http_route_conflict')

    def response(body, status=200):
        return JSONResponse(body, status_code=status, headers=HEADERS)

    @app.post(PATH)
    async def status(request: Request):
        try:
            tokens = request.headers.getlist('x-api-loop-internal-token')
            if len(tokens) != 1 or not tokens[0]:
                return response({'ok': False, 'error': 'unauthorized'}, 401)
            access = authenticate(request)
            if type(access) is not AccessScope:
                return response({'ok': False, 'error': 'unauthorized'}, 401)
            if request.scope.get('query_string', b''):
                return response({'ok': False, 'error': 'invalid_request'}, 400)
            encodings = request.headers.getlist('content-encoding')
            if len(encodings) > 1 or (encodings and encodings[0].lower() not in ('', 'identity')):
                return response({'ok': False, 'error': 'encoding_unsupported'}, 415)
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 4096:
                    return response({'ok': False, 'error': 'request_too_large'}, 413)
                raw.extend(chunk)
            body = parse(bytes(raw))
            if set(body) != FIELDS:
                return response({'ok': False, 'error': 'invalid_request'}, 400)
            result = await asyncio.to_thread(inspect_recorded_activation, journal,
                body['conversation_id'], body['activation_request_id'],
                attempt_request_id=body['attempt_request_id'], packet_sha256=body['packet_sha256'],
                access=access, enabled=True)
            return response({'ok': True, **result})
        except HTTPException as exc:
            return response({'ok': False, 'error': 'unauthorized'}, 403 if exc.status_code == 403 else 401)
        except LifecycleError as exc:
            if exc.category == 'lifecycle_access_denied':
                return response({'ok': False, 'error': 'access_denied'}, 403)
            if exc.category in ('coordination_invalid', 'lifecycle_invalid', 'lifecycle_attempt_invalid'):
                return response({'ok': False, 'error': 'invalid_request'}, 400)
            if exc.category in ('lifecycle_attempt_request_conflict', 'lifecycle_activation_status_conflict'):
                return response({'ok': False, 'error': 'request_conflict'}, 409)
            return response({'ok': False, 'error': 'status_unavailable'}, 503)
        except (OSError, sqlite3.Error, ValueError, TypeError, RuntimeError):
            return response({'ok': False, 'error': 'status_unavailable'}, 503)
    return True


def install_api_loop(loop, journal, *, access, enabled=False):
    """Server-owned scope + existing internal-header guard; no startup call."""
    if enabled is not True:
        return False
    if type(access) is not AccessScope or not callable(getattr(loop, 'check_internal_auth', None)):
        raise ValueError('lifecycle_status_http_binding_invalid')
    _binding(access)
    def authenticate(request):
        loop.check_internal_auth(request)
        return access
    return install(loop.app, journal, authenticate=authenticate, enabled=True)
