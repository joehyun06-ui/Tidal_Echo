"""Authenticated P3 generation reads/interrupts; no provider or thread retargeting."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .codex_generation_progress import safe_id, valid_snapshot
from .codex_generation_streaming import GenerationControlError


def error_response(error):
    return JSONResponse({"ok": False, "error": error.category}, status_code=error.status_code)


def install_loop(app, legacy, integration, runtime):
    def check(session_id):
        if not safe_id(session_id):
            raise GenerationControlError("generation_request_invalid", 400)
        if not runtime.generation_enabled:
            raise GenerationControlError("generation_disabled", 503)
        row = integration.session_authority.row_for_session(session_id)
        if row is None:
            raise GenerationControlError("generation_not_found", 404)
        if row["provider"] != "codex":
            raise GenerationControlError("generation_provider_mismatch")

    @app.get("/loop/sessions/{session_id}/generation")
    async def status(session_id: str, request: Request):
        legacy.check_internal_auth(request)
        try:
            check(session_id)
            return runtime.controls.status(session_id)
        except GenerationControlError as error:
            return error_response(error)
        except Exception:
            return error_response(GenerationControlError("generation_unavailable", 503))

    @app.post("/loop/sessions/{session_id}/generation/stop")
    async def stop(session_id: str, request: Request):
        legacy.check_internal_auth(request)
        body = await legacy.read_internal_json(request)
        try:
            check(session_id)
            if not isinstance(body, dict) or set(body) != {"generation_id"} or not safe_id(body["generation_id"]):
                raise GenerationControlError("generation_request_invalid", 400)
            return await runtime.controls.stop(session_id, body["generation_id"])
        except GenerationControlError as error:
            return error_response(error)
        except Exception:
            return error_response(GenerationControlError("generation_unavailable", 503))


def install_relay(relay):
    if getattr(relay, "_CODEX_GENERATION_CONTROLS_INSTALLED", False):
        return
    original = relay.handle_stream_delta

    async def stream_delta(kind, body):
        if body.get("snapshot") is not True:
            return await original(kind, body)
        if kind != "reply_delta" or not valid_snapshot(body):
            raise HTTPException(status_code=400, detail="generation_snapshot_invalid")
        payload = {key: body[key] for key in (
            "provider", "api_session", "generation_id", "canonical_message_id", "stream_id", "epoch", "revision", "text", "ts",
        )}
        payload["type"] = "reply_snapshot"
        await relay.broadcast(relay.app_subs, payload)
        return {"ok": True}

    relay.handle_stream_delta = stream_delta

    async def forward(session_id, body=None):
        if not safe_id(session_id):
            raise GenerationControlError("generation_request_invalid", 400)
        if body is not None and (
            not isinstance(body, dict) or set(body) != {"generation_id"} or not safe_id(body["generation_id"])
        ):
            raise GenerationControlError("generation_request_invalid", 400)
        path = f"/loop/sessions/{quote(session_id, safe='')}/generation" + ("/stop" if body is not None else "")
        try:
            payload = await asyncio.to_thread(relay.loop_json, path, method="POST" if body is not None else "GET", body=body)
        except HTTPException as error:
            # Never expose loop URLs, internal headers or upstream exception text.
            category = "generation_unavailable"
            try:
                candidate = json.loads(error.detail).get("error")
                if candidate in {"generation_disabled", "generation_not_found", "generation_not_interruptible", "generation_provider_mismatch", "generation_request_invalid"}:
                    category = candidate
            except (TypeError, ValueError, AttributeError):
                pass
            raise GenerationControlError(category, error.status_code if error.status_code in {400, 404, 409, 503} else 502) from None
        if not isinstance(payload, dict) or payload.get("ok") is not True or payload.get("contract_version") != 1 or payload.get("api_session") != session_id:
            raise GenerationControlError("generation_response_invalid", 502)
        generation = payload.get("generation")
        if generation is not None and (not isinstance(generation, dict) or not safe_id(generation.get("id"))):
            raise GenerationControlError("generation_response_invalid", 502)
        if body is not None and (not generation or generation["id"] != body["generation_id"]):
            raise GenerationControlError("generation_response_invalid", 502)
        snapshot = payload.get("snapshot")
        if snapshot is not None and (not valid_snapshot(snapshot) or snapshot["api_session"] != session_id or not generation or snapshot["generation_id"] != generation["id"]):
            raise GenerationControlError("generation_response_invalid", 502)
        return payload

    @relay.app.get("/app/sessions/{session_id}/generation")
    async def status(session_id: str, request: Request):
        relay.check_auth(request)
        try:
            return await forward(session_id)
        except GenerationControlError as error:
            return error_response(error)

    @relay.app.post("/app/sessions/{session_id}/generation/stop")
    async def stop(session_id: str, request: Request):
        relay.check_auth(request)
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 1024:
                    raise GenerationControlError("generation_request_invalid", 400)
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                raise GenerationControlError("generation_request_invalid", 400) from None
            if body is None:
                raise GenerationControlError("generation_request_invalid", 400)
            return await forward(session_id, body)
        except GenerationControlError as error:
            return error_response(error)

    relay._CODEX_GENERATION_CONTROLS_INSTALLED = True
