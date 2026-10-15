"""Authenticated inventory only; no outbound connections or native config writes."""
import asyncio
import json
import sqlite3
from fastapi import Request
from fastapi.responses import JSONResponse
from . import web_mcp_settings as settings
from . import web_mcp_inspection as inspection


def install(relay):
    async def handle(request):
        relay.check_auth(request)
        headers={'Cache-Control':'private, no-store'}
        try:
            if request.method=='GET': value=await asyncio.to_thread(settings.read,relay.DB_PATH)
            else:
                data=bytearray()
                async for chunk in request.stream():
                    data.extend(chunk)
                    if len(data)>262144: raise settings.SettingsError('mcp_config_too_large',413)
                try: body=json.loads(data)
                except (ValueError,UnicodeError): raise settings.SettingsError('mcp_config_invalid') from None
                value=await asyncio.to_thread(settings.save,relay.DB_PATH,body)
            return JSONResponse(value,headers=headers)
        except settings.SettingsError as exc:
            return JSONResponse({'ok':False,'error':exc.category},status_code=exc.status,headers=headers)
        except (sqlite3.Error,OSError,ValueError,TypeError):
            return JSONResponse({'ok':False,'error':'mcp_settings_unavailable'},status_code=503,headers=headers)

    @relay.app.get('/app/mcp/settings')
    async def read(request:Request): return await handle(request)
    @relay.app.put('/app/mcp/settings')
    async def save(request:Request): return await handle(request)

    @relay.app.post('/app/mcp/plugins/{plugin_id}/inspect')
    async def inspect(plugin_id:str, request:Request):
        relay.check_auth(request)
        headers={'Cache-Control':'private, no-store'}
        try:
            data=bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data)>1024: raise settings.SettingsError('mcp_config_too_large',413)
            try: body=json.loads(data)
            except (ValueError,UnicodeError): raise settings.SettingsError('mcp_config_invalid') from None
            # DNS is system-resolver bounded; the worker retains its concurrency slot
            # after a client timeout, so repeated slow lookups cannot grow workers.
            value=await asyncio.wait_for(asyncio.to_thread(inspection.inspect,relay.DB_PATH,plugin_id,body),18)
            return JSONResponse(value,headers=headers)
        except settings.SettingsError as exc:
            return JSONResponse({'ok':False,'error':exc.category},status_code=exc.status,headers=headers)
        except (TimeoutError,sqlite3.Error,OSError,ValueError,TypeError):
            return JSONResponse({'ok':False,'error':'mcp_inspection_unavailable'},status_code=503,headers=headers)
