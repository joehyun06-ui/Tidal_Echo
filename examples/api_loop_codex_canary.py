#!/usr/bin/env python3
"""Alternate api-loop entrypoint for explicit provider-aware Web sessions."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from backend import codex_generation_observability, codex_generation_store
from backend import memory_formation_v2_loopback
from backend import memory_hierarchy_refinement_loopback
from backend import memory_hierarchy_summary_loopback_v2
from backend import web_session_delete, web_session_provider_authority
from backend.codex_canary_loop_integration import (
    CodexCanaryLoopIntegrationError,
    build_completion_callback,
)
from backend.codex_generation_live_reliability import FailClosedCodexCanaryLoopIntegration
from backend.codex_generation_subscription_reliability import (
    ResubscribingCodexGenerationRuntime,
)
from backend.codex_generation_runtime_config import load_generation_runtime_config
from examples import api_loop as legacy


PERSISTENT_ROOT = Path(os.environ.get("RENDER_PERSISTENT_ROOT", "/var/data"))
GENERATION_CONFIG = load_generation_runtime_config(
    os.environ,
    persistent_root=PERSISTENT_ROOT,
    relay_db=Path(legacy.RELAY_DB),
)
RUNTIME = ResubscribingCodexGenerationRuntime(
    control_config=legacy.CODEX_CONTROL_CONFIG,
    generation_config=GENERATION_CONFIG,
    relay_db=legacy.RELAY_DB,
    persona_loader=lambda: legacy.PERSONA,
    completion_callback=build_completion_callback(legacy),
)
INTEGRATION = FailClosedCodexCanaryLoopIntegration(legacy, RUNTIME)
INTEGRATION.install_legacy_globals()


def _upload_dir() -> Path | None:
    raw = str(os.environ.get("RELAY_UPLOAD_DIR", "")).strip()
    return Path(raw) if raw else None


def _public_sessions() -> dict:
    return web_session_delete.public_session_state(
        INTEGRATION.session_authority,
        relay_db=legacy.RELAY_DB,
        codex_store=GENERATION_CONFIG.store_path,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async with legacy.lifespan(legacy.app):
        try:
            await RUNTIME.start()
            if RUNTIME.generation_enabled:
                codex_generation_observability.log_latest_job_snapshot(
                    GENERATION_CONFIG.store_path
                )
                codex_generation_observability.log_recent_ingress_receipt(
                    legacy.RELAY_DB,
                    GENERATION_CONFIG.store_path,
                )
            yield
        finally:
            await RUNTIME.close()


app = FastAPI(
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def _error(exc: CodexCanaryLoopIntegrationError) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "dispatch_uncertain": False,
            "error": exc.category,
        },
        status_code=exc.status_code,
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
    if category == "web_session_deleted":
        return 410
    if category in {
        "web_session_provider_immutable",
        "web_session_conflict",
        web_session_delete.DELETE_FORBIDDEN,
        web_session_delete.DELETE_REQUIRES_RETIREMENT,
        web_session_delete.DELETE_JOB_ACTIVE,
    }:
        return 409
    return 503


def _authority_error(
    exc: web_session_provider_authority.WebSessionProviderAuthorityError,
) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "dispatch_uncertain": False, "error": exc.category},
        status_code=_authority_status(exc.category),
    )


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
    if "provider" in body and body["provider"] not in {"api", "codex"}:
        return False
    return True


@app.get("/loop/sessions")
async def loop_sessions(request: Request):
    legacy.check_internal_auth(request)
    try:
        return _public_sessions()
    except web_session_provider_authority.WebSessionProviderAuthorityError as exc:
        return _authority_error(exc)


@app.post("/loop/sessions")
async def loop_sessions_create(request: Request):
    legacy.check_internal_auth(request)
    body = await legacy.read_internal_json(request)
    if not _valid_session_create_body(body):
        return JSONResponse(
            {"ok": False, "error": "invalid_session_request"},
            status_code=400,
        )
    try:
        row = await INTEGRATION.create_web_session(
            provider=body.get("provider", "api"),
            title=body.get("title", "New chat"),
            since_id=body.get("since_id", 0),
            activate=body.get("activate", True),
        )
        public = _public_sessions()
    except CodexCanaryLoopIntegrationError as exc:
        return _error(exc)
    except web_session_provider_authority.WebSessionProviderAuthorityError as exc:
        return _authority_error(exc)
    return {**public, "created": row}


@app.patch("/loop/sessions/{session_id}")
async def loop_sessions_patch(session_id: str, request: Request):
    legacy.check_internal_auth(request)
    body = await legacy.read_internal_json(request)
    try:
        INTEGRATION.patch_web_session(session_id, body)
        return _public_sessions()
    except CodexCanaryLoopIntegrationError as exc:
        return _error(exc)
    except web_session_provider_authority.WebSessionProviderAuthorityError as exc:
        return _authority_error(exc)


@app.delete("/loop/sessions/{session_id}")
async def loop_sessions_delete(session_id: str, request: Request):
    legacy.check_internal_auth(request)
    try:
        with INTEGRATION._session_lock:
            return web_session_delete.delete_conversation(
                INTEGRATION.session_authority,
                session_id,
                relay_db=legacy.RELAY_DB,
                upload_dir=_upload_dir(),
                codex_store=GENERATION_CONFIG.store_path,
            )
    except web_session_provider_authority.WebSessionProviderAuthorityError as exc:
        return _authority_error(exc)


@app.post("/loop/ingest")
async def loop_ingest(request: Request):
    legacy.check_internal_auth(request)
    body = await legacy.read_internal_json(request)
    try:
        result = await INTEGRATION.handle_ingest(body)
    except CodexCanaryLoopIntegrationError as exc:
        return _error(exc)
    if result.get("ok") is not True:
        return JSONResponse(
            result,
            status_code=504 if result.get("dispatch_uncertain") else 502,
        )
    return result


@app.post(memory_formation_v2_loopback.ENDPOINT)
async def loop_memory_formation_v2(request: Request):
    return await memory_formation_v2_loopback.handle_request(legacy, request)


@app.post(memory_hierarchy_refinement_loopback.ENDPOINT)
async def loop_memory_hierarchy_refinement(request: Request):
    return await memory_hierarchy_refinement_loopback.handle_request(legacy, request)


@app.post(memory_hierarchy_summary_loopback_v2.ENDPOINT)
async def loop_memory_hierarchy_summary_v2(request: Request):
    return await memory_hierarchy_summary_loopback_v2.handle_request(legacy, request)


@app.post("/loop/provider/canary/create")
async def create_canary(request: Request):
    legacy.check_internal_auth(request)
    body = await legacy.read_internal_json(request)
    if not isinstance(body, dict) or set(body) - {"title"}:
        return JSONResponse(
            {"ok": False, "error": "invalid_canary_request"}, status_code=400
        )
    title = body.get("title", "Codex canary")
    if not isinstance(title, str) or len(title) > 120:
        return JSONResponse(
            {"ok": False, "error": "invalid_canary_request"}, status_code=400
        )
    try:
        row = await INTEGRATION.create_canary_session(title=title)
    except CodexCanaryLoopIntegrationError as exc:
        return _error(exc)
    return {"ok": True, "provider": "codex", "created": row}


@app.get("/loop/provider/canary/{session_id}/status")
async def canary_status(session_id: str, request: Request):
    legacy.check_internal_auth(request)
    if not RUNTIME.generation_enabled:
        return _error(CodexCanaryLoopIntegrationError(
            "codex_generation_disabled", status_code=503
        ))
    try:
        row = codex_generation_store.get_session(
            GENERATION_CONFIG.store_path,
            session_id,
        )
    except codex_generation_store.CodexGenerationStoreError as exc:
        status = 400 if exc.category == "codex_generation_session_invalid" else 503
        return _error(CodexCanaryLoopIntegrationError(exc.category, status_code=status))
    if row is None:
        return _error(CodexCanaryLoopIntegrationError(
            "codex_canary_session_not_found", status_code=404
        ))
    return {
        "ok": True,
        "provider": "codex",
        "session": {
            "api_session": row["api_session"],
            "status": row["status"],
            "model": row["model"],
            "model_provider": row["model_provider"],
            "reasoning_effort": row["reasoning_effort"],
            "thread_bound": row["thread_id"] is not None,
        },
    }


@app.post("/loop/provider/canary/{session_id}/retire")
async def retire_canary(session_id: str, request: Request):
    legacy.check_internal_auth(request)
    try:
        row = INTEGRATION.retire_canary_session(session_id)
    except CodexCanaryLoopIntegrationError as exc:
        return _error(exc)
    return {
        "ok": True,
        "provider": "api",
        "retired": {
            "api_session": row["api_session"],
            "status": row["status"],
        },
    }


app.mount("/", legacy.app)
