"""Data-free, prompt-free capability probe for the authoritative provider model.

The probe is intentionally narrow: the browser supplies no model, URL, key,
prompt, or messages. The server resolves the current authoritative primary route
using the same migration authority as the model-only write path, then performs a
bodyless OpenAI-compatible Models API check. Upstream response bodies are never
read, logged, returned, or persisted.
"""

from __future__ import annotations

import asyncio
import os
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import httpx

from backend import provider_model_migration


CONTRACT_VERSION: Final = 1
PROBE_VERSION: Final = "openai-model-retrieve-v1"
PROBE_TIMEOUT_SECONDS: Final = 10.0

INVALID_REQUEST: Final = "provider_model_capability_probe_invalid_request"
UNAVAILABLE: Final = "provider_model_capability_probe_unavailable"

MODEL_VISIBLE: Final = "model_visible"
MODEL_NOT_VISIBLE: Final = "model_not_visible"
PROBE_UNSUPPORTED: Final = "probe_unsupported"
AUTH_REJECTED: Final = "provider_auth_rejected"
PROVIDER_TIMEOUT: Final = "provider_timeout"
RATE_LIMITED: Final = "provider_rate_limited"
UPSTREAM_UNAVAILABLE: Final = "provider_upstream_unavailable"
EXPLICIT_REJECTION: Final = "provider_explicit_rejection"

_ALLOWED_ERRORS: Final = frozenset({INVALID_REQUEST, UNAVAILABLE})
_ALLOWED_CAPABILITIES: Final = frozenset({
    MODEL_VISIBLE,
    MODEL_NOT_VISIBLE,
    PROBE_UNSUPPORTED,
    AUTH_REJECTED,
    PROVIDER_TIMEOUT,
    RATE_LIMITED,
    UPSTREAM_UNAVAILABLE,
    EXPLICIT_REJECTION,
})


class ProviderModelCapabilityProbeError(RuntimeError):
    """Fixed, data-free failure for the operator probe surface."""

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
        return f"ProviderModelCapabilityProbeError({str(self)!r})"


def _raise(category: str) -> None:
    raise ProviderModelCapabilityProbeError(category)


def validate_empty_probe_request(
    raw: object,
    *,
    content_length: object = None,
    content_encoding: object = "",
) -> None:
    """Require an empty, identity-encoded request so the browser supplies no probe data."""

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
    except ProviderModelCapabilityProbeError:
        raise
    except BaseException:
        _raise(UNAVAILABLE)


def _classify_status(status: int) -> str:
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        _raise(UNAVAILABLE)
    if 200 <= status < 300:
        return MODEL_VISIBLE
    if status in {401, 403}:
        return AUTH_REJECTED
    if status == 408:
        return PROVIDER_TIMEOUT
    if status == 429:
        return RATE_LIMITED
    if status >= 500:
        return UPSTREAM_UNAVAILABLE
    if status in {405, 501} or 300 <= status < 400:
        return PROBE_UNSUPPORTED
    return EXPLICIT_REJECTION


def _result(
    *,
    model: str,
    capability: str,
    provider_http_status: int,
    catalog_http_status: int | None = None,
) -> dict[str, object]:
    if capability not in _ALLOWED_CAPABILITIES:
        _raise(UNAVAILABLE)
    payload: dict[str, object] = {
        "ok": True,
        "contract_version": CONTRACT_VERSION,
        "probe": PROBE_VERSION,
        "model": model,
        "capability": capability,
        "provider_http_status": provider_http_status,
    }
    if catalog_http_status is not None:
        payload["catalog_http_status"] = catalog_http_status
    return payload


async def _status_only_get(client: httpx.AsyncClient, url: str, key: str) -> int:
    """Issue a bodyless GET and inspect only the HTTP status."""

    async with client.stream(
        "GET",
        url,
        headers={"Authorization": f"Bearer {key}"},
    ) as response:
        status = response.status_code
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        _raise(UNAVAILABLE)
    return status


async def probe_authoritative_primary_model(
    loop_config: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Probe the current primary model without sending a prompt or reading response bodies."""

    route = _authoritative_route(loop_config, environ)
    base = route["url"].rstrip("/")
    encoded_model = urllib.parse.quote(route["model"], safe="")
    exact_url = f"{base}/models/{encoded_model}"
    catalog_url = f"{base}/models"

    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(
                timeout=PROBE_TIMEOUT_SECONDS,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                exact_status = await _status_only_get(
                    client,
                    exact_url,
                    route["key"],
                )
                if exact_status != 404:
                    return _result(
                        model=route["model"],
                        capability=_classify_status(exact_status),
                        provider_http_status=exact_status,
                    )

                catalog_status = await _status_only_get(
                    client,
                    catalog_url,
                    route["key"],
                )
                if 200 <= catalog_status < 300:
                    capability = MODEL_NOT_VISIBLE
                elif (
                    catalog_status in {404, 405, 501}
                    or 300 <= catalog_status < 400
                ):
                    capability = PROBE_UNSUPPORTED
                else:
                    capability = _classify_status(catalog_status)
                return _result(
                    model=route["model"],
                    capability=capability,
                    provider_http_status=exact_status,
                    catalog_http_status=catalog_status,
                )
    except asyncio.CancelledError:
        raise
    except ProviderModelCapabilityProbeError:
        raise
    except (TimeoutError, httpx.TimeoutException, httpx.TransportError):
        _raise(UNAVAILABLE)
    except BaseException:
        _raise(UNAVAILABLE)


__all__ = (
    "AUTH_REJECTED",
    "CONTRACT_VERSION",
    "EXPLICIT_REJECTION",
    "INVALID_REQUEST",
    "MODEL_NOT_VISIBLE",
    "MODEL_VISIBLE",
    "PROBE_TIMEOUT_SECONDS",
    "PROBE_UNSUPPORTED",
    "PROBE_VERSION",
    "PROVIDER_TIMEOUT",
    "ProviderModelCapabilityProbeError",
    "RATE_LIMITED",
    "UNAVAILABLE",
    "UPSTREAM_UNAVAILABLE",
    "probe_authoritative_primary_model",
    "validate_empty_probe_request",
)
