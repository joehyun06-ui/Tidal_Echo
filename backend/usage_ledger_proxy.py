"""Default-off authenticated relay -> authoritative loop ledger forwarding.

Only fixed ledger routes to an explicitly supplied numeric loopback origin.
No browser credentials/headers are forwarded; no redirect, retry or model RPC.
"""
import asyncio
import json
import inspect
import re
from urllib.parse import urlsplit, quote, urlencode

import httpx
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from .usage_ledger import LedgerError, identifier, model_name, validate_policy, count

JSON_LIMIT = 8*1024*1024
HEADERS = {'Cache-Control': 'private, no-store'}


class ClosingStream(StreamingResponse):
    def __init__(self, *args, on_close, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_close = on_close

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Also runs when sending headers fails before body iteration starts.
            await asyncio.shield(self.on_close())


def error(status=502, category='ledger_proxy_unavailable'):
    return JSONResponse({'ok': False, 'error': category}, status_code=status, headers=HEADERS)


def loop_origin(value):
    try:
        p = urlsplit(value)
        if (p.scheme != 'http' or p.hostname not in {'127.0.0.1', '::1'} or p.username
                or p.password or p.query or p.fragment or p.path not in {'', '/'}
                or p.port is None or not 1 <= p.port <= 65535): raise ValueError()
        return f'http://[{p.hostname}]:{p.port}' if ':' in p.hostname else f'http://{p.hostname}:{p.port}'
    except (ValueError, TypeError, AttributeError):
        raise LedgerError('ledger_proxy_config_invalid') from None


def query(request, kind):
    pairs = request.query_params.multi_items(); q = dict(pairs)
    allowed = {'mode', 'scope', 'model'} | ({'cursor', 'limit'} if kind == 'records' else {'format'} if kind == 'export' else set())
    if len(q) != len(pairs) or set(q)-allowed: raise ValueError()
    if 'mode' in q and q['mode'] not in {'api', 'codex'}: raise ValueError()
    if q.get('scope', 'session') not in {'session', 'all'}: raise ValueError()
    if 'model' in q: model_name(q['model'])
    if 'cursor' in q and not 0 < len(q['cursor']) <= 4096: raise ValueError()
    if 'limit' in q and (not q['limit'].isascii() or not q['limit'].isdecimal() or len(q['limit']) > 3 or not 1 <= int(q['limit']) <= 200): raise ValueError()
    if kind == 'export' and q.get('format', 'json') not in {'json', 'csv'}: raise ValueError()
    return q


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError()
        result[key] = value
    return result


def validate_response(value, kind, sid, q):
    if not isinstance(value, dict) or value.get('ok') is not True: raise ValueError()
    mode = value.get('mode')
    if mode not in {'api', 'codex'} or 'mode' in q and mode != q['mode']: raise ValueError()
    if kind == 'settings':
        if count(value.get('revision')) is None: raise ValueError()
        validate_policy(value.get('policy'))
        return
    scope = q.get('scope', 'session')
    expected_sid = None if kind == 'records' and scope == 'all' else sid
    if value.get('contract_version') != 1 or value.get('scope') != scope or value.get('session_id') != expected_sid: raise ValueError()
    if kind == 'records':
        if value.get('model') != q.get('model') or not isinstance(value.get('records'), list): raise ValueError()
        if len(value['records']) > int(q.get('limit', '25')): raise ValueError()
        if any(not isinstance(r, dict) or r.get('mode') != mode or scope == 'session' and r.get('session_id') != sid for r in value['records']): raise ValueError()
    elif not isinstance(value.get('summary'), dict): raise ValueError()


def install(app, *, enabled=False, check_auth=None, base_url=None, internal_token=None, client_factory=httpx.AsyncClient):
    if type(enabled) is not bool: raise LedgerError('ledger_proxy_config_invalid')
    if not enabled: return False  # No config/client/app inspection in the default path.
    origin = loop_origin(base_url)
    if (not callable(check_auth) or not callable(client_factory) or not isinstance(internal_token, str)
            or not 32 <= len(internal_token) <= 512 or not internal_token.isascii()
            or any(ord(c) < 33 or ord(c) > 126 for c in internal_token)):
        raise LedgerError('ledger_proxy_config_invalid')
    paths = {'/app/sessions/{}/usage-ledger', '/app/sessions/{}/usage-ledger/records',
             '/app/sessions/{}/usage-ledger/export', '/app/usage-ledger/settings/{}'}
    if any(re.sub(r'\{[^}]+\}', '{}', getattr(r, 'path', '')) in paths for r in app.routes):
        raise LedgerError('ledger_proxy_route_conflict')

    async def forward(request, *, sid=None, kind='summary', mode=None):
        try:
            auth_result = check_auth(request)
            if inspect.isawaitable(auth_result): auth_result = await auth_result
            if auth_result is False: raise HTTPException(401)
        except HTTPException as exc:
            return error(exc.status_code if exc.status_code in {401, 403} else 401, 'ledger_auth_required')
        except Exception:
            return error(503, 'ledger_auth_unavailable')
        client = upstream = None; handed_off = False; closed = False
        async def cleanup():
            nonlocal closed
            if closed: return
            closed = True
            if upstream is not None:
                try: await upstream.aclose()
                except Exception: pass
            if client is not None:
                try: await client.aclose()
                except Exception: pass
        try:
            try:
                if kind == 'settings':
                    if mode not in {'api', 'codex'} or request.query_params: raise ValueError()
                    data = bytearray()
                    async for part in request.stream():
                        if len(data)+len(part) > 8192: return error(413, 'ledger_request_invalid')
                        data.extend(part)
                    body = json.loads(data, object_pairs_hook=unique_pairs)
                    if not isinstance(body, dict) or set(body) != {'revision', 'policy'} or count(body['revision']) is None: raise ValueError()
                    validate_policy(body['policy'])
                    q = {'mode': mode}; path = '/loop/usage-ledger/settings/'+mode
                    suffix = ''
                else:
                    identifier(sid); q = query(request, kind); body = None
                    path = '/loop/sessions/'+quote(sid, safe='')+'/usage-ledger'+('' if kind == 'summary' else '/'+kind)
                    suffix = '?'+urlencode(q) if q else ''
            except (ValueError, TypeError, UnicodeError, RecursionError):
                return error(400, 'ledger_request_invalid')
            client = client_factory(timeout=httpx.Timeout(35, connect=5), trust_env=False, follow_redirects=False)
            req = client.build_request(request.method, origin+path+suffix,
                                       headers={'X-API-Loop-Internal-Token': internal_token}, json=body)
            upstream = await client.send(req, stream=True)
            if upstream.status_code != 200:
                status = upstream.status_code if upstream.status_code in {400, 404, 409, 410, 413, 422, 429, 503, 504} else 502
                return error(status)
            content_type = upstream.headers.get('content-type', '').split(';', 1)[0].strip().lower()
            if kind == 'export':
                fmt = q.get('format', 'json'); expected = 'text/csv' if fmt == 'csv' else 'application/json'
                if content_type != expected: return error()
                async def chunks():
                    try:
                        async for chunk in upstream.aiter_bytes(chunk_size=65536): yield chunk
                    except asyncio.CancelledError: raise
                    except Exception:
                        raise RuntimeError('ledger_proxy_stream_interrupted') from None
                    finally:
                        await asyncio.shield(cleanup())
                response = ClosingStream(chunks(), media_type=expected, on_close=cleanup,
                    headers={**HEADERS, 'Content-Disposition': f'attachment; filename="guiting-ledger.{fmt}"'},
                    background=BackgroundTask(cleanup))
                handed_off = True
                return response
            if content_type != 'application/json': return error()
            data = bytearray()
            async for chunk in upstream.aiter_bytes(chunk_size=65536):
                if len(data)+len(chunk) > JSON_LIMIT: return error()
                data.extend(chunk)
            try:
                value = json.loads(data, object_pairs_hook=unique_pairs)
                validate_response(value, kind, sid, q)
            except (ValueError, TypeError, UnicodeError, RecursionError):
                return error()
            return JSONResponse(value, headers=HEADERS)
        except httpx.TimeoutException:
            return error(504)
        except Exception:
            return error()
        finally:
            if not handed_off: await asyncio.shield(cleanup())

    @app.get('/app/sessions/{sid}/usage-ledger')
    async def summary(sid: str, request: Request): return await forward(request, sid=sid)
    @app.get('/app/sessions/{sid}/usage-ledger/records')
    async def records(sid: str, request: Request): return await forward(request, sid=sid, kind='records')
    @app.get('/app/sessions/{sid}/usage-ledger/export')
    async def export(sid: str, request: Request): return await forward(request, sid=sid, kind='export')
    @app.put('/app/usage-ledger/settings/{mode}')
    async def settings(mode: str, request: Request): return await forward(request, mode=mode, kind='settings')
    return True
