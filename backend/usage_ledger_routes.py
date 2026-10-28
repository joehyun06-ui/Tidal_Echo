"""Explicitly installed offline adapter. Not mounted in production entrypoints.

The owner supplies a dedicated ledger DB and authoritative session resolver.
No model requests, memory access, runtime monkeypatches, or history backfill.
"""
import asyncio
import json
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from .usage_ledger import LedgerError, build_view, identifier, model_name
from .usage_ledger_storage import page,stream_export


def install(app, *, ledger, check_auth, session_provider, message_source=None, continuations=None, manual_compactions=None, prefix='/app'):
    if prefix not in {'/app','/loop'}:raise LedgerError('ledger_route_prefix_invalid')
    headers={'Cache-Control':'private, no-store'}
    def error(exc): return JSONResponse({'ok':False,'error':str(exc)},status_code=409 if str(exc)=='ledger_revision_conflict' else 400,headers=headers)

    def query(sid,request,*,page_query=False):
        identifier(sid)
        q=dict(request.query_params)
        allowed={'mode','scope','model','format'}|({'cursor','limit'} if page_query else set())
        if len(q)!=len(request.query_params.multi_items()) or set(q)-allowed: raise LedgerError('ledger_query_invalid')
        actual=session_provider(sid)
        if actual not in ('api','codex'): raise LedgerError('ledger_session_unavailable')
        mode=q.get('mode',actual)
        if mode not in ('api','codex') or q.get('scope','session') not in ('session','all'): raise LedgerError('ledger_query_invalid')
        if q.get('model') is not None:model_name(q['model'])
        return mode,q.get('scope')=='all',q.get('model')

    @app.get(prefix+'/sessions/{sid}/usage-ledger')
    async def read(sid:str,request:Request):
        check_auth(request)
        try:
            mode,all_sessions,model=query(sid,request)
            if continuations is not None and not all_sessions:
                await asyncio.to_thread(continuations.reconcile,sid,mode)
            if manual_compactions is not None and not all_sessions:
                await asyncio.to_thread(manual_compactions.reconcile,sid,mode)
            result=await asyncio.to_thread(build_view,ledger,mode,sid,all_sessions=all_sessions,model=model,message_source=message_source)
            return JSONResponse(result,headers=headers)
        except LedgerError as exc: return error(exc)

    @app.get(prefix+'/sessions/{sid}/usage-ledger/export')
    async def export(sid:str,request:Request):
        check_auth(request)
        try:
            mode,all_sessions,model=query(sid,request);format=request.query_params.get('format','json')
            if format not in {'json','csv'}:raise LedgerError('ledger_export_invalid')
            data=stream_export(ledger,mode,None if all_sessions else sid,model=model,format=format)
            return StreamingResponse(data,media_type='application/json' if format=='json' else 'text/csv',headers={**headers,'Content-Disposition':f'attachment; filename="guiting-{mode}-ledger.{format}"'})
        except LedgerError as exc: return error(exc)

    @app.get(prefix+'/sessions/{sid}/usage-ledger/records')
    async def records(sid:str,request:Request):
        check_auth(request)
        try:
            mode,all_sessions,model=query(sid,request,page_query=True)
            limit=request.query_params.get('limit','25')
            if not limit.isascii() or not limit.isdecimal() or len(limit)>3:raise LedgerError('ledger_page_invalid')
            result=await asyncio.to_thread(page,ledger,mode,None if all_sessions else sid,model=model,limit=int(limit),cursor=request.query_params.get('cursor'))
            return JSONResponse(result,headers=headers)
        except LedgerError as exc:return error(exc)

    @app.put(prefix+'/usage-ledger/settings/{mode}')
    async def settings(mode:str,request:Request):
        check_auth(request)
        try:
            data=bytearray()
            async for part in request.stream():
                data.extend(part)
                if len(data)>8192: raise LedgerError('ledger_policy_invalid')
            try: body=json.loads(data)
            except (ValueError,UnicodeError): raise LedgerError('ledger_policy_invalid') from None
            if not isinstance(body,dict) or set(body)!={'revision','policy'}: raise LedgerError('ledger_policy_invalid')
            result=await asyncio.to_thread(ledger.save_settings,mode,body['revision'],body['policy'])
            return JSONResponse(result,headers=headers)
        except LedgerError as exc: return error(exc)
