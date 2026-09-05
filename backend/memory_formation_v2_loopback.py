"""Strict localhost-only provider adapter for Atomic Memory Formation V2.

The relay sends only the exact canonical source text to the api-loop. The api-loop
owns provider/model resolution and invokes the V2 extractor contract internally,
then returns only signal classes and source ranges. No candidate plaintext,
session identity, provider output, Memory store, or persistence authority crosses
this boundary.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import os
import sys
import urllib.parse
from dataclasses import asdict
from typing import Final

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

from backend import (
    deployment_config,
    memory_formation_extractor_v2,
    memory_formation_v2,
)


ENDPOINT: Final = "/loop/memory/formation-v2"
CLIENT_RESPONSE_MAX_BYTES: Final = 16 * 1024
CLIENT_TIMEOUT_SECONDS: Final = memory_formation_extractor_v2.EXTRACTOR_TIMEOUT_SECONDS + 5.0
_GPT56_CHAT_REASONING_NONE_MODELS: Final = frozenset({
    "[Pro按量]gpt-5.6-sol",
})
_SAFE_CHAT_FINISH_REASONS: Final = frozenset({
    "stop",
    "length",
    "content_filter",
    "tool_calls",
    "function_call",
})
_REASONING_EFFORT_CONTEXT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "memory_formation_v2_reasoning_effort",
    default=None,
)
_DIAGNOSTIC_ACTIVE_CONTEXT: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "memory_formation_v2_diagnostic_active",
    default=False,
)
_FINISH_REASON_CONTEXT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "memory_formation_v2_finish_reason",
    default="missing",
)
_REASONING_PATCH_MARKER: Final = "_MEMORY_FORMATION_V2_REASONING_EFFORT_PATCHED"
_JSON_DIAGNOSTIC_PATCH_MARKER: Final = "_MEMORY_FORMATION_V2_JSON_DIAGNOSTIC_PATCHED"

_ERROR_CATEGORIES: Final = frozenset({
    "extractor_invalid_output",
    "extractor_timeout",
    "extractor_unavailable",
    "invalid_source_text",
    "loopback_invalid_response",
    "loopback_unavailable",
})


class MemoryFormationV2LoopbackError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "loopback_unavailable"
        )
        self.category = safe
        super().__init__(safe)


def _raise(category: str) -> None:
    raise MemoryFormationV2LoopbackError(category)


def _endpoint_from_ingest(ingest_url: object) -> str:
    raw = str(ingest_url or "")
    parsed = urllib.parse.urlsplit(raw)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        _raise("loopback_unavailable")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, ENDPOINT, "", ""))


def _safe_chat_finish_reason(value: object) -> str:
    if type(value) is str and value in _SAFE_CHAT_FINISH_REASONS:
        return value
    if value is None:
        return "missing"
    return "other"


class _JsonLoadsDiagnosticProxy:
    """Observe only safe Chat Completions metadata while V2 extraction is active."""

    __slots__ = ("_target",)

    def __init__(self, target: object):
        self._target = target

    def loads(self, *args, **kwargs):
        payload = self._target.loads(*args, **kwargs)
        if _DIAGNOSTIC_ACTIVE_CONTEXT.get() and isinstance(payload, dict):
            choices = payload.get("choices")
            first = choices[0] if isinstance(choices, list) and choices else None
            if isinstance(first, dict) and "finish_reason" in first:
                _FINISH_REASON_CONTEXT.set(
                    _safe_chat_finish_reason(first.get("finish_reason"))
                )
        return payload

    def __getattr__(self, name: str):
        return getattr(self._target, name)


def _install_reasoning_effort_hook(legacy: object) -> None:
    """Add one context-local Chat Completions hint for the V2 extractor only."""

    if getattr(legacy, _REASONING_PATCH_MARKER, False):
        return
    original = getattr(legacy, "_chat_completion_body", None)
    if not callable(original):
        return

    @functools.wraps(original)
    def wrapped(
        route,
        messages,
        *,
        stream,
        temperature=None,
        max_tokens=None,
    ):
        body = original(
            route,
            messages,
            stream=stream,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if (
            _REASONING_EFFORT_CONTEXT.get() == "none"
            and isinstance(route, dict)
            and route.get("model") in _GPT56_CHAT_REASONING_NONE_MODELS
            and isinstance(body, dict)
        ):
            body = dict(body)
            body["reasoning_effort"] = "none"
        return body

    legacy._chat_completion_body = wrapped
    setattr(legacy, _REASONING_PATCH_MARKER, True)


def _install_finish_reason_hook(legacy: object) -> None:
    """Observe a bounded finish_reason without retaining provider response text."""

    if getattr(legacy, _JSON_DIAGNOSTIC_PATCH_MARKER, False):
        return
    target = getattr(legacy, "json", None)
    if not callable(getattr(target, "loads", None)):
        return
    legacy.json = _JsonLoadsDiagnosticProxy(target)
    setattr(legacy, _JSON_DIAGNOSTIC_PATCH_MARKER, True)


def _log_extractor_diagnostic(
    failure_stage: object,
    finish_reason: object,
) -> None:
    stage = (
        failure_stage
        if (
            type(failure_stage) is str
            and failure_stage in memory_formation_extractor_v2._FAILURE_STAGES
        )
        else "unknown"
    )
    safe_finish_reason = _safe_chat_finish_reason(finish_reason)
    print(
        "[memory-formation-v2-extractor-diagnostic] "
        "status=failed category=extractor_invalid_output "
        f"failure_stage={stage} finish_reason={safe_finish_reason}",
        file=sys.stderr,
        flush=True,
    )


def _serialize_extraction(
    extraction: memory_formation_extractor_v2.AutoMemoryExtractionV2,
) -> dict:
    return {
        "ok": True,
        "version": memory_formation_extractor_v2.EXTRACTOR_CONTRACT_VERSION,
        "proposals": [
            {
                "signal_type": proposal.signal_type,
                "spans": [asdict(span) for span in proposal.spans],
            }
            for proposal in extraction.proposals
        ],
    }


def _parse_extraction_payload(
    payload: object,
    *,
    source_length: int,
) -> memory_formation_extractor_v2.AutoMemoryExtractionV2:
    if (
        type(payload) is not dict
        or set(payload) != {"ok", "version", "proposals"}
        or payload.get("ok") is not True
        or payload.get("version")
        != memory_formation_extractor_v2.EXTRACTOR_CONTRACT_VERSION
    ):
        _raise("loopback_invalid_response")
    try:
        raw = json.dumps(
            {
                "version": payload["version"],
                "proposals": payload["proposals"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return memory_formation_extractor_v2._parse_model_output(
            raw,
            source_length,
        )
    except memory_formation_extractor_v2.MemoryFormationExtractorV2Error as error:
        raise MemoryFormationV2LoopbackError("loopback_invalid_response") from error
    except (TypeError, ValueError, UnicodeError):
        _raise("loopback_invalid_response")


async def run_server_extraction(
    legacy: object,
    source_text: object,
) -> memory_formation_extractor_v2.AutoMemoryExtractionV2:
    """Run one strict V2 extraction using the api-loop's frozen primary provider."""

    if type(source_text) is not str:
        _raise("invalid_source_text")
    try:
        provider_defaults = await asyncio.to_thread(
            deployment_config.resolve_kelivo_provider_contract_defaults,
            os.environ,
            legacy.LOOP_CONFIG,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _raise("extractor_unavailable")

    _install_reasoning_effort_hook(legacy)
    _install_finish_reason_hook(legacy)
    provider_finish_reason = "missing"

    async def generation_callable(
        messages,
        session_id,
        provider_model,
        temperature,
        max_tokens,
        context,
    ):
        nonlocal provider_finish_reason
        if (
            session_id != memory_formation_extractor_v2.EXTRACTOR_SESSION_ID
            or provider_model != provider_defaults.provider_model
            or temperature != memory_formation_extractor_v2.EXTRACTOR_TEMPERATURE
            or max_tokens > memory_formation_extractor_v2.EXTRACTOR_MAX_TOKENS
            or context
            != {
                "prompt_contract_version": legacy.kelivo_service.PROMPT_CONTRACT_VERSION
                if hasattr(legacy, "kelivo_service")
                else "kelivo-provider-prompt-v1",
                "memory_formation_extractor": (
                    memory_formation_extractor_v2.EXTRACTOR_CONTRACT_VERSION
                ),
                "memory_formation_contract": memory_formation_v2.FORMATION_CONTRACT_VERSION,
            }
        ):
            raise RuntimeError("invalid v2 extractor dispatch")
        effort = (
            "none"
            if provider_model in _GPT56_CHAT_REASONING_NONE_MODELS
            else None
        )
        effort_token = _REASONING_EFFORT_CONTEXT.set(effort)
        diagnostic_token = _DIAGNOSTIC_ACTIVE_CONTEXT.set(True)
        finish_token = _FINISH_REASON_CONTEXT.set("missing")
        try:
            out = await legacy.run_kelivo_provider_contract(
                provider_model,
                list(messages),
                temperature=float(temperature),
                max_tokens=int(max_tokens),
            )
        finally:
            provider_finish_reason = _FINISH_REASON_CONTEXT.get()
            _FINISH_REASON_CONTEXT.reset(finish_token)
            _DIAGNOSTIC_ACTIVE_CONTEXT.reset(diagnostic_token)
            _REASONING_EFFORT_CONTEXT.reset(effort_token)
        if not isinstance(out, dict) or out.get("outcome") != "success":
            if isinstance(out, dict) and out.get("error") == "model_timeout":
                raise TimeoutError
            raise RuntimeError("provider dispatch unavailable")
        return {"text": str(out.get("text") or "")}

    try:
        return await memory_formation_extractor_v2.extract_auto_memory_proposals_v2(
            generation_callable,
            source_text,
            provider_model=provider_defaults.provider_model,
            provider_prompt_contract_version="kelivo-provider-prompt-v1",
        )
    except asyncio.CancelledError:
        raise
    except memory_formation_extractor_v2.MemoryFormationExtractorV2Error as error:
        if error.category == "extractor_invalid_output":
            _log_extractor_diagnostic(error.stage, provider_finish_reason)
        if error.category in {
            "extractor_invalid_output",
            "extractor_timeout",
            "extractor_unavailable",
            "invalid_source_text",
        }:
            raise MemoryFormationV2LoopbackError(error.category) from None
        _raise("extractor_unavailable")


async def handle_request(legacy: object, request: Request):
    """FastAPI localhost handler with fixed, data-free error projection."""

    legacy.check_internal_auth(request)
    body = await legacy.read_internal_json(request)
    if not isinstance(body, dict) or set(body) != {"source_text"}:
        return JSONResponse(
            {"ok": False, "error": "invalid_source_text"},
            status_code=422,
        )
    try:
        extraction = await run_server_extraction(legacy, body.get("source_text"))
    except MemoryFormationV2LoopbackError as error:
        status = 504 if error.category == "extractor_timeout" else (
            422 if error.category == "invalid_source_text" else 503
        )
        return JSONResponse(
            {"ok": False, "error": error.category},
            status_code=status,
        )
    return _serialize_extraction(extraction)


async def extract_v2_via_loopback(
    *,
    ingest_url: object,
    internal_token: object,
    source_text: object,
    transport: httpx.AsyncBaseTransport | None = None,
) -> memory_formation_extractor_v2.AutoMemoryExtractionV2:
    """Call the strict localhost endpoint and rebuild a validated V2 extraction."""

    if type(source_text) is not str or not source_text:
        _raise("invalid_source_text")
    token = str(internal_token or "")
    if len(token) < 32:
        _raise("loopback_unavailable")
    url = _endpoint_from_ingest(ingest_url)
    try:
        async with httpx.AsyncClient(
            timeout=CLIENT_TIMEOUT_SECONDS,
            trust_env=False,
            transport=transport,
        ) as client:
            async with client.stream(
                "POST",
                url,
                headers={"X-API-Loop-Internal-Token": token},
                json={"source_text": source_text},
            ) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > CLIENT_RESPONSE_MAX_BYTES:
                        _raise("loopback_invalid_response")
                if response.status_code == 504:
                    _raise("extractor_timeout")
                if response.status_code < 200 or response.status_code >= 300:
                    try:
                        error_payload = json.loads(bytes(data))
                    except Exception:
                        error_payload = {}
                    category = error_payload.get("error") if isinstance(error_payload, dict) else ""
                    if category in {
                        "extractor_invalid_output",
                        "extractor_unavailable",
                        "invalid_source_text",
                    }:
                        _raise(category)
                    _raise("loopback_unavailable")
    except asyncio.CancelledError:
        raise
    except MemoryFormationV2LoopbackError:
        raise
    except (httpx.TimeoutException, httpx.TransportError, OSError):
        _raise("loopback_unavailable")
    try:
        payload = json.loads(bytes(data))
    except (json.JSONDecodeError, UnicodeError, ValueError):
        _raise("loopback_invalid_response")
    return _parse_extraction_payload(payload, source_length=len(source_text))
