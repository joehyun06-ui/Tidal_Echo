"""Narrow context actions. Session authority and provider gates stay server-owned."""
import asyncio
import json
import sqlite3
from urllib.parse import quote

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse

from .web_codex_compaction import CompactionError, request_id
from .codex_generation_progress import safe_id
from .web_session_provider_authority import WebSessionProviderAuthorityError
from .web_continuation import preview
from .usage_ledger_creation import CreationError


def error(category, status):
    return JSONResponse({'ok':False,'error':category},status_code=status,headers={'Cache-Control':'private, no-store'})


def install_loop(app, legacy, integration, runtime, *, continuation_creator=None):
    def session(sid):
        if not safe_id(sid): raise CompactionError('context_session_unavailable',404)
        row=integration.session_authority.row_for_session(sid)
        if not row: raise CompactionError('context_session_unavailable',404)
        return row

    async def operation(sid, request, *, action=None):
        legacy.check_internal_auth(request)
        try:
            body=await legacy.read_internal_json(request) if action in {'start','reconcile'} else None
            with integration._session_lock:
                row=session(sid)
                if action=='preview': return continuation_creator.preview(sid) if continuation_creator is not None else preview(legacy.RELAY_DB,sid,row['title'])
                if row['provider']!='codex' or not runtime.generation_enabled:
                    if action: raise CompactionError('compaction_not_supported',409)
                    return {'ok':True,'contract_version':1,'api_session':sid,'supported':False,'can_start':False,'operation':None}
                return runtime.compactions.reserve(sid,body,reconcile=action=='reconcile') if action else runtime.compactions.status(sid)
        except CompactionError as exc: return error(exc.category,exc.status)
        except CreationError as exc: return error(exc.category,exc.status)
        except WebSessionProviderAuthorityError: return error('context_session_unavailable',404)
        except (sqlite3.Error,OSError,ValueError,TypeError): return error('context_action_unavailable',503)

    @app.get('/loop/sessions/{session_id}/compact')
    async def status(session_id:str,request:Request): return await operation(session_id,request)
    @app.post('/loop/sessions/{session_id}/compact')
    async def start(session_id:str,request:Request): return await operation(session_id,request,action='start')
    @app.post('/loop/sessions/{session_id}/compact/reconcile')
    async def reconcile(session_id:str,request:Request): return await operation(session_id,request,action='reconcile')
    @app.get('/loop/sessions/{session_id}/continuation')
    async def continuation(session_id:str,request:Request): return await operation(session_id,request,action='preview')

    @app.post('/loop/sessions/{session_id}/continuation')
    async def create_continuation(session_id:str,request:Request):
        legacy.check_internal_auth(request)
        if continuation_creator is None: return error('continuation_registration_unavailable',409)
        try:
            with integration._session_lock: session(session_id)
            body=await read_continuation(request)
            return await continuation_creator.create(session_id,body)
        except CreationError as exc: return error(exc.category,exc.status)
        except (OSError,sqlite3.Error,ValueError,TypeError,WebSessionProviderAuthorityError): return error('continuation_creation_unavailable',503)


def install_relay(relay):
    async def forward(sid,request,suffix):
        relay.check_auth(request)
        if not safe_id(sid): return error('context_session_unavailable',404)
        body=None
        if request.method=='POST' and suffix=='continuation':
            try: body=await read_continuation(request)
            except CreationError as exc: return error(exc.category,exc.status)
        elif request.method=='POST':
            data=bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data)>512: return error('compaction_request_invalid',400)
            try:
                body=json.loads(data);request_id(body)
            except (ValueError,UnicodeError,CompactionError): return error('compaction_request_invalid',400)
        try:
            payload=await asyncio.to_thread(relay.loop_json,f'/loop/sessions/{quote(sid,safe="")}/{suffix}',method=request.method,body=body)
            if not isinstance(payload,dict) or payload.get('ok') is not True or payload.get('api_session')!=sid:
                return error('context_action_unavailable',502)
            return JSONResponse(payload,headers={'Cache-Control':'private, no-store'})
        except HTTPException as exc:
            return error('context_action_unavailable',exc.status_code if exc.status_code in {400,404,409,503} else 502)

    @relay.app.get('/app/sessions/{session_id}/compact')
    async def status(session_id:str,request:Request): return await forward(session_id,request,'compact')
    @relay.app.post('/app/sessions/{session_id}/compact')
    async def start(session_id:str,request:Request): return await forward(session_id,request,'compact')
    @relay.app.post('/app/sessions/{session_id}/compact/reconcile')
    async def reconcile(session_id:str,request:Request): return await forward(session_id,request,'compact/reconcile')
    @relay.app.get('/app/sessions/{session_id}/continuation')
    async def continuation(session_id:str,request:Request): return await forward(session_id,request,'continuation')
    @relay.app.post('/app/sessions/{session_id}/continuation')
    async def create_continuation(session_id:str,request:Request): return await forward(session_id,request,'continuation')


async def read_continuation(request):
    data=bytearray()
    async for part in request.stream():
        data.extend(part)
        if len(data)>160*1024: raise CreationError('continuation_request_invalid',400)
    try: return json.loads(data)
    except (ValueError,UnicodeError): raise CreationError('continuation_request_invalid',400) from None
