"""Opt-in external admin proxy for the explicit Codex Web canary.

The routes in this module are installed only by ``backend.codex_canary_relay_app``.
They reuse the relay's existing authentication and its localhost/internal-token loop
proxy. Raw loop errors are never reflected to the external caller.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


MAX_ADMIN_BODY_BYTES = 4096
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_SAFE_LOOP_ERRORS = frozenset({
    "invalid_canary_request",
    "codex_generation_disabled",
    "codex_generation_unavailable",
    "codex_generation_busy",
    "codex_generation_account_unavailable",
    "codex_generation_model_unavailable",
    "codex_generation_provider_unavailable",
    "codex_generation_persona_invalid",
    "codex_generation_store_unavailable",
    "codex_generation_session_invalid",
    "codex_generation_session_conflict",
    "codex_canary_session_conflict",
    "codex_canary_session_contract_changed",
    "codex_canary_session_unavailable",
    "codex_canary_session_not_found",
})


class CodexCanaryAdminProxyError(RuntimeError):
    def __init__(self, category: str, *, status_code: int = 503):
        super().__init__(category)
        self.category = category
        self.status_code = status_code


def _session_id(value: object) -> str:
    if not isinstance(value, str) or _SESSION_ID.fullmatch(value) is None:
        raise CodexCanaryAdminProxyError("invalid_canary_request", status_code=400)
    return value


def _loop_error(exc: HTTPException) -> CodexCanaryAdminProxyError:
    detail = exc.detail if isinstance(exc.detail, str) else ""
    parsed = None
    if detail and len(detail) <= 512:
        try:
            parsed = json.loads(detail)
        except (json.JSONDecodeError, TypeError, ValueError):
            parsed = None
    category = ""
    if isinstance(parsed, dict) and set(parsed).issubset({"ok", "dispatch_uncertain", "error"}):
        candidate = parsed.get("error")
        category = candidate if isinstance(candidate, str) else ""
    elif detail in _SAFE_LOOP_ERRORS:
        category = detail
    if category not in _SAFE_LOOP_ERRORS:
        category = "codex_canary_unavailable"
    status = exc.status_code if exc.status_code in {400, 404, 409, 503} else 503
    return CodexCanaryAdminProxyError(category, status_code=status)


def _proxy(relay_module, path: str, *, method: str = "GET", body=None) -> Mapping[str, object]:
    try:
        result = relay_module.loop_json(path, method=method, body=body)
    except HTTPException as exc:
        raise _loop_error(exc) from None
    except Exception:
        raise CodexCanaryAdminProxyError("codex_canary_unavailable") from None
    if not isinstance(result, dict):
        raise CodexCanaryAdminProxyError("codex_canary_unavailable")
    return result


async def _read_create_body(request: Request) -> dict[str, object]:
    content_length = request.headers.get("content-length", "")
    if content_length:
        if not content_length.isascii() or not content_length.isdecimal():
            raise CodexCanaryAdminProxyError("invalid_canary_request", status_code=400)
        if int(content_length) > MAX_ADMIN_BODY_BYTES:
            raise CodexCanaryAdminProxyError("invalid_canary_request", status_code=400)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_ADMIN_BODY_BYTES:
            raise CodexCanaryAdminProxyError("invalid_canary_request", status_code=400)
    if not raw:
        return {}
    try:
        body = json.loads(bytes(raw))
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise CodexCanaryAdminProxyError("invalid_canary_request", status_code=400) from None
    if not isinstance(body, dict) or set(body) - {"title"}:
        raise CodexCanaryAdminProxyError("invalid_canary_request", status_code=400)
    title = body.get("title", "Codex canary")
    if not isinstance(title, str) or len(title) > 120:
        raise CodexCanaryAdminProxyError("invalid_canary_request", status_code=400)
    return {"title": title}


def _project_created(result: Mapping[str, object]) -> dict[str, object]:
    if result.get("ok") is not True or result.get("provider") != "codex":
        raise CodexCanaryAdminProxyError("codex_canary_unavailable")
    created = result.get("created")
    if not isinstance(created, dict):
        raise CodexCanaryAdminProxyError("codex_canary_unavailable")
    sid = _session_id(created.get("id"))
    title = created.get("title")
    if not isinstance(title, str) or not title or len(title) > 120:
        raise CodexCanaryAdminProxyError("codex_canary_unavailable")
    return {
        "ok": True,
        "provider": "codex",
        "created": {"api_session": sid, "title": title},
    }


def _project_status(result: Mapping[str, object], expected_session: str) -> dict[str, object]:
    if result.get("ok") is not True or result.get("provider") != "codex":
        raise CodexCanaryAdminProxyError("codex_canary_unavailable")
    session = result.get("session")
    if not isinstance(session, dict) or session.get("api_session") != expected_session:
        raise CodexCanaryAdminProxyError("codex_canary_unavailable")
    status = session.get("status")
    model = session.get("model")
    model_provider = session.get("model_provider")
    effort = session.get("reasoning_effort")
    thread_bound = session.get("thread_bound")
    if (
        status not in {"active", "retired"}
        or not isinstance(model, str)
        or not model
        or not isinstance(model_provider, str)
        or not model_provider
        or (effort is not None and not isinstance(effort, str))
        or not isinstance(thread_bound, bool)
    ):
        raise CodexCanaryAdminProxyError("codex_canary_unavailable")
    return {
        "ok": True,
        "provider": "codex",
        "session": {
            "api_session": expected_session,
            "status": status,
            "model": model,
            "model_provider": model_provider,
            "reasoning_effort": effort,
            "thread_bound": thread_bound,
        },
    }


def _project_retired(result: Mapping[str, object], expected_session: str) -> dict[str, object]:
    if result.get("ok") is not True or result.get("provider") != "api":
        raise CodexCanaryAdminProxyError("codex_canary_unavailable")
    retired = result.get("retired")
    if (
        not isinstance(retired, dict)
        or retired.get("api_session") != expected_session
        or retired.get("status") != "retired"
    ):
        raise CodexCanaryAdminProxyError("codex_canary_unavailable")
    return {
        "ok": True,
        "provider": "api",
        "retired": {"api_session": expected_session, "status": "retired"},
    }


def _error(exc: CodexCanaryAdminProxyError) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": exc.category},
        status_code=exc.status_code,
    )


def install(relay_module) -> None:
    """Install authenticated canary-admin routes on the alternate relay only."""
    if getattr(relay_module, "_CODEX_CANARY_ADMIN_PROXY_INSTALLED", False):
        return
    app = relay_module.app

    @app.post("/provider/canary/create")
    async def create_canary(request: Request):
        relay_module.check_auth(request)
        try:
            body = await _read_create_body(request)
            return _project_created(_proxy(
                relay_module,
                "/loop/provider/canary/create",
                method="POST",
                body=body,
            ))
        except CodexCanaryAdminProxyError as exc:
            return _error(exc)

    @app.get("/provider/canary/{session_id}/status")
    async def canary_status(session_id: str, request: Request):
        relay_module.check_auth(request)
        try:
            sid = _session_id(session_id)
            return _project_status(_proxy(
                relay_module,
                f"/loop/provider/canary/{sid}/status",
            ), sid)
        except CodexCanaryAdminProxyError as exc:
            return _error(exc)

    @app.post("/provider/canary/{session_id}/retire")
    async def retire_canary(session_id: str, request: Request):
        relay_module.check_auth(request)
        try:
            sid = _session_id(session_id)
            return _project_retired(_proxy(
                relay_module,
                f"/loop/provider/canary/{sid}/retire",
                method="POST",
            ), sid)
        except CodexCanaryAdminProxyError as exc:
            return _error(exc)

    relay_module._CODEX_CANARY_ADMIN_PROXY_INSTALLED = True
