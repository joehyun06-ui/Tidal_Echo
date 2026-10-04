"""Authenticated internal routes for conversation version copies."""
from fastapi import Request
from fastapi.responses import JSONResponse
import sqlite3
from .web_session_fork import ForkError, fork_conversation
from .web_session_provider_authority import WebSessionProviderAuthorityError


def install(app, legacy, authority, lock):
    @app.post("/loop/sessions/{session_id}/fork")
    async def fork_session(session_id: str, request: Request):
        legacy.check_internal_auth(request)
        body = await legacy.read_internal_json(request)
        try:
            with lock:
                return fork_conversation(authority, session_id, body, relay_db=legacy.RELAY_DB)
        except ForkError as error:
            return JSONResponse({"ok": False, "error": error.category}, status_code=error.status)
        except WebSessionProviderAuthorityError:
            return JSONResponse({"ok": False, "error": "fork_authority_unavailable"}, status_code=409)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return JSONResponse({"ok": False, "error": "fork_storage_unavailable"}, status_code=503)
