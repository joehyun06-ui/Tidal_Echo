"""Authenticated, no-store budget observations; no inference or mutation RPC."""
import asyncio
import sqlite3
from urllib.parse import quote

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse

from .codex_generation_progress import safe_id
from .web_context_budget import read
from .web_session_provider_authority import WebSessionProviderAuthorityError


def install_loop(app, legacy, integration, runtime):
    @app.get("/loop/sessions/{session_id}/context-budget")
    async def budget(session_id: str, request: Request):
        legacy.check_internal_auth(request)
        if not safe_id(session_id):
            return JSONResponse({"ok": False, "error": "context_session_unavailable"}, status_code=404)
        try:
            with integration._session_lock:
                session = integration.session_authority.row_for_session(session_id)
                if not session or session["provider"] not in {"api", "codex"}:
                    return JSONResponse({"ok": False, "error": "context_session_unavailable"}, status_code=404)
                return read(legacy.RELAY_DB, session_id, session["provider"], epoch=runtime.context_budget.epoch)
        except WebSessionProviderAuthorityError:
            return JSONResponse({"ok": False, "error": "context_session_unavailable"}, status_code=404)
        except (sqlite3.Error, OSError, ValueError, TypeError):
            return JSONResponse({"ok": False, "error": "context_budget_unavailable"}, status_code=503)


def install_relay(relay):
    @relay.app.get("/app/sessions/{session_id}/context-budget")
    async def budget(session_id: str, request: Request):
        relay.check_auth(request)
        headers = {"Cache-Control": "private, no-store"}
        if not safe_id(session_id):
            return JSONResponse({"ok": False, "error": "context_session_unavailable"}, status_code=404, headers=headers)
        try:
            payload = await asyncio.to_thread(relay.loop_json, f"/loop/sessions/{quote(session_id, safe='')}/context-budget")
        except HTTPException as error:
            status = error.status_code if error.status_code in {404, 503} else 502
            return JSONResponse({"ok": False, "error": "context_session_unavailable" if status == 404 else "context_budget_unavailable"}, status_code=status, headers=headers)
        if not isinstance(payload, dict) or payload.get("ok") is not True or payload.get("contract_version") != 1 or payload.get("api_session") != session_id:
            return JSONResponse({"ok": False, "error": "context_budget_unavailable"}, status_code=502, headers=headers)
        return JSONResponse(payload, headers=headers)
