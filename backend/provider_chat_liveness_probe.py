"""Prompt-free liveness probe for the authoritative provider chat endpoint.

The browser supplies no provider data and no prompt. The server resolves the
current authoritative primary route, then sends a fixed invalid JSON object to
``/chat/completions`` so a normal OpenAI-compatible endpoint should reject it
before any generation work can begin. Upstream response bodies are never read,
logged, returned, or persisted.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import httpx

from backend import provider_model_migration


CONTRACT_VERSION: Final = 1
PROBE_VERSION: Final = "openai-chat-completions-validation-v1"
PROBE_TIMEOUT_SECONDS: Final = 10.0

INVALID_REQUEST: Final = "provider_chat_liveness_probe_invalid_request"
UNAVAILABLE: Final = "provider_chat_liveness_probe_unavailable"

ENDPOINT_VALIDATING: Final = "endpoint_validating"
AUTH_REJECTED: Final = "provider_auth_rejected"
PROVIDER_TIMEOUT: Final = "provider_timeout"
RATE_LIMITED: Final = "provider_rate_limited"
UPSTREAM_UNAVAILABLE: Final = "provider_upstream_unavailable"
ENDPOINT_UNSUPPORTED: Final = "endpoint_unsupported"
UNEXPECTED_ACCEPTANCE: Final = "unexpected_acceptance"
ENDPOINT_REDIRECTED: Final = "endpoint_redirected"
EXPLICIT_REJECTION: Final = "provider_explicit_rejection"

_ALLOWED_ERRORS: Final = frozenset({INVALID_REQUEST, UNAVAILABLE})
_ALLOWED_CAPABILITIES: Final = frozenset({
    ENDPOINT_VALIDATING,
    AUTH_REJECTED,
    PROVIDER_TIMEOUT,
    RATE_LIMITED,
    UPSTREAM_UNAVAILABLE,
    ENDPOINT_UNSUPPORTED,
    UNEXPECTED_ACCEPTANCE,
    ENDPOINT_REDIRECTED,
    EXPLICIT_REJECTION,
})


class ProviderChatLivenessProbeError(RuntimeError):
    """Fixed, data-free failure for the operator liveness probe surface."""

    __slots__ = ("category", "status_code")

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ALLOWED_ERRORS
            else UNAVAILABLE
        )
        self.category = safe
        self.status_code = 400 if safe == INVALID_REQUEST else 503
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except BaseException:
            return UNAVAILABLE

    def __repr__(self) -> str:
        return f"ProviderChatLivenessProbeError({str(self)!r})"


def _raise(category: str) -> None:
    raise ProviderChatLivenessProbeError(category)


def validate_empty_probe_request(
    raw: object,
    *,
    content_length: object = None,
    content_encoding: object = "",
) -> None:
    """Require an empty browser request so callers cannot influence the probe."""

    encoding = str(content_encoding if content_encoding is not None else "").strip().lower()
    if encoding not in {"", "identity"}:
        _raise(INVALID_REQUEST)
    if content_length not in (None, ""):
        text = str(content_length)
        if not text.isascii() or not text.isdecimal() or int(text) != 0:
            _raise(INVALID_REQUEST)
    if type(raw) is not bytes or raw != b"":
        _raise(INVALID_REQUEST)


def _authoritative_route(
    loop_config: str | Path,
    environ: Mapping[str, str] | None,
) -> dict[str, str]:
    try:
        _raw, _current, route = provider_model_migration._load_authoritative_route(
            Path(loop_config),
            os.environ if environ is None else environ,
        )
        provider_model_migration.validate_model_request({"model": route.get("model")})
        return {
            "url": str(route["url"]),
            "key": str(route["key"]),
            "model": str(route["model"]),
        }
    except ProviderChatLivenessProbeError:
        raise
    except BaseException:
        _raise(UNAVAILABLE)


def _classify_status(status: int) -> str:
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        _raise(UNAVAILABLE)
    if status in {400, 422}:
        return ENDPOINT_VALIDATING
    if status in {401, 403}:
        return AUTH_REJECTED
    if status == 408:
        return PROVIDER_TIMEOUT
    if status == 429:
        return RATE_LIMITED
    if status >= 500:
        return UPSTREAM_UNAVAILABLE
    if status in {404, 405, 501}:
        return ENDPOINT_UNSUPPORTED
    if 200 <= status < 300:
        return UNEXPECTED_ACCEPTANCE
    if 300 <= status < 400:
        return ENDPOINT_REDIRECTED
    return EXPLICIT_REJECTION


def _result(
    *,
    configured_model: str,
    capability: str,
    provider_http_status: int,
) -> dict[str, object]:
    if capability not in _ALLOWED_CAPABILITIES:
        _raise(UNAVAILABLE)
    return {
        "ok": True,
        "contract_version": CONTRACT_VERSION,
        "probe": PROBE_VERSION,
        "configured_model": configured_model,
        "capability": capability,
        "provider_http_status": provider_http_status,
    }


async def _status_only_post(
    client: httpx.AsyncClient,
    url: str,
    key: str,
) -> int:
    """Send the fixed invalid JSON object and inspect only the HTTP status."""

    async with client.stream(
        "POST",
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        content=b"{}",
    ) as response:
        status = response.status_code
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        _raise(UNAVAILABLE)
    return status


async def probe_authoritative_chat_endpoint(
    loop_config: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Probe chat endpoint validation without sending model, messages, or prompt."""

    route = _authoritative_route(loop_config, environ)
    endpoint = route["url"].rstrip("/") + "/chat/completions"
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(
                timeout=PROBE_TIMEOUT_SECONDS,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                status = await _status_only_post(client, endpoint, route["key"])
                return _result(
                    configured_model=route["model"],
                    capability=_classify_status(status),
                    provider_http_status=status,
                )
    except asyncio.CancelledError:
        raise
    except ProviderChatLivenessProbeError:
        raise
    except (TimeoutError, httpx.TimeoutException, httpx.TransportError):
        _raise(UNAVAILABLE)
    except BaseException:
        _raise(UNAVAILABLE)


__all__ = (
    "AUTH_REJECTED",
    "CONTRACT_VERSION",
    "ENDPOINT_REDIRECTED",
    "ENDPOINT_UNSUPPORTED",
    "ENDPOINT_VALIDATING",
    "EXPLICIT_REJECTION",
    "INVALID_REQUEST",
    "PROBE_TIMEOUT_SECONDS",
    "PROBE_VERSION",
    "PROVIDER_TIMEOUT",
    "ProviderChatLivenessProbeError",
    "RATE_LIMITED",
    "UNAVAILABLE",
    "UNEXPECTED_ACCEPTANCE",
    "UPSTREAM_UNAVAILABLE",
    "probe_authoritative_chat_endpoint",
    "validate_empty_probe_request",
)
