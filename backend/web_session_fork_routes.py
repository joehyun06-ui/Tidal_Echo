"""Authenticated internal routes for conversation version copies."""
from fastapi import Request
from fastapi.responses import JSONResponse
import sqlite3
import asyncio
import os
from .web_session_fork import ForkError, fork_conversation
from . import web_message_versions as versions
from .web_session_provider_authority import WebSessionProviderAuthorityError
from .codex_generation_protocol import CodexGenerationError
from .codex_generation_store import CodexGenerationStoreError


def install(app, legacy, authority, lock, *, runtime=None):
    fork_lock = asyncio.Lock()
    async def perform(session_id, body, *, version=False):
        try:
            async with fork_lock:
                if version:
                    with lock:
                        edge = versions.prepare(authority, session_id, body, relay_db=legacy.RELAY_DB)
                if runtime is not None and authority.provider_for_session(session_id) == "codex":
                    from .web_codex_fork import fork_codex_conversation
                    result = await fork_codex_conversation(
                        authority, session_id, body, relay_db=legacy.RELAY_DB,
                        runtime=runtime, lock=lock,
                    )
                else:
                    with lock:
                        result = fork_conversation(authority, session_id, body, relay_db=legacy.RELAY_DB)
                if version:
                    with lock:
                        versions.finish(authority, edge['version_id'], relay_db=legacy.RELAY_DB, select=True)
                    result['version'] = edge
                return result
        except ForkError as error:
            return JSONResponse({"ok": False, "error": error.category}, status_code=error.status)
        except WebSessionProviderAuthorityError:
            return JSONResponse({"ok": False, "error": "fork_authority_unavailable"}, status_code=409)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return JSONResponse({"ok": False, "error": "fork_storage_unavailable"}, status_code=503)

    @app.post("/loop/sessions/{session_id}/fork")
    async def fork_session(session_id: str, request: Request):
        legacy.check_internal_auth(request)
        return await perform(session_id, await legacy.read_internal_json(request))

    @app.post("/loop/sessions/{session_id}/versions")
    async def new_version(session_id: str, request: Request):
        legacy.check_internal_auth(request)
        return await perform(session_id, await legacy.read_internal_json(request), version=True)

    def version_call(operation):
        try:
            with lock:
                return operation()
        except ForkError as error:
            return JSONResponse({"ok": False, "error": error.category}, status_code=error.status)
        except WebSessionProviderAuthorityError as error:
            return JSONResponse({"ok": False, "error": error.category}, status_code=409)
        except CodexGenerationStoreError:
            return JSONResponse({"ok": False, "error": "codex_session_unavailable"}, status_code=409)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return JSONResponse({"ok": False, "error": "version_storage_unavailable"}, status_code=503)

    @app.get("/loop/conversation-versions")
    async def list_versions(request: Request):
        legacy.check_internal_auth(request)
        return version_call(lambda: versions.public_state(authority, relay_db=legacy.RELAY_DB))

    @app.post("/loop/sessions/{session_id}/version-selection")
    async def version_selection(session_id: str, request: Request):
        legacy.check_internal_auth(request)
        body = await legacy.read_internal_json(request)
        return version_call(lambda: versions.select_version(authority, session_id, body, relay_db=legacy.RELAY_DB))

    @app.delete("/loop/conversations/{session_id}")
    async def delete_versions(session_id: str, request: Request):
        legacy.check_internal_auth(request)
        operation = lambda: versions.delete_versions(authority, session_id, relay_db=legacy.RELAY_DB,
            upload_dir=os.environ.get('RELAY_UPLOAD_DIR') or None,
            codex_store=runtime.config.store_path if runtime else None)
        async with fork_lock:
            if runtime is not None:
                try:
                    async with runtime.foundation.activity_gate.control():
                        return version_call(operation)
                except CodexGenerationError:
                    return JSONResponse({"ok": False, "error": "web_session_delete_job_active"}, status_code=409)
            return version_call(operation)
