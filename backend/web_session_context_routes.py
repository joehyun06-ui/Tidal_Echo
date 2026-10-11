"""Authenticated P3 settings endpoints and trusted canonical snapshot admission."""
import asyncio
import sqlite3
from urllib.parse import quote
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from . import web_session_context as context
from .web_session_provider_authority import WebSessionProviderAuthorityError


def install_loop(app, legacy, integration):
    def operation(sid, body=None, *, write=False):
        try:
            with integration._session_lock:
                if not integration.session_authority.row_for_session(sid):
                    raise context.ContextError("context_session_unavailable", 404)
                return context.save(legacy.RELAY_DB, sid, body) if write else context.get(legacy.RELAY_DB, sid)
        except context.ContextError as error:
            return JSONResponse({"ok": False, "error": error.category}, status_code=error.status)
        except WebSessionProviderAuthorityError:
            return JSONResponse({"ok": False, "error": "context_session_unavailable"}, status_code=404)
        except (sqlite3.Error, OSError, ValueError):
            return JSONResponse({"ok": False, "error": "context_storage_unavailable"}, status_code=503)

    @app.get("/loop/sessions/{session_id}/context")
    async def read(session_id: str, request: Request):
        legacy.check_internal_auth(request)
        return operation(session_id)

    @app.patch("/loop/sessions/{session_id}/context")
    async def update(session_id: str, request: Request):
        legacy.check_internal_auth(request)
        return operation(session_id, await legacy.read_internal_json(request), write=True)


def install_relay(relay):
    if getattr(relay, "_WEB_SESSION_CONTEXT_INSTALLED", False):
        return

    @relay.app.get("/app/sessions/{session_id}/context")
    async def read(session_id: str, request: Request):
        relay.check_auth(request)
        payload = await asyncio.to_thread(relay.loop_json, f"/loop/sessions/{quote(session_id, safe='')}/context")
        return JSONResponse(payload, headers={"Cache-Control": "private, no-store"})

    @relay.app.patch("/app/sessions/{session_id}/context")
    async def update(session_id: str, request: Request):
        relay.check_auth(request)
        body = await request.json()
        payload = await asyncio.to_thread(relay.loop_json, f"/loop/sessions/{quote(session_id, safe='')}/context", method="PATCH", body=body)
        return JSONResponse(payload, headers={"Cache-Control": "private, no-store"})

    original = relay.dispatch_human_message
    async def dispatch(text, *, attachments=None, api_session='', existing_message=None, route=True, extra_meta=None):
        if route and existing_message is None and api_session and relay.brain_target() == 'loop' and extra_meta and extra_meta.get('channel') == 'web' and extra_meta.get('source') == 'relay':
            try:
                snapshot = context.snapshot(relay.DB_PATH, api_session, extra_meta.get('client_timezone'))
                extra_meta = {**extra_meta, context.META_KEY: snapshot}
                extra_meta.pop('client_timezone', None)
            except (context.ContextError, sqlite3.Error, OSError):
                raise HTTPException(status_code=503, detail='context_storage_unavailable') from None
        return await original(text, attachments=attachments, api_session=api_session, existing_message=existing_message, route=route, extra_meta=extra_meta)
    relay.dispatch_human_message = dispatch
    relay._WEB_SESSION_CONTEXT_INSTALLED = True
