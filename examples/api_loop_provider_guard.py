"""Provider-aware fail-closed wrapper for the legacy API loop.

P3-B keeps ordinary production generation on the existing API provider while making
rollback semantics safe for durable Web-session provider authority introduced in
P3-A. This entrypoint owns no Codex runtime and cannot generate through Codex.

Rules:
- ordinary/unknown sessions continue through the reviewed legacy API loop;
- Web session lists project durable ``provider: api|codex`` authority;
- new sessions created here are API-authority only;
- only API-authority Web session index rows may be deleted;
- any explicit or historical Codex Web session fails closed before API model work;
- provider authority is never inferred from UI title, active-window state, local
  storage, or the presentation-level ``pinned`` bit.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from threading import RLock

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from backend import autonomous_wake_session_guard
from backend import web_session_delete
from backend import web_session_provider_authority
from examples import api_loop as legacy


_SESSION_LOCK = RLock()


def _historical_provider(session_id: str) -> str | None:
    try:
        return (
            web_session_provider_authority.CODEX_PROVIDER
            if autonomous_wake_session_guard.is_codex_web_session(
                session_id,
                os.environ,
            )
            else None
        )
    except autonomous_wake_session_guard.AutonomousWakeSessionError:
        raise web_session_provider_authority.WebSessionProviderAuthorityError(
            "web_session_provider_authority_unavailable"
        ) from None


AUTHORITY = web_session_provider_authority.WebSessionProviderAuthority(
    legacy,
    historical_provider=_historical_provider,
)


def _authority_status(category: str) -> int:
    if category in {
        "web_session_provider_invalid",
        "web_session_id_invalid",
        "web_session_title_invalid",
        "web_session_since_id_invalid",
        "web_session_created_at_invalid",
        "web_session_patch_invalid",
    }:
        return 400
    if category == "web_session_not_found":
        return 404
    if category in {
        "web_session_provider_immutable",
        "web_session_conflict",
        web_session_delete.DELETE_FORBIDDEN,
    }:
        return 409
    return 503


def _error(status_code: int, category: str) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "dispatch_uncertain": False,
            "error": category,
        },
        status_code=status_code,
    )


def _authority_error(
    error: web_session_provider_authority.WebSessionProviderAuthorityError,
) -> JSONResponse:
    return _error(_authority_status(error.category), error.category)


def _valid_session_create_body(body) -> bool:
    if not isinstance(body, dict) or set(body) - {
        "title", "since_id", "activate", "provider"
    }:
        return False
    if "title" in body and (
        not isinstance(body["title"], str) or len(body["title"]) > 120
    ):
        return False
    if "since_id" in body and (
        isinstance(body["since_id"], bool)
        or not isinstance(body["since_id"], int)
        or body["since_id"] < 0
    ):
        return False
    if "activate" in body and not isinstance(body["activate"], bool):
        return False
    if "provider" in body and body["provider"] not in {
        web_session_provider_authority.API_PROVIDER,
        web_session_provider_authority.CODEX_PROVIDER,
    }:
        return False
    return True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Mounted sub-app lifespans are not relied upon. Keep the legacy lifecycle
    # exactly once while this wrapper owns only provider-authority routing.
    async with legacy.lifespan(legacy.app):
        yield


app = FastAPI(
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/loop/sessions")
async def loop_sessions(request: Request):
    legacy.check_internal_auth(request)
    try:
        with _SESSION_LOCK:
            return AUTHORITY.sessions_public()
    except web_session_provider_authority.WebSessionProviderAuthorityError as error:
        return _authority_error(error)


@app.post("/loop/sessions")
async def loop_sessions_create(request: Request):
    legacy.check_internal_auth(request)
    body = await legacy.read_internal_json(request)
    if not _valid_session_create_body(body):
        return _error(400, "invalid_session_request")
    provider = body.get(
        "provider",
        web_session_provider_authority.API_PROVIDER,
    )
    if provider == web_session_provider_authority.CODEX_PROVIDER:
        # This entrypoint deliberately owns no Codex runtime. Creating a Codex
        # authority row here would publish a session that cannot be serviced.
        return _error(503, "codex_generation_disabled")
    try:
        with _SESSION_LOCK:
            row = AUTHORITY.create_api_session(
                title=body.get("title", "New chat"),
                since_id=body.get("since_id", 0),
                activate=body.get("activate", True),
            )
            public = AUTHORITY.sessions_public()
    except web_session_provider_authority.WebSessionProviderAuthorityError as error:
        return _authority_error(error)
    return {**public, "created": row}


@app.patch("/loop/sessions/{session_id}")
async def loop_sessions_patch(session_id: str, request: Request):
    legacy.check_internal_auth(request)
    body = await legacy.read_internal_json(request)
    try:
        with _SESSION_LOCK:
            return AUTHORITY.patch_session(session_id, body)
    except web_session_provider_authority.WebSessionProviderAuthorityError as error:
        return _authority_error(error)


@app.delete("/loop/sessions/{session_id}")
async def loop_sessions_delete(session_id: str, request: Request):
    legacy.check_internal_auth(request)
    try:
        with _SESSION_LOCK:
            return web_session_delete.delete_api_session(AUTHORITY, session_id)
    except web_session_provider_authority.WebSessionProviderAuthorityError as error:
        return _authority_error(error)


@app.get("/loop/config")
async def loop_config(request: Request):
    legacy.check_internal_auth(request)
    try:
        with _SESSION_LOCK:
            sessions = AUTHORITY.sessions_public()
        payload = legacy.public_config()
    except web_session_provider_authority.WebSessionProviderAuthorityError as error:
        return _authority_error(error)
    payload["active_session"] = sessions["active_session"]
    payload["sessions"] = sessions["sessions"]
    return payload


@app.post("/loop/ingest")
async def loop_ingest(request: Request):
    legacy.check_internal_auth(request)
    body = await legacy.read_internal_json(request)
    if not isinstance(body, dict):
        return _error(400, "invalid_body")
    text = str(body.get("text") or body.get("message") or "").strip()
    if not text:
        return _error(400, "empty_text")
    try:
        with _SESSION_LOCK:
            session_id = str(
                body.get("session_id")
                or body.get("api_session")
                or AUTHORITY.active_session_id()
                or ""
            ).strip()
            provider = AUTHORITY.provider_for_session(session_id)
    except web_session_provider_authority.WebSessionProviderAuthorityError as error:
        return _authority_error(error)
    if provider == web_session_provider_authority.CODEX_PROVIDER:
        # Critical rollback invariant: a Codex-authority Web session may not cross
        # into the API model merely because a non-Codex supervisor is active.
        return _error(503, "codex_generation_disabled")
    if provider != web_session_provider_authority.API_PROVIDER:
        return _error(503, "web_session_provider_invalid")

    msg_id = body.get("id")
    try:
        before_id = int(msg_id) if msg_id is not None else None
    except Exception:
        before_id = None
    result = await legacy.handle_ingest(
        text,
        before_id,
        session_id,
        dry=bool(body.get("dry")),
        stream_id=str(body.get("stream_id") or "").strip(),
        generation_id=str(body.get("generation_id") or "").strip(),
        reply_to=str(body.get("reply_to") or "").strip(),
        channel=str(body.get("channel") or "").strip(),
        channel_account=str(body.get("channel_account") or "").strip(),
        channel_conversation=str(body.get("channel_conversation") or "").strip(),
    )
    if result.get("ok") is not True:
        return JSONResponse(
            result,
            status_code=504 if result.get("dispatch_uncertain") else 502,
        )
    return result


# Every route not involved in Web-session authority remains the reviewed legacy API
# surface: provider control, Kelivo internal chat, health, config mutation, and other
# frozen contracts.
app.mount("/", legacy.app)
