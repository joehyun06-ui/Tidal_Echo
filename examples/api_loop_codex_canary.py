#!/usr/bin/env python3
"""Alternate api-loop entrypoint for the explicit Codex Web canary.

Current Render startup still imports `examples.api_loop:app`; therefore merging this
module alone does not activate Codex generation. A later explicit activation change
may point the supervisor at `examples.api_loop_codex_canary:app`.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from backend.codex_canary_loop_integration import (
    CodexCanaryLoopIntegration,
    CodexCanaryLoopIntegrationError,
    build_completion_callback,
)
from backend.codex_generation_runtime import CodexGenerationRuntime
from backend.codex_generation_runtime_config import load_generation_runtime_config
from examples import api_loop as legacy


PERSISTENT_ROOT = Path(os.environ.get("RENDER_PERSISTENT_ROOT", "/var/data"))
GENERATION_CONFIG = load_generation_runtime_config(
    os.environ,
    persistent_root=PERSISTENT_ROOT,
    relay_db=Path(legacy.RELAY_DB),
)
RUNTIME = CodexGenerationRuntime(
    control_config=legacy.CODEX_CONTROL_CONFIG,
    generation_config=GENERATION_CONFIG,
    relay_db=legacy.RELAY_DB,
    persona_loader=lambda: legacy.PERSONA,
    completion_callback=build_completion_callback(legacy),
)
INTEGRATION = CodexCanaryLoopIntegration(legacy, RUNTIME)
INTEGRATION.install_legacy_globals()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Mounted sub-app lifespans are not relied upon. Run the reviewed legacy lifespan
    # explicitly, with its CODEX_CONTROL global already replaced by a no-op-close adapter.
    async with legacy.lifespan(legacy.app):
        try:
            await RUNTIME.start()
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


# All non-overridden routes remain exactly the reviewed legacy api-loop surface.
app.mount("/", legacy.app)
