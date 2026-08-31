"""Strict localhost-only provider adapter for Atomic Memory Formation V2.

The relay sends only the exact canonical source text to the api-loop. The api-loop
owns provider/model resolution and invokes the V2 extractor contract internally,
then returns only signal classes and source ranges. No candidate plaintext,
session identity, provider output, Memory store, or persistence authority crosses
this boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
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

    async def generation_callable(
        messages,
        session_id,
        provider_model,
        temperature,
        max_tokens,
        context,
    ):
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
        out = await legacy.run_kelivo_provider_contract(
            provider_model,
            list(messages),
            temperature=float(temperature),
            max_tokens=int(max_tokens),
        )
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
