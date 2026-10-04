"""Authenticated internal routes for conversation version copies."""
from fastapi import Request
from fastapi.responses import JSONResponse
import sqlite3
import asyncio
from .web_session_fork import ForkError, fork_conversation
from .web_session_provider_authority import WebSessionProviderAuthorityError


def install(app, legacy, authority, lock, *, runtime=None):
    fork_lock = asyncio.Lock()
    @app.post("/loop/sessions/{session_id}/fork")
    async def fork_session(session_id: str, request: Request):
        legacy.check_internal_auth(request)
        body = await legacy.read_internal_json(request)
        try:
            if runtime is not None and authority.provider_for_session(session_id) == "codex":
                from .web_codex_fork import fork_codex_conversation
                async with fork_lock:
                    return await fork_codex_conversation(
                        authority, session_id, body, relay_db=legacy.RELAY_DB,
                        runtime=runtime, lock=lock,
                    )
            with lock:
                return fork_conversation(authority, session_id, body, relay_db=legacy.RELAY_DB)
        except ForkError as error:
            return JSONResponse({"ok": False, "error": error.category}, status_code=error.status)
        except WebSessionProviderAuthorityError:
            return JSONResponse({"ok": False, "error": "fork_authority_unavailable"}, status_code=409)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return JSONResponse({"ok": False, "error": "fork_storage_unavailable"}, status_code=503)
