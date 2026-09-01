"""Strict localhost-only generation adapter for hierarchy summary v2.

The public relay may call this adapter only with the exact two-message payload
constructed by ``memory_hierarchy_summary_extractor_v2``.  The api-loop rechecks
the extractor session, developer instruction, bounded untrusted-data schema,
provider model, temperature/token limits, and context before dispatching to the
frozen primary provider.  It is not a generic prompt proxy.
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
    memory_hierarchy_summary as summary,
    memory_hierarchy_summary_extractor_v2 as extractor,
)


ENDPOINT: Final = "/loop/memory/hierarchy-summary-v2"
CLIENT_RESPONSE_MAX_BYTES: Final = extractor.MAX_RESPONSE_CHARS + 4096
CLIENT_TIMEOUT_SECONDS: Final = extractor.EXTRACTOR_TIMEOUT_SECONDS + 5.0

_ERROR_CATEGORIES: Final = frozenset({
    "extractor_invalid_output",
    "extractor_timeout",
    "extractor_unavailable",
    "loopback_invalid_request",
    "loopback_invalid_response",
    "loopback_unavailable",
})


class MemoryHierarchySummaryLoopbackV2Error(RuntimeError):
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
    raise MemoryHierarchySummaryLoopbackV2Error(category)


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


def _validate_user_payload(raw: object, expected_node_type: str) -> None:
    if type(raw) is not str or not raw or len(raw) > extractor.MAX_SERIALIZED_INPUT_CHARS:
        _raise("loopback_invalid_request")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError):
        _raise("loopback_invalid_request")
    if type(payload) is not dict or set(payload) != {
        "target_type", "records", "episode_groups"
    }:
        _raise("loopback_invalid_request")
    if payload["target_type"] != expected_node_type or expected_node_type not in {
        "topic", "episode", "canonical_state"
    }:
        _raise("loopback_invalid_request")
    records = payload["records"]
    if type(records) is not list or not 1 <= len(records) <= summary.MAX_SUMMARY_ATOMICS:
        _raise("loopback_invalid_request")
    seen: set[str] = set()
    for record in records:
        if type(record) is not dict or set(record) != {
            "memory_key", "kind", "first_observed_at", "last_confirmed_at", "content"
        }:
            _raise("loopback_invalid_request")
        key = record["memory_key"]
        if type(key) is not str or not key or key in seen:
            _raise("loopback_invalid_request")
        seen.add(key)
        if any(type(record[name]) is not str for name in (
            "kind", "first_observed_at", "last_confirmed_at", "content"
        )):
            _raise("loopback_invalid_request")
        if not record["content"] or len(record["content"]) > 4096:
            _raise("loopback_invalid_request")
    episode_groups = payload["episode_groups"]
    if type(episode_groups) is not list:
        _raise("loopback_invalid_request")
    for group in episode_groups:
        if type(group) is not list or len(group) < 2:
            _raise("loopback_invalid_request")
        if any(type(key) is not str or key not in seen for key in group):
            _raise("loopback_invalid_request")


def _validate_dispatch_body(body: object, provider_model: str) -> tuple[tuple[dict[str, str], ...], dict]:
    if type(body) is not dict or set(body) != {
        "messages", "session_id", "provider_model", "temperature", "max_tokens", "context"
    }:
        _raise("loopback_invalid_request")
    if (
        body["session_id"] != extractor.EXTRACTOR_SESSION_ID
        or body["provider_model"] != provider_model
        or body["temperature"] != extractor.EXTRACTOR_TEMPERATURE
        or type(body["max_tokens"]) is not int
        or not 1 <= body["max_tokens"] <= extractor.EXTRACTOR_MAX_TOKENS
    ):
        _raise("loopback_invalid_request")
    messages = body["messages"]
    if type(messages) is not list or len(messages) != 2:
        _raise("loopback_invalid_request")
    if messages[0] != {"role": "developer", "content": extractor.EXTRACTOR_INSTRUCTION}:
        _raise("loopback_invalid_request")
    if type(messages[1]) is not dict or set(messages[1]) != {"role", "content"} or messages[1]["role"] != "user":
        _raise("loopback_invalid_request")
    context = body["context"]
    if type(context) is not dict or set(context) != {
        "prompt_contract_version",
        "memory_hierarchy_summary_extractor",
        "memory_hierarchy_summary_contract",
        "summary_target_type",
    }:
        _raise("loopback_invalid_request")
    if (
        context["memory_hierarchy_summary_extractor"] != extractor.EXTRACTOR_CONTRACT_VERSION
        or context["memory_hierarchy_summary_contract"] != summary.SUMMARY_CONTRACT_VERSION_V2
        or context["summary_target_type"] not in {"topic", "episode", "canonical_state"}
    ):
        _raise("loopback_invalid_request")
    _validate_user_payload(messages[1]["content"], context["summary_target_type"])
    return tuple(messages), context


async def handle_request(legacy: object, request: Request):
    legacy.check_internal_auth(request)
    body = await legacy.read_internal_json(request)
    try:
        defaults = await asyncio.to_thread(
            deployment_config.resolve_kelivo_provider_contract_defaults,
            os.environ,
            legacy.LOOP_CONFIG,
        )
        messages, _context = _validate_dispatch_body(body, defaults.provider_model)
        out = await legacy.run_kelivo_provider_contract(
            defaults.provider_model,
            list(messages),
            temperature=extractor.EXTRACTOR_TEMPERATURE,
            max_tokens=int(body["max_tokens"]),
        )
        if not isinstance(out, dict) or out.get("outcome") != "success":
            if isinstance(out, dict) and out.get("error") == "model_timeout":
                raise TimeoutError
            _raise("extractor_unavailable")
        text = str(out.get("text") or "")
        if not text or len(text) > extractor.MAX_RESPONSE_CHARS:
            _raise("extractor_invalid_output")
        return {"ok": True, "text": text}
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        return JSONResponse({"ok": False, "error": "extractor_timeout"}, status_code=504)
    except MemoryHierarchySummaryLoopbackV2Error as error:
        status = 422 if error.category == "loopback_invalid_request" else 503
        return JSONResponse({"ok": False, "error": error.category}, status_code=status)
    except Exception:
        return JSONResponse({"ok": False, "error": "extractor_unavailable"}, status_code=503)


async def generate_v2_via_loopback(
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
    except MemoryHierarchySummaryLoopbackV2Error:
        raise
    except (httpx.TimeoutException, httpx.TransportError, OSError):
        _raise("loopback_unavailable")
    try:
        payload = json.loads(bytes(data))
    except (json.JSONDecodeError, UnicodeError, ValueError):
        _raise("loopback_invalid_response")
    if type(payload) is not dict or set(payload) != {"ok", "text"} or payload.get("ok") is not True:
        _raise("loopback_invalid_response")
    text = payload.get("text")
    if type(text) is not str or not text or len(text) > extractor.MAX_RESPONSE_CHARS:
        _raise("loopback_invalid_response")
    return {"text": text}
