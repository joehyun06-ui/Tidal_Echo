"""Data-free classification of the authoritative provider base route.

The browser supplies no provider data. The server resolves the current
authoritative primary route using the same model-migration authority, classifies
the base URL locally, then discards it. No provider network request is made and
no URL, host, key, model, header, or arbitrary provider string is returned.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from backend import provider_model_migration


CONTRACT_VERSION: Final = 1
PROBE_VERSION: Final = "provider-route-classification-v1"

INVALID_REQUEST: Final = "provider_route_classification_probe_invalid_request"
UNAVAILABLE: Final = "provider_route_classification_probe_unavailable"

OFFICIAL_OPENAI: Final = "official_openai"
OPENAI_COMPATIBLE_OTHER: Final = "openai_compatible_other"

_ALLOWED_ERRORS: Final = frozenset({INVALID_REQUEST, UNAVAILABLE})
_ALLOWED_CLASSIFICATIONS: Final = frozenset({
    OFFICIAL_OPENAI,
    OPENAI_COMPATIBLE_OTHER,
})


class ProviderRouteClassificationProbeError(RuntimeError):
    """Fixed, data-free failure for the operator classification surface."""

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
        return f"ProviderRouteClassificationProbeError({str(self)!r})"


def _raise(category: str) -> None:
    raise ProviderRouteClassificationProbeError(category)


def validate_empty_probe_request(
    raw: object,
    *,
    content_length: object = None,
    content_encoding: object = "",
) -> None:
    """Require an empty, identity-encoded browser request."""

    encoding = str(content_encoding if content_encoding is not None else "").strip().lower()
    if encoding not in {"", "identity"}:
        _raise(INVALID_REQUEST)
    if content_length not in (None, ""):
        text = str(content_length)
        if not text.isascii() or not text.isdecimal() or int(text) != 0:
            _raise(INVALID_REQUEST)
    if type(raw) is not bytes or raw != b"":
        _raise(INVALID_REQUEST)


def _authoritative_provider_url(
    loop_config: str | Path,
    environ: Mapping[str, str] | None,
) -> str:
    try:
        _raw, _current, route = provider_model_migration._load_authoritative_route(
            Path(loop_config),
            os.environ if environ is None else environ,
        )
        provider_model_migration.validate_model_request({"model": route.get("model")})
        url = route.get("url")
        if type(url) is not str or not url:
            _raise(UNAVAILABLE)
        return url
    except ProviderRouteClassificationProbeError:
        raise
    except Exception:
        _raise(UNAVAILABLE)


def _classify_provider_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return OPENAI_COMPATIBLE_OTHER

    official = (
        parsed.scheme.lower() == "https"
        and hostname is not None
        and hostname.lower() == "api.openai.com"
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and parsed.path in {"/v1", "/v1/"}
        and parsed.query == ""
        and parsed.fragment == ""
    )
    return OFFICIAL_OPENAI if official else OPENAI_COMPATIBLE_OTHER


def _result(classification: str) -> dict[str, object]:
    if classification not in _ALLOWED_CLASSIFICATIONS:
        _raise(UNAVAILABLE)
    return {
        "ok": True,
        "contract_version": CONTRACT_VERSION,
        "probe": PROBE_VERSION,
        "classification": classification,
    }


def classify_authoritative_provider_route(
    loop_config: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Classify the current provider base without making a provider request."""

    url = _authoritative_provider_url(loop_config, environ)
    return _result(_classify_provider_url(url))


__all__ = (
    "CONTRACT_VERSION",
    "INVALID_REQUEST",
    "OFFICIAL_OPENAI",
    "OPENAI_COMPATIBLE_OTHER",
    "PROBE_VERSION",
    "ProviderRouteClassificationProbeError",
    "UNAVAILABLE",
    "classify_authoritative_provider_route",
    "validate_empty_probe_request",
)
