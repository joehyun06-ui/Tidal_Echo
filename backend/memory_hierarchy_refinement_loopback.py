"""Strict localhost-only provider adapter for B4/B5 hierarchy refinement.

This endpoint accepts only the exact two-message envelopes emitted by the merged
Topic or Episode refinement extractors.  It rechecks session identity, developer
instruction, prompt/contract context, provider model, temperature/token budget,
and the corresponding bounded untrusted-data payload schema before dispatching
to the frozen primary provider.  It is not a generic prompt proxy.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
from typing import Final

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

from backend import (
    deployment_config,
    memory_hierarchy_baseline as baseline,
    memory_hierarchy_episode_refinement as episode_refinement,
    memory_hierarchy_episode_refinement_extractor as episode_extractor,
    memory_hierarchy_projection as hierarchy,
    memory_hierarchy_refinement as topic_refinement,
    memory_hierarchy_refinement_extractor as topic_extractor,
)


ENDPOINT: Final = "/loop/memory/hierarchy-refinement"
CLIENT_RESPONSE_MAX_BYTES: Final = max(
    topic_extractor.MAX_RESPONSE_CHARS,
    episode_extractor.MAX_RESPONSE_CHARS,
) + 4096
CLIENT_TIMEOUT_SECONDS: Final = max(
    topic_extractor.EXTRACTOR_TIMEOUT_SECONDS,
    episode_extractor.EXTRACTOR_TIMEOUT_SECONDS,
) + 5.0

_ERROR_CATEGORIES: Final = frozenset({
    "extractor_invalid_output",
    "extractor_timeout",
    "extractor_unavailable",
    "loopback_invalid_request",
    "loopback_invalid_response",
    "loopback_unavailable",
})


class MemoryHierarchyRefinementLoopbackError(RuntimeError):
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
    raise MemoryHierarchyRefinementLoopbackError(category)


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


def _records_payload(raw: object, *, maximum: int) -> list[dict]:
    if type(raw) is not str or not raw or len(raw) > maximum:
        _raise("loopback_invalid_request")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError):
        _raise("loopback_invalid_request")
    if type(payload) is not dict or set(payload) != {"records"}:
        _raise("loopback_invalid_request")
    records = payload["records"]
    if type(records) is not list:
        _raise("loopback_invalid_request")
    return records


def _valid_common_record(record: object, *, required: frozenset[str]) -> str:
    if type(record) is not dict or set(record) != required:
        _raise("loopback_invalid_request")
    key = record.get("memory_key")
    if type(key) is not str or not key:
        _raise("loopback_invalid_request")
    for name in required - {"memory_key"}:
        if type(record.get(name)) is not str:
            _raise("loopback_invalid_request")
    content = record.get("content")
    if type(content) is not str or not content or len(content) > hierarchy.MAX_ATOMIC_CONTENT_CHARS:
        _raise("loopback_invalid_request")
    return key


def _validate_topic_payload(raw: object) -> None:
    records = _records_payload(raw, maximum=topic_extractor.MAX_SERIALIZED_INPUT_CHARS)
    if not 1 <= len(records) <= topic_extractor.MAX_REFINEMENT_ATOMICS:
        _raise("loopback_invalid_request")
    seen: set[str] = set()
    required = frozenset({
        "memory_key",
        "broad_topic",
        "kind",
        "first_observed_at",
        "last_confirmed_at",
        "content",
    })
    allowed_topics = frozenset(baseline.TOPIC_BY_KIND.values())
    for record in records:
        key = _valid_common_record(record, required=required)
        if key in seen:
            _raise("loopback_invalid_request")
        seen.add(key)
        kind = record["kind"]
        if kind not in baseline.TOPIC_BY_KIND:
            _raise("loopback_invalid_request")
        if record["broad_topic"] != baseline.TOPIC_BY_KIND[kind] or record["broad_topic"] not in allowed_topics:
            _raise("loopback_invalid_request")


def _validate_episode_payload(raw: object) -> None:
    records = _records_payload(raw, maximum=episode_extractor.MAX_SERIALIZED_INPUT_CHARS)
    if not 2 <= len(records) <= episode_extractor.MAX_EXTRACTOR_ATOMICS:
        _raise("loopback_invalid_request")
    seen: set[str] = set()
    required = frozenset({
        "memory_key",
        "topic_key",
        "kind",
        "first_observed_at",
        "last_confirmed_at",
        "content",
    })
    topic_counts: dict[str, int] = {}
    for record in records:
        key = _valid_common_record(record, required=required)
        if key in seen:
            _raise("loopback_invalid_request")
        seen.add(key)
        if record["kind"] not in episode_refinement.EVENT_CAPABLE_KINDS:
            _raise("loopback_invalid_request")
        topic_key = record["topic_key"]
        if (
            not topic_key
            or len(topic_key) > hierarchy.MAX_NODE_KEY_CHARS
            or topic_key.startswith("state:")
        ):
            _raise("loopback_invalid_request")
        topic_counts[topic_key] = topic_counts.get(topic_key, 0) + 1
    if not topic_counts or any(count < 2 for count in topic_counts.values()):
        _raise("loopback_invalid_request")


def _extractor_contract(session_id: object):
    if session_id == topic_extractor.EXTRACTOR_SESSION_ID:
        return {
            "instruction": topic_extractor.EXTRACTOR_INSTRUCTION,
            "temperature": topic_extractor.EXTRACTOR_TEMPERATURE,
            "max_tokens": topic_extractor.EXTRACTOR_MAX_TOKENS,
            "response_chars": topic_extractor.MAX_RESPONSE_CHARS,
            "context": {
                "memory_hierarchy_refinement_extractor": topic_extractor.EXTRACTOR_CONTRACT_VERSION,
                "memory_hierarchy_refinement_contract": topic_refinement.REFINEMENT_CONTRACT_VERSION,
                "memory_hierarchy_projection_contract": hierarchy.PROJECTION_CONTRACT_VERSION,
            },
            "validate_payload": _validate_topic_payload,
        }
    if session_id == episode_extractor.EXTRACTOR_SESSION_ID:
        return {
            "instruction": episode_extractor.EXTRACTOR_INSTRUCTION,
            "temperature": episode_extractor.EXTRACTOR_TEMPERATURE,
            "max_tokens": episode_extractor.EXTRACTOR_MAX_TOKENS,
            "response_chars": episode_extractor.MAX_RESPONSE_CHARS,
            "context": {
                "memory_hierarchy_episode_refinement_extractor": episode_extractor.EXTRACTOR_CONTRACT_VERSION,
                "memory_hierarchy_episode_refinement_contract": episode_refinement.EPISODE_REFINEMENT_CONTRACT_VERSION,
                "memory_hierarchy_projection_contract": hierarchy.PROJECTION_CONTRACT_VERSION,
            },
            "validate_payload": _validate_episode_payload,
        }
    _raise("loopback_invalid_request")


def _validate_dispatch_body(
    body: object,
    provider_model: str,
    prompt_contract_version: str,
) -> tuple[tuple[dict[str, str], ...], dict, dict]:
    if type(body) is not dict or set(body) != {
        "messages", "session_id", "provider_model", "temperature", "max_tokens", "context"
    }:
        _raise("loopback_invalid_request")
    contract = _extractor_contract(body.get("session_id"))
    if (
        body.get("provider_model") != provider_model
        or body.get("temperature") != contract["temperature"]
        or type(body.get("max_tokens")) is not int
        or not 1 <= body["max_tokens"] <= contract["max_tokens"]
    ):
        _raise("loopback_invalid_request")
    messages = body.get("messages")
    if type(messages) is not list or len(messages) != 2:
        _raise("loopback_invalid_request")
    if messages[0] != {"role": "developer", "content": contract["instruction"]}:
        _raise("loopback_invalid_request")
    if (
        type(messages[1]) is not dict
        or set(messages[1]) != {"role", "content"}
        or messages[1].get("role") != "user"
    ):
        _raise("loopback_invalid_request")
    context = body.get("context")
    expected_context = {
        "prompt_contract_version": prompt_contract_version,
        **contract["context"],
    }
    if context != expected_context:
        _raise("loopback_invalid_request")
    contract["validate_payload"](messages[1]["content"])
    return tuple(messages), context, contract


async def handle_request(legacy: object, request: Request):
    legacy.check_internal_auth(request)
    body = await legacy.read_internal_json(request)
    try:
        defaults = await asyncio.to_thread(
            deployment_config.resolve_kelivo_provider_contract_defaults,
            os.environ,
            legacy.LOOP_CONFIG,
        )
        prompt_contract_version = (
            legacy.kelivo_service.PROMPT_CONTRACT_VERSION
            if hasattr(legacy, "kelivo_service")
            else "kelivo-provider-prompt-v1"
        )
        messages, _context, contract = _validate_dispatch_body(
            body,
            defaults.provider_model,
            prompt_contract_version,
        )
        out = await legacy.run_kelivo_provider_contract(
            defaults.provider_model,
            list(messages),
            temperature=float(contract["temperature"]),
            max_tokens=int(body["max_tokens"]),
        )
        if not isinstance(out, dict) or out.get("outcome") != "success":
            if isinstance(out, dict) and out.get("error") == "model_timeout":
                raise TimeoutError
            _raise("extractor_unavailable")
        text = str(out.get("text") or "")
        if not text or len(text) > int(contract["response_chars"]):
            _raise("extractor_invalid_output")
        return {"ok": True, "text": text}
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        return JSONResponse({"ok": False, "error": "extractor_timeout"}, status_code=504)
    except MemoryHierarchyRefinementLoopbackError as error:
        status = 422 if error.category == "loopback_invalid_request" else 503
        return JSONResponse({"ok": False, "error": error.category}, status_code=status)
    except Exception:
        return JSONResponse({"ok": False, "error": "extractor_unavailable"}, status_code=503)


async def generate_via_loopback(
    messages,
    session_id,
    provider_model,
    temperature,
    max_tokens,
    context,
    *,
    ingest_url: object,
    internal_token: object,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    token = str(internal_token or "")
    if len(token) < 32:
        _raise("loopback_unavailable")
    url = _endpoint_from_ingest(ingest_url)
    body = {
        "messages": list(messages),
        "session_id": session_id,
        "provider_model": provider_model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "context": context,
    }
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
                json=body,
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
                        payload = json.loads(bytes(data))
                    except Exception:
                        payload = {}
                    category = payload.get("error") if isinstance(payload, dict) else ""
                    if category in {"extractor_invalid_output", "extractor_unavailable"}:
                        _raise(category)
                    _raise("loopback_unavailable")
    except asyncio.CancelledError:
        raise
    except MemoryHierarchyRefinementLoopbackError:
        raise
    except (httpx.TimeoutException, httpx.TransportError, OSError):
        _raise("loopback_unavailable")
    try:
        payload = json.loads(bytes(data))
    except (json.JSONDecodeError, UnicodeError, ValueError):
        _raise("loopback_invalid_response")
    if (
        type(payload) is not dict
        or set(payload) != {"ok", "text"}
        or payload.get("ok") is not True
    ):
        _raise("loopback_invalid_response")
    text = payload.get("text")
    if type(text) is not str or not text or len(text) > max(
        topic_extractor.MAX_RESPONSE_CHARS,
        episode_extractor.MAX_RESPONSE_CHARS,
    ):
        _raise("loopback_invalid_response")
    return {"text": text}
