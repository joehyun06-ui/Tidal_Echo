"""Opt-in relay integration for the P2-C Codex Web canary.

This module patches an already-imported relay module only when an alternate entrypoint
explicitly installs it. Production `backend.app` and `legacy_chat_bridge_app` remain
unchanged until the supervisor command is deliberately switched.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping

from . import codex_web_completion


class CodexCanaryRelayIntegrationError(RuntimeError):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


_REQUIRED_QUEUED_ACK = frozenset({
    "ok",
    "queued",
    "provider",
    "generation_provider",
    "generation_id",
    "api_session",
    "canonical_message_id",
    "status",
})


def _codex_meta_candidate(meta: object) -> bool:
    if not isinstance(meta, dict):
        return False
    return any(
        key in meta
        for key in (
            "codex_callback_identity",
            "client_message_id",
        )
    ) or meta.get("provider") == "codex" or meta.get("source") == "codex_generation"


def _complete_codex_reply(relay_module, msg: Mapping[str, object]) -> dict | None:
    if msg.get("kind") != "reply":
        return None
    meta = msg.get("meta")
    if not _codex_meta_candidate(meta):
        return None
    if not isinstance(meta, dict):
        raise CodexCanaryRelayIntegrationError("codex_web_completion_invalid")
    if meta.get("channel") != "web" or meta.get("provider") != "codex" or meta.get("source") != "codex_generation":
        raise CodexCanaryRelayIntegrationError("codex_web_completion_invalid")
    try:
        return codex_web_completion.complete_codex_web_generation(
            relay_module.DB_PATH,
            callback_identity=meta.get("codex_callback_identity"),
            generation_id=meta.get("generation_id"),
            client_message_id=meta.get("client_message_id"),
            api_session=meta.get("api_session"),
            reply_to=meta.get("reply_to"),
            text=msg.get("text") or "",
            ts=meta.get("ts") or relay_module.now_iso(),
            usage=meta.get("usage") if isinstance(meta.get("usage"), dict) else None,
            timeout_seconds=float(relay_module.DEPLOYMENT.sqlite_busy_timeout_seconds),
        )
    except codex_web_completion.CodexWebCompletionError as exc:
        raise CodexCanaryRelayIntegrationError(exc.category) from None


def _queued_ack(payload: object, *, msg: Mapping[str, object]) -> dict | None:
    if not isinstance(payload, dict) or payload.get("queued") is not True:
        return None
    if not _REQUIRED_QUEUED_ACK.issubset(payload):
        raise CodexCanaryRelayIntegrationError("loop_queued_ack_invalid")
    meta = msg.get("meta") or {}
    expected_session = str(meta.get("api_session") or "") if isinstance(meta, dict) else ""
    canonical_id = msg.get("id")
    expected_generation_id = (
        f"codex-gen-{canonical_id}"
        if isinstance(canonical_id, int) and not isinstance(canonical_id, bool) and canonical_id > 0
        else ""
    )
    if (
        not expected_generation_id
        or payload.get("ok") is not True
        or payload.get("provider") != "codex"
        or payload.get("generation_provider") != "codex"
        or payload.get("status") != "queued"
        or str(payload.get("api_session") or "") != expected_session
        or payload.get("canonical_message_id") != canonical_id
        or payload.get("generation_id") != expected_generation_id
    ):
        raise CodexCanaryRelayIntegrationError("loop_queued_ack_correlation_mismatch")
    return payload


def _forward_web_to_loop_sync(relay_module, msg: Mapping[str, object]) -> dict:
    meta = msg.get("meta") or {}
    payload = {
        "id": msg.get("id"),
        "text": msg.get("text", ""),
        "session_id": meta.get("api_session") or "" if isinstance(meta, dict) else "",
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        relay_module.LOOP_INGEST_URL,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Loop-Internal-Token": relay_module.API_LOOP_INTERNAL_TOKEN,
        },
    )
    max_bytes = int(relay_module.DEPLOYMENT.kelivo.internal_response_max_bytes)
    try:
        with urllib.request.urlopen(req, timeout=relay_module.LOOP_DISPATCH_TIMEOUT_SECONDS) as response:
            response_bytes = response.read(max_bytes + 1)
            if len(response_bytes) > max_bytes:
                raise relay_module.LoopDispatchError("loop_response_too_large", True)
            raw = response_bytes.decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_bytes = exc.read(max_bytes + 1)
        if len(error_bytes) > max_bytes:
            raise relay_module.LoopDispatchError("loop_response_too_large", True) from None
        try:
            body = json.loads(error_bytes.decode("utf-8"))
        except Exception:
            body = {}
        uncertain = bool(body.get("dispatch_uncertain")) if isinstance(body, dict) else False
        raise relay_module.LoopDispatchError(
            "loop_dispatch_uncertain" if uncertain else "loop_explicit_failed",
            uncertain,
        ) from None
    except (TimeoutError, urllib.error.URLError, ConnectionError, OSError):
        raise relay_module.LoopDispatchError("loop_dispatch_timeout", True) from None
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise relay_module.LoopDispatchError("loop_invalid_ack", True) from None
    if not isinstance(body, dict):
        raise relay_module.LoopDispatchError("loop_invalid_ack", True)
    try:
        queued = _queued_ack(body, msg=msg)
    except CodexCanaryRelayIntegrationError as exc:
        raise relay_module.LoopDispatchError(exc.category, False) from None
    if queued is not None:
        return queued
    # Preserve the existing synchronous API-Web ACK contract exactly.
    required_ack = ("callback_delivered", "generation_id", "stream_id", "api_session")
    if body.get("ok") is False:
        raise relay_module.LoopDispatchError(
            "loop_dispatch_uncertain" if body.get("dispatch_uncertain") else "loop_explicit_failed",
            bool(body.get("dispatch_uncertain")),
        )
    if body.get("ok") is not True or any(key not in body for key in required_ack):
        raise relay_module.LoopDispatchError("correlation_missing", True)
    if body.get("callback_delivered") is not True:
        raise relay_module.LoopDispatchError("loop_callback_failed", False)
    if str(body.get("api_session") or "") != str(payload["session_id"] or ""):
        raise relay_module.LoopDispatchError("loop_ack_correlation_mismatch", False)
    return body


def install(relay_module) -> None:
    """Install one idempotent opt-in patch set on a legacy relay module."""
    if getattr(relay_module, "_CODEX_CANARY_RELAY_INSTALLED", False):
        return
    original_completion = relay_module.telegram_completion_for
    original_forward = relay_module._forward_to_loop_sync

    def completion_for(msg):
        codex = _complete_codex_reply(relay_module, msg)
        if codex is not None:
            return codex
        return original_completion(msg)

    def forward_to_loop_sync(msg, routing=None):
        if routing:
            return original_forward(msg, routing)
        return _forward_web_to_loop_sync(relay_module, msg)

    relay_module.telegram_completion_for = completion_for
    relay_module._forward_to_loop_sync = forward_to_loop_sync
    relay_module._CODEX_CANARY_RELAY_INSTALLED = True
