"""Opt-in internal HTTP candidate; no automatic installation or bootstrap."""
import asyncio
import sqlite3
import inspect
import re
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from .session_lifecycle import LifecycleError
from .session_lifecycle_commands import ConfigCommandAccess
from .session_lifecycle_coordination import parse
from .session_lifecycle_history_packet import PrivateHistorySetting
from .session_lifecycle_request_packets import PacketReference
from .session_lifecycle_history_status import inspect_history

HEADERS={'Cache-Control':'private, no-store','Referrer-Policy':'no-referrer'}
PREFIX='/loop/lifecycle/history'


def install(app, adapter, *, authenticate, enabled=False):
    """authenticate synchronously verifies the current internal header and maps
    it to trusted ConfigCommandAccess. It must not use body/query/cookie grants.
    Existing API-loop check_internal_auth can be wrapped by the server owner.
    Installing on an isolated app does not fence other writers or enable prod.
    """
    if enabled is not True:return False
    if type(adapter) is not PrivateHistorySetting or not callable(authenticate) or inspect.iscoroutinefunction(authenticate):
        raise ValueError('lifecycle_http_binding_invalid')
    paths={PREFIX+'/prepare',PREFIX+'/execute',PREFIX+'/status'}
    if any(getattr(r,'path',None) in paths for r in app.routes):
        raise ValueError('lifecycle_http_route_conflict')

    def response(body,status=200):return JSONResponse(body,status_code=status,headers=HEADERS)

    async def handle(request, operation):
        try:
            tokens=request.headers.getlist('x-api-loop-internal-token')
            if len(tokens)!=1 or not tokens[0]:return response({'ok':False,'error':'unauthorized'},401)
            access=authenticate(request)
            if type(access) is not ConfigCommandAccess:return response({'ok':False,'error':'unauthorized'},401)
            if request.headers.get('content-encoding','identity').lower() not in ('','identity'):
                return response({'ok':False,'error':'encoding_unsupported'},415)
            raw=bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw)>4096:return response({'ok':False,'error':'request_too_large'},413)
            body=parse(bytes(raw))
            if operation=='prepare':
                if set(body)!={'request_id','history_n'}:raise LifecycleError('coordination_invalid')
                ref=await asyncio.to_thread(adapter.prepare,body['history_n'],request_id=body['request_id'],access=access)
                return response({'ok':True,'request_id':ref.request_id,'packet_sha':ref.packet_sha,'state':'prepared'})
            if operation=='status':
                if set(body)!={'request_id','packet_sha'}:raise LifecycleError('coordination_invalid')
                result=await asyncio.to_thread(inspect_history,adapter,PacketReference(body['request_id'],body['packet_sha']),access=access,enabled=True)
                return response({'ok':True,**result})
            if set(body)!={'request_id','packet_sha','recover'} or type(body['recover']) is not bool:
                raise LifecycleError('coordination_invalid')
            ref=PacketReference(body['request_id'],body['packet_sha'])
            result=await asyncio.to_thread(adapter.execute,ref,access=access,recover=body['recover'])
            return response({'ok':True,'generation':result.generation,'replayed':result.replayed})
        except HTTPException as exc:
            status=403 if exc.status_code==403 else 401
            return response({'ok':False,'error':'unauthorized'},status)
        except LifecycleError as exc:
            if exc.category in ('coordination_access_denied','coordination_access_unavailable'):
                return response({'ok':False,'error':'access_denied'},403)
            if exc.category in ('coordination_invalid','coordination_history_packet_invalid','coordination_packet_invalid'):
                return response({'ok':False,'error':'invalid_request'},400)
            if exc.category in ('coordination_packet_conflict','coordination_packet_changed','coordination_request_conflict',
                                'coordination_request_missing','coordination_packet_missing','coordination_recovery_required'):
                return response({'ok':False,'error':'request_conflict'},409)
            return response({'ok':False,'error':'outcome_unconfirmed'},503)
        except (OSError,sqlite3.Error,ValueError,TypeError,RuntimeError):
            return response({'ok':False,'error':'outcome_unconfirmed'},503)

    @app.post(PREFIX+'/prepare')
    async def prepare(request:Request):return await handle(request,'prepare')

    @app.post(PREFIX+'/execute')
    async def execute(request:Request):return await handle(request,'execute')

    @app.post(PREFIX+'/status')
    async def status(request:Request):return await handle(request,'status')
    return True


def install_api_loop(loop, adapter, *, principal_id, enabled=False):
    """Explicit server assembly with the existing API-loop internal token guard.
    The stable owner ID is server configuration, never an HTTP request field.
    No call from normal API-loop startup is added by this module.
    """
    if enabled is not True:return False
    if (type(principal_id) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}',principal_id)
            or not callable(loop.check_internal_auth)):
        raise ValueError('lifecycle_http_binding_invalid')
    def authenticate(request):
        loop.check_internal_auth(request)
        return ConfigCommandAccess(principal_id)
    return install(loop.app,adapter,authenticate=authenticate,enabled=True)
