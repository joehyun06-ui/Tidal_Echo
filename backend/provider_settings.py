"""Authenticated API settings and narrow loopback proxy; secrets stay server-side."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

ERRORS = {
    "provider_settings_invalid": 400,
    "provider_key_required": 400,
    "provider_settings_unavailable": 503,
    "provider_auth_rejected": 400,
    "provider_models_unavailable": 503,
    "codex_generation_disabled": 503,
    "codex_generation_busy": 409,
    "codex_session_unavailable": 409,
    "codex_model_invalid": 400,
}


def fail(category: str):
    raise HTTPException(status_code=ERRORS[category], detail=category)


def text(value, maximum=512):
    if not isinstance(value, str) or not value or len(value) > maximum or value != value.strip():
        fail("provider_settings_invalid")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        fail("provider_settings_invalid")
    return value


def base_url(value):
    value = text(value, 2048).rstrip("/")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        valid_scheme = parsed.scheme == "https" or (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        )
        if (not valid_scheme or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or "\\" in value):
            raise ValueError
    except ValueError:
        fail("provider_settings_invalid")
    return value


def api_config(legacy):
    routes = legacy.main_chain()
    if len(routes) != 1:
        fail("provider_settings_unavailable")
    route = routes[0]
    return {"url": route.get("url", ""), "model": route.get("model", ""),
            "has_key": bool(route.get("key")), "scope": "all_api_sessions"}


def save_api_config(legacy, body):
    if not isinstance(body, dict) or set(body) - {"url", "key", "model"} or not {"url", "model"} <= set(body):
        fail("provider_settings_invalid")
    url, model = base_url(body["url"]), text(body["model"])
    routes = legacy.main_chain()
    previous = routes[0] if len(routes) == 1 else {}
    key = body.get("key")
    if key is None or key == "":
        # Never silently forward an existing credential to a newly supplied URL.
        if url != str(previous.get("url", "")).rstrip("/") or not previous.get("key"):
            fail("provider_key_required")
        key = previous["key"]
    else:
        key = text(key, 8192)
    try:
        legacy.update_config({"main_chain": [{"url": url, "key": key, "model": model}]})
    except Exception:
        fail("provider_settings_unavailable")
    return {"ok": True, **api_config(legacy)}


async def api_models(legacy):
    routes = legacy.main_chain()
    if len(routes) != 1 or not routes[0].get("key"):
        fail("provider_settings_unavailable")
    route = routes[0]
    url = base_url(route["url"])
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", url + "/models", headers={
                "Authorization": "Bearer " + route["key"], "Accept": "application/json",
            }) as response:
                if response.status_code in {401, 403}:
                    fail("provider_auth_rejected")
                if response.status_code != 200:
                    fail("provider_models_unavailable")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 1024 * 1024:
                        fail("provider_models_unavailable")
        payload = json.loads(raw)
        data = payload.get("data")
        if not isinstance(data, list) or len(data) > 4096:
            fail("provider_models_unavailable")
        models = []
        for item in data:
            if not isinstance(item, dict):
                continue
            model = item.get("id")
            try:
                text(model)
            except HTTPException:
                continue
            if model not in models:
                models.append(model)
        return {"models": models}
    except HTTPException:
        raise
    except Exception:
        fail("provider_models_unavailable")


async def read_body(request):
    if request.headers.get("content-encoding", "identity") not in {"", "identity"}:
        fail("provider_settings_invalid")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 16384:
            fail("provider_settings_invalid")
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        fail("provider_settings_invalid")


def install_loop(app, legacy):
    @app.get("/loop/provider/api/config")
    async def get_config(request: Request):
        legacy.check_internal_auth(request)
        return api_config(legacy)

    @app.post("/loop/provider/api/config")
    async def set_config(request: Request):
        legacy.check_internal_auth(request)
        return save_api_config(legacy, await read_body(request))

    @app.get("/loop/provider/api/models")
    async def get_models(request: Request):
        legacy.check_internal_auth(request)
        return await api_models(legacy)


async def proxy(relay, path, method="GET", body=None):
    try:
        return await run_in_threadpool(relay.loop_json, path, method=method, body=body)
    except HTTPException as exc:
        detail = exc.detail
        try:
            if isinstance(detail, str) and detail not in ERRORS:
                detail = json.loads(detail).get("detail")
        except (ValueError, AttributeError):
            detail = None
        if not isinstance(detail, str) or detail not in ERRORS:
            detail = "provider_settings_unavailable"
        fail(detail)
    except Exception:
        fail("provider_settings_unavailable")


def install_relay(app, relay):
    @app.get("/app/provider/api/config")
    async def get_config(request: Request):
        relay.check_auth(request)
        return await proxy(relay, "/loop/provider/api/config")

    @app.post("/app/provider/api/config")
    async def set_config(request: Request):
        relay.check_auth(request)
        return await proxy(relay, "/loop/provider/api/config", "POST", await read_body(request))

    @app.get("/app/provider/api/models")
    async def get_models(request: Request):
        relay.check_auth(request)
        return await proxy(relay, "/loop/provider/api/models")

    @app.get("/app/sessions/{session_id}/model")
    async def get_model(session_id: str, request: Request):
        relay.check_auth(request)
        from .codex_session_model import valid_session_id
        valid_session_id(session_id)
        return await proxy(relay, f"/loop/sessions/{session_id}/model")

    @app.post("/app/sessions/{session_id}/model")
    async def set_model(session_id: str, request: Request):
        relay.check_auth(request)
        from .codex_session_model import valid_session_id
        valid_session_id(session_id)
        return await proxy(relay, f"/loop/sessions/{session_id}/model", "POST", await read_body(request))
