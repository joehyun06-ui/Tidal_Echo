"""Authenticated session selection, action history and one-use decisions."""
import asyncio
import json
import re
from urllib.parse import quote
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from .web_mcp_chat import Store
from .web_mcp_settings import SettingsError

HEADERS = {'Cache-Control': 'private, no-store'}
ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$')


async def body_of(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 8192:
            raise SettingsError('mcp_config_too_large', 413)
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise SettingsError('mcp_config_invalid') from None


def response_error(exc):
    return JSONResponse({'ok': False, 'error': exc.category}, status_code=exc.status, headers=HEADERS)


def install_loop(app, legacy, integration):
    store = Store(legacy.RELAY_DB)
    def operation(sid, kind, method, body, aid):
        with integration._session_lock:
            if not ID.fullmatch(sid):
                raise SettingsError('mcp_config_invalid')
            row = integration.session_authority.row_for_session(sid)
            if row is None:
                raise SettingsError('mcp_session_not_found', 404)
            if row['provider'] != 'api':
                if method != 'GET':
                    raise SettingsError('mcp_provider_not_supported', 409)
                return {'ok': True, 'contract_version': 1, 'api_session': sid, 'supported': False, 'actions': []}
            if kind == 'actions':
                value = store.actions(sid) if method == 'GET' else store.decision(sid, aid, body)
            else:
                value = store.read(sid) if method == 'GET' else store.save(sid, body)
            return {**value, 'supported': True}

    async def handle(sid, request, kind='settings', aid=None):
        legacy.check_internal_auth(request)
        try:
            body = None if request.method == 'GET' else await body_of(request)
            value = await asyncio.to_thread(operation, sid, kind, request.method, body, aid)
            return JSONResponse(value, headers=HEADERS)
        except SettingsError as exc:
            return response_error(exc)
        except Exception:
            return response_error(SettingsError('mcp_chat_unavailable', 503))

    @app.api_route('/loop/sessions/{session_id}/mcp', methods=['GET', 'PUT'])
    async def config(session_id: str, request: Request):
        return await handle(session_id, request)
    @app.get('/loop/sessions/{session_id}/mcp/actions')
    async def actions(session_id: str, request: Request):
        return await handle(session_id, request, 'actions')
    @app.post('/loop/sessions/{session_id}/mcp/actions/{action_id}')
    async def decide(session_id: str, action_id: str, request: Request):
        return await handle(session_id, request, 'actions', action_id)


def install_relay(relay):
    async def handle(sid, request, suffix=''):
        relay.check_auth(request)
        try:
            if not ID.fullmatch(sid):
                raise SettingsError('mcp_config_invalid')
            body = None if request.method == 'GET' else await body_of(request)
            value = await asyncio.to_thread(relay.loop_json, f"/loop/sessions/{quote(sid, safe='')}/mcp" + suffix,
                                           method=request.method, body=body)
            if not isinstance(value, dict) or value.get('ok') is not True or value.get('api_session') != sid or value.get('contract_version') != 1:
                raise SettingsError('mcp_chat_unavailable', 502)
            return JSONResponse(value, headers=HEADERS)
        except SettingsError as exc:
            return response_error(exc)
        except HTTPException as exc:
            # Preserve actionable status, never upstream URLs, headers or bodies.
            return response_error(SettingsError('mcp_chat_conflict' if exc.status_code == 409 else 'mcp_chat_unavailable',
                                                exc.status_code if exc.status_code in (400, 404, 409, 413, 503) else 502))
        except Exception:
            return response_error(SettingsError('mcp_chat_unavailable', 503))

    @relay.app.api_route('/app/sessions/{session_id}/mcp', methods=['GET', 'PUT'])
    async def config(session_id: str, request: Request):
        return await handle(session_id, request)
    @relay.app.get('/app/sessions/{session_id}/mcp/actions')
    async def actions(session_id: str, request: Request):
        return await handle(session_id, request, '/actions')
    @relay.app.post('/app/sessions/{session_id}/mcp/actions/{action_id}')
    async def decide(session_id: str, action_id: str, request: Request):
        return await handle(session_id, request, '/actions/' + quote(action_id, safe=''))
