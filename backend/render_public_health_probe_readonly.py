"""Opt-in *read-only* fixed-endpoint Render public health observation.

A genuine HTTPS GET can observe a point-in-time result, NOT certify that
Telegram webhooks work, ingress provenance is trusted, or release/rollback/
login/memory/paid-API actions are authorized. Network is OFF by default.
No arbitrary URLs, redirects, credentials, proxy env or TLS bypass.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping

ORIGIN = "https://ouou-home-telegram.onrender.com"
FLAG = "RENDER_READONLY_HEALTH_GET_EXPLICITLY_APPROVED"
PATHS = {"healthz": "/healthz", "readyz": "/readyz"}
CHECKS = frozenset({
    "database", "persistent_path", "brain_target", "telegram_config",
    "telegram_worker", "api_loop", "memory_core", "memory_explicit_entry",
    "memory_auto_candidate_persistence", "memory_candidate_review",
    "memory_candidate_decisions", "kelivo", "operit_share",
})
_MAX_BYTES = 4096
_STATUS = frozenset({"ready", "not_ready"})


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True, repr=False)
class ReadonlyHealthObservation:
    endpoint: str
    status: str
    http_status: int | None
    check_count: int = 0
    failed_checks: tuple[str, ...] = ()
    may_deploy: bool = False
    may_rollback: bool = False
    may_enable_oidc: bool = False
    may_write_memory: bool = False
    may_use_paid_api: bool = False

    def __repr__(self):
        return "<ReadonlyHealthObservation no_release_authority>"


def _from_json(endpoint: str, http_status: int, raw: bytes, content_type: str):
    if (
        http_status != 200
        or len(raw) > _MAX_BYTES
        or type(raw) is not bytes
        or type(content_type) is not str
        or not content_type.lower().split(";", 1)[0].strip() == "application/json"
    ):
        return ReadonlyHealthObservation(endpoint, "invalid_response", http_status)
    try:
        data = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeError, ValueError):
        return ReadonlyHealthObservation(endpoint, "invalid_response", http_status)
    if type(data) is not dict:
        return ReadonlyHealthObservation(endpoint, "invalid_response", http_status)
    if endpoint == "healthz":
        if data == {"ok": True} and type(data.get("ok")) is bool:
            return ReadonlyHealthObservation(endpoint, "http_observed", 200)
        return ReadonlyHealthObservation(endpoint, "invalid_response", 200)
    if set(data) - {"ready", "checks", "status", "errors"}:
        return ReadonlyHealthObservation(endpoint, "invalid_response", 200)
    checks = data.get("checks")
    if (
        type(data.get("ready")) is not bool
        or data.get("status") not in _STATUS
        or type(checks) is not dict
        or not checks or not set(checks) <= CHECKS
        or any(type(x) is not bool for x in checks.values())
        or type(data.get("errors", {})) is not dict
    ):
        return ReadonlyHealthObservation(endpoint, "invalid_response", 200)
    if any(type(x) is not str or x not in CHECKS for x in data.get("errors", {})):
        return ReadonlyHealthObservation(endpoint, "invalid_response", 200)
    failed = tuple(sorted(k for k, value in checks.items() if not value))
    if (
        data["ready"] is True and data["status"] == "ready"
        and set(failed) <= {"memory_core"}
        and not data.get("errors")
    ):
        # memory_core may be deliberately non-gating when disabled.
        return ReadonlyHealthObservation(endpoint, "ready_observed", 200, len(checks), failed)
    # The current deployed endpoint uses 503 for not_ready. Any 200 with a
    # not_ready payload or contradictory checks is not accepted as healthy.
    return ReadonlyHealthObservation(endpoint, "invalid_response", 200)


def probe_render_public_health(
    endpoint: str, *,
    environ: Mapping[str, str] | None = None,
    open_readonly: Callable | None = None,
) -> ReadonlyHealthObservation:
    if type(endpoint) is not str or endpoint not in PATHS:
        return ReadonlyHealthObservation("unrecognized", "blocked", None)
    env = os.environ if environ is None else environ
    if env.get(FLAG) != "true":
        return ReadonlyHealthObservation(endpoint, "disabled", None)
    req = urllib.request.Request(
        ORIGIN + PATHS[endpoint],
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        if open_readonly is None:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=ssl.create_default_context()),
                _RejectRedirect(),
            )
            open_readonly = opener.open
        with open_readonly(req, timeout=5) as response:
            code = response.status
            if type(code) is not int:
                raise ValueError("invalid status")
            raw = response.read(_MAX_BYTES + 1)
            ctype = response.headers.get("Content-Type", "")
        return _from_json(endpoint, code, raw, ctype)
    except urllib.error.HTTPError as exc:
        # Never read or print an error body, request URL, token or headers.
        status = int(exc.code) if type(exc.code) is int and 100 <= exc.code <= 599 else None
        return ReadonlyHealthObservation(endpoint, "http_error", status)
    except Exception:
        # Includes DNS, TLS, timeouts, redirects and injected transport failures.
        return ReadonlyHealthObservation(endpoint, "unavailable", None)
