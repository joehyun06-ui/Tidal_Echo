"""Explicitly installed offline adapter. Not mounted in production entrypoints.

The owner supplies a dedicated ledger DB and authoritative session resolver.
No model requests, memory access, runtime monkeypatches, or history backfill.
"""
import asyncio
import json
from fastapi import Request
from fastapi.responses import JSONResponse, Response
from .usage_ledger import LedgerError, build_view, export_records, identifier


def install(app, *, ledger, check_auth, session_provider):
    headers={'Cache-Control':'private, no-store'}
    def error(exc): return JSONResponse({'ok':False,'error':str(exc)},status_code=409 if str(exc)=='ledger_revision_conflict' else 400,headers=headers)

    def query(sid,request):
        identifier(sid)
        q=dict(request.query_params)
        if len(q)!=len(request.query_params.multi_items()) or set(q)-{'mode','scope','model','format'}: raise LedgerError('ledger_query_invalid')
        actual=session_provider(sid)
        if actual not in ('api','codex'): raise LedgerError('ledger_session_unavailable')
        mode=q.get('mode',actual)
        if mode not in ('api','codex') or q.get('scope','session') not in ('session','all'): raise LedgerError('ledger_query_invalid')
        return mode,q.get('scope')=='all',q.get('model')

    @app.get('/app/sessions/{sid}/usage-ledger')
    async def read(sid:str,request:Request):
        check_auth(request)
        try:
            mode,all_sessions,model=query(sid,request)
            result=await asyncio.to_thread(build_view,ledger,mode,sid,all_sessions=all_sessions,model=model)
            return JSONResponse(result,headers=headers)
        except LedgerError as exc: return error(exc)

    @app.get('/app/sessions/{sid}/usage-ledger/export')
    async def export(sid:str,request:Request):
        check_auth(request)
        try:
            mode,all_sessions,model=query(sid,request);format=request.query_params.get('format','json')
            rows=await asyncio.to_thread(ledger.records,mode,None if all_sessions else sid,model=model)
            data=await asyncio.to_thread(export_records,rows,format)
            return Response(data,media_type='application/json' if format=='json' else 'text/csv',headers={**headers,'Content-Disposition':f'attachment; filename="guiting-{mode}-ledger.{format}"'})
        except LedgerError as exc: return error(exc)

    @app.put('/app/usage-ledger/settings/{mode}')
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
