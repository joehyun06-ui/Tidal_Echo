"""One-shot, local-only operator CLI for explicit Memory actions."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from typing import BinaryIO, TextIO

from . import (
    deployment_config,
    memory_explicit_actions,
    memory_operator_composition,
    telegram_integration,
)


STDIN_MAX_BYTES = 32 * 1024
_READ_LIMIT = STDIN_MAX_BYTES + 1
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{32,96}\Z")
_MEMORY_KEY = re.compile(r"[A-Za-z0-9_-]{32,96}\Z")

_COMMANDS = frozenset(
    {
        "remember",
        "correct",
        "forget",
        "status",
        "validate",
        "generate-request-id",
    }
)
_WRITE_COMMANDS = frozenset({"remember", "correct", "forget"})
_ACTION_NAMES = {
    "remember": "remember",
    "correct": "correct",
    "forget": "forget",
    "status": "status",
    "validate": "validate",
    "generate-request-id": "generate_request_id",
}

_PUBLIC_EXIT_CODES = {
    "internal_error": 1,
    "input_invalid": 2,
    "readiness_failed": 3,
    "request_binding_conflict": 4,
    "not_found": 5,
    "unsupported_action": 5,
    "storage_unavailable": 6,
    "transaction_outcome_uncertain": 7,
}

# This is an exact, reviewed mapping. Unknown internal categories fail closed.
_ACTION_CATEGORY_MAP = {
    "invalid_request": "input_invalid",
    "invalid_memory_key": "input_invalid",
    "invalid_content": "input_invalid",
    "content_too_long": "input_invalid",
    "empty_content": "input_invalid",
    "invalid_scope": "input_invalid",
    "invalid_kind": "input_invalid",
    "invalid_sensitivity": "input_invalid",
    "secret_detected": "input_invalid",
    "sensitivity_downgrade": "input_invalid",
    "sensitive_storage_disabled": "input_invalid",
    "forbidden_test_content": "input_invalid",
    "forbidden_log_content": "input_invalid",
    "technical_identifier_forbidden": "input_invalid",
    "invalid_state": "unsupported_action",
    "conflict": "request_binding_conflict",
    "request_binding_conflict": "request_binding_conflict",
    "not_found": "not_found",
    "unsupported_evidence": "unsupported_action",
    "storage_unavailable": "storage_unavailable",
    "transaction_outcome_uncertain": "transaction_outcome_uncertain",
    "feature_disabled": "readiness_failed",
    "explicit_writes_disabled": "readiness_failed",
    "memory_configuration_invalid": "readiness_failed",
    "memory_schema_invalid": "readiness_failed",
    "memory_fingerprint_profile_mismatch": "readiness_failed",
}

_COMPOSITION_STORAGE_CATEGORIES = frozenset(
    {
        "memory_storage_missing",
        "memory_storage_unavailable",
        "storage_unavailable",
    }
)
_COMPOSITION_READINESS_CATEGORIES = frozenset(
    {
        "deployment_config_invalid",
        "deployment_configuration_invalid",
        "invalid_brain_target",
        "invalid_heartbeat_contact_cooldown_seconds",
        "invalid_heartbeat_enabled",
        "invalid_heartbeat_interval_seconds",
        "invalid_heartbeat_quiet_hours_end",
        "invalid_heartbeat_quiet_hours_relationship",
        "invalid_heartbeat_quiet_hours_start",
        "invalid_heartbeat_schedule_revision",
        "invalid_heartbeat_timezone",
        "invalid_kelivo_allow_session_remap",
        "invalid_kelivo_auto_idempotency_enabled",
        "invalid_kelivo_auto_idempotency_replay_seconds",
        "invalid_kelivo_auto_idempotency_replay_window",
        "invalid_kelivo_client_concurrency",
        "invalid_kelivo_completion_commit_margin",
        "invalid_kelivo_concurrency_relationship",
        "invalid_kelivo_default_max_tokens",
        "invalid_kelivo_default_temperature",
        "invalid_kelivo_enabled",
        "invalid_kelivo_global_concurrency",
        "invalid_kelivo_internal_response_max_bytes",
        "invalid_kelivo_provider_contract",
        "invalid_kelivo_queue_timeout",
        "invalid_kelivo_rate_limit",
        "invalid_kelivo_require_telegram_session",
        "invalid_kelivo_stale_dispatch_relationship",
        "invalid_kelivo_stale_dispatch",
        "invalid_loop_config",
        "invalid_loop_config_size",
        "invalid_loop_config_structure",
        "invalid_loop_ingest_url",
        "invalid_loop_model_route",
        "invalid_loop_port",
        "invalid_loop_timeout",
        "invalid_loop_timeout_relationship",
        "invalid_memory_core_enabled",
        "invalid_memory_explicit_entry_enabled",
        "invalid_memory_explicit_writes_enabled",
        "invalid_memory_feature_relationship",
        "invalid_memory_forget_retention_policy",
        "invalid_memory_max_item_chars",
        "invalid_memory_sensitive_storage_enabled",
        "invalid_operit_share_enabled",
        "invalid_render_telegram_mvp",
        "invalid_sqlite_busy_timeout",
        "invalid_telegram_enabled",
        "invalid_telegram_test_mode",
        "brain_target_loop_required",
        "kelivo_api_key_missing",
        "kelivo_api_key_must_be_distinct",
        "kelivo_identity_invalid",
        "kelivo_provider_model_missing",
        "memory_configuration_invalid",
        "memory_core_disabled",
        "memory_explicit_entry_configuration_invalid",
        "memory_explicit_entry_disabled",
        "memory_explicit_entry_requires_core",
        "memory_explicit_entry_requires_writes",
        "memory_explicit_writes_disabled",
        "memory_fingerprint_hmac_secret_invalid",
        "memory_fingerprint_hmac_secret_missing",
        "memory_fingerprint_hmac_secret_must_be_distinct",
        "memory_fingerprint_key_id_invalid",
        "memory_fingerprint_key_id_missing",
        "memory_fingerprint_profile_mismatch",
        "memory_operator_schema_invalid",
        "model_fallback_not_allowed",
        "operit_share_api_key_missing",
        "operit_share_api_key_must_be_distinct",
        "operit_share_identity_invalid",
        "persistent_path_missing",
        "persistent_path_outside_root",
        "primary_model_config_incomplete",
        "telegram_config_incomplete",
        "telegram_config_invalid",
        "telegram_must_be_enabled",
        "telegram_secrets_must_be_distinct",
        "telegram_test_mode_not_allowed",
    }
)

_SUCCESS_CATEGORIES = {
    "remember": frozenset({"created", "idempotent_existing", "suppressed"}),
    "correct": frozenset({"corrected", "unchanged", "suppressed"}),
    "forget": frozenset({"forgotten", "already_forgotten"}),
}


class _InputInvalid(ValueError):
    pass


class _PublicFailure(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: str):
        if category not in _PUBLIC_EXIT_CODES:
            category = "internal_error"
        super().__init__(category)
        self.category = category

    def __repr__(self) -> str:
        return "<MemoryOperatorCliFailure>"


def _output(
    *,
    ok: bool,
    request_id: str | None,
    action: str,
    status: str,
    category: str,
    memory_key: str | None,
    replayed: bool,
) -> dict[str, object]:
    return {
        "ok": ok,
        "request_id": request_id,
        "action": action,
        "status": status,
        "category": category,
        "memory_key": memory_key,
        "replayed": replayed,
    }


def _write_text(stream: TextIO, value: str) -> None:
    binary_stream = getattr(stream, "buffer", None)
    if callable(getattr(binary_stream, "write", None)):
        binary_stream.write(value.encode("utf-8"))
        binary_stream.flush()
        return
    stream.write(value)
    stream.flush()


def _write_json(stream: TextIO, payload: Mapping[str, object]) -> None:
    _write_text(
        stream,
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        + "\n",
    )


def _binary_stdin(stdin: object | None) -> BinaryIO:
    if stdin is None:
        return sys.stdin.buffer
    candidate = getattr(stdin, "buffer", stdin)
    if not callable(getattr(candidate, "read", None)):
        raise _InputInvalid
    return candidate


def _read_stdin(stdin: object | None) -> bytes:
    stream = _binary_stdin(stdin)
    chunks: list[bytes] = []
    remaining = _READ_LIMIT
    while remaining > 0:
        chunk = stream.read(remaining)
        if chunk in (b"", None):
            break
        if type(chunk) is not bytes:
            raise _InputInvalid
        if len(chunk) > remaining:
            raise _InputInvalid
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > STDIN_MAX_BYTES:
        raise _InputInvalid
    return payload


def _decode_stdin(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _InputInvalid
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _InputInvalid from None


def _pairs_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _InputInvalid
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise _InputInvalid


def _json_object(raw: bytes) -> dict[str, object]:
    text = _decode_stdin(raw)
    if not text.strip():
        raise _InputInvalid
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, UnicodeError, _InputInvalid):
        raise _InputInvalid from None
    if type(value) is not dict:
        raise _InputInvalid
    return value


def _require_whitespace_only(raw: bytes) -> None:
    text = _decode_stdin(raw)
    if text.strip():
        raise _InputInvalid


def _exact_string(value: object) -> str:
    if type(value) is not str or any(
        0xD800 <= ord(character) <= 0xDFFF for character in value
    ):
        raise _InputInvalid
    return value


def _exact_fields(
    value: Mapping[str, object],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    keys = frozenset(value)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        raise _InputInvalid


def _request_id(value: object) -> str:
    candidate = _exact_string(value)
    if _REQUEST_ID.fullmatch(candidate) is None:
        raise _InputInvalid
    return candidate


def _memory_key(value: object) -> str:
    candidate = _exact_string(value)
    if _MEMORY_KEY.fullmatch(candidate) is None:
        raise _InputInvalid
    return candidate


def _remember_request(
    value: Mapping[str, object],
) -> memory_explicit_actions.RememberExplicitMemoryRequest:
    required = frozenset(
        {"request_id", "kind", "scope_type", "content", "sensitivity"}
    )
    _exact_fields(
        value,
        required=required,
        optional=frozenset({"scope_ref"}),
    )
    request_id = _request_id(value["request_id"])
    kind = _exact_string(value["kind"])
    scope_type = _exact_string(value["scope_type"])
    content = _exact_string(value["content"])
    sensitivity = _exact_string(value["sensitivity"])
    if kind == "assistant_experience":
        raise _InputInvalid
    if scope_type == "global_user":
        scope_ref = _exact_string(value.get("scope_ref", ""))
        if scope_ref != "":
            raise _InputInvalid
    else:
        if "scope_ref" not in value:
            raise _InputInvalid
        scope_ref = _exact_string(value["scope_ref"])
    return memory_explicit_actions.RememberExplicitMemoryRequest(
        request_id,
        kind,
        scope_type,
        scope_ref,
        content,
        sensitivity,
    )


def _correct_request(
    value: Mapping[str, object],
) -> memory_explicit_actions.CorrectExplicitMemoryRequest:
    _exact_fields(
        value,
        required=frozenset(
            {
                "request_id",
                "memory_key",
                "replacement_content",
                "sensitivity",
            }
        ),
    )
    return memory_explicit_actions.CorrectExplicitMemoryRequest(
        _request_id(value["request_id"]),
        _memory_key(value["memory_key"]),
        _exact_string(value["replacement_content"]),
        _exact_string(value["sensitivity"]),
    )


def _forget_request(
    value: Mapping[str, object],
) -> memory_explicit_actions.ForgetExplicitMemoryRequest:
    _exact_fields(
        value,
        required=frozenset({"request_id", "memory_key"}),
    )
    return memory_explicit_actions.ForgetExplicitMemoryRequest(
        _request_id(value["request_id"]),
        _memory_key(value["memory_key"]),
    )


def _status(
    telegram_config: telegram_integration.TelegramConfig,
    environ: Mapping[str, str],
) -> dict[str, object]:
    try:
        deployment = deployment_config.load_deployment_config(
            telegram_config,
            environ,
        )
    except deployment_config.DeploymentConfigError:
        raise _PublicFailure("readiness_failed") from None
    memory = deployment.memory
    if not (
        memory.enabled
        and memory.explicit_writes_enabled
        and memory.explicit_entry_enabled
        and memory.configuration_valid
        and memory.entry_configuration_valid
    ):
        raise _PublicFailure("readiness_failed")
    return _output(
        ok=True,
        request_id=None,
        action="status",
        status="configured",
        category="configured",
        memory_key=None,
        replayed=False,
    )


def _validate(
    telegram_config: telegram_integration.TelegramConfig,
    environ: Mapping[str, str],
) -> dict[str, object]:
    result = (
        memory_operator_composition
        .preflight_operator_memory_from_environment(
            telegram_config,
            environ,
        )
    )
    if not result.ready:
        category = (
            "storage_unavailable"
            if result.category in _COMPOSITION_STORAGE_CATEGORIES
            else "readiness_failed"
        )
        raise _PublicFailure(category)
    return _output(
        ok=True,
        request_id=None,
        action="validate",
        status="ready",
        category="ready",
        memory_key=None,
        replayed=False,
    )


def _generate_request_id() -> dict[str, object]:
    request_id = memory_explicit_actions.issue_request_id()
    if type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None:
        raise _PublicFailure("internal_error")
    return _output(
        ok=True,
        request_id=request_id,
        action="generate_request_id",
        status="generated",
        category="generated",
        memory_key=None,
        replayed=False,
    )


def _operator_telegram_config(
    environ: Mapping[str, str],
) -> telegram_integration.TelegramConfig:
    try:
        telegram_enabled = deployment_config.parse_strict_bool(
            environ.get("TELEGRAM_ENABLED", "false"),
            "invalid_telegram_enabled",
        )
    except deployment_config.DeploymentConfigError:
        raise _PublicFailure("readiness_failed") from None
    if telegram_enabled:
        raise _PublicFailure("readiness_failed")
    config = telegram_integration.TelegramConfig.from_env(
        {"TELEGRAM_ENABLED": "false"}
    )
    if (
        type(config) is not telegram_integration.TelegramConfig
        or config.requested is not False
        or config.enabled is not False
        or config.error != ""
    ):
        raise _PublicFailure("internal_error")
    return config


def _composition_failure(category: object) -> _PublicFailure:
    if category in _COMPOSITION_STORAGE_CATEGORIES:
        return _PublicFailure("storage_unavailable")
    if category in _COMPOSITION_READINESS_CATEGORIES:
        return _PublicFailure("readiness_failed")
    return _PublicFailure("internal_error")


def _action_failure(category: object) -> _PublicFailure:
    if type(category) is not str:
        return _PublicFailure("internal_error")
    return _PublicFailure(
        _ACTION_CATEGORY_MAP.get(category, "internal_error")
    )


def _write_action(
    command: str,
    request: object,
    telegram_config: telegram_integration.TelegramConfig,
    environ: Mapping[str, str],
) -> dict[str, object]:
    try:
        service = (
            memory_operator_composition
            .compose_operator_memory_service_from_environment(
                telegram_config,
                environ,
            )
        )
    except memory_operator_composition.MemoryOperatorCompositionError as error:
        raise _composition_failure(error.category) from None
    try:
        if command == "remember":
            result = service.remember_explicit_user_memory(request)
        elif command == "correct":
            result = service.correct_explicit_user_memory(request)
        else:
            result = service.forget_explicit_user_memory(request)
    except memory_explicit_actions.ExplicitMemoryActionError as error:
        raise _action_failure(error.category) from None
    if (
        type(result) is not memory_explicit_actions.ExplicitMemoryActionResult
        or result.action_kind != command
        or result.status != "completed"
        or result.category not in _SUCCESS_CATEGORIES[command]
        or type(result.request_id) is not str
        or _REQUEST_ID.fullmatch(result.request_id) is None
        or type(result.replayed) is not bool
    ):
        raise _PublicFailure("internal_error")
    if result.category == "suppressed":
        if result.memory_key is not None:
            raise _PublicFailure("internal_error")
    elif (
        type(result.memory_key) is not str
        or _MEMORY_KEY.fullmatch(result.memory_key) is None
    ):
        raise _PublicFailure("internal_error")
    return _output(
        ok=True,
        request_id=result.request_id,
        action=command,
        status=result.status,
        category=result.category,
        memory_key=result.memory_key,
        replayed=result.replayed,
    )


def main(
    argv: Sequence[str] | None = None,
    stdin: object | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr
    raw_command = args[0] if args and type(args[0]) is str else None
    command = raw_command if raw_command in _COMMANDS else None
    action = _ACTION_NAMES.get(command, "unknown")
    try:
        if len(args) != 1 or command is None:
            raise _PublicFailure("input_invalid")
        raw = _read_stdin(stdin)
        request = None
        if command in _WRITE_COMMANDS:
            value = _json_object(raw)
            if command == "remember":
                request = _remember_request(value)
            elif command == "correct":
                request = _correct_request(value)
            else:
                request = _forget_request(value)
        else:
            _require_whitespace_only(raw)

        if command == "generate-request-id":
            payload = _generate_request_id()
        else:
            environ = dict(os.environ)
            telegram_config = _operator_telegram_config(environ)
        if command == "status":
            payload = _status(telegram_config, environ)
        elif command == "validate":
            payload = _validate(telegram_config, environ)
        elif command in _WRITE_COMMANDS:
            payload = _write_action(
                command,
                request,
                telegram_config,
                environ,
            )
        _write_json(output_stream, payload)
        return 0
    except _InputInvalid:
        failure = _PublicFailure("input_invalid")
    except _PublicFailure as error:
        failure = error
    except KeyboardInterrupt:
        failure = _PublicFailure("internal_error")
    except Exception:
        failure = _PublicFailure("internal_error")

    _write_json(
        output_stream,
        _output(
            ok=False,
            request_id=None,
            action=action,
            status="failed",
            category=failure.category,
            memory_key=None,
            replayed=False,
        ),
    )
    _write_text(error_stream, failure.category + "\n")
    return _PUBLIC_EXIT_CODES[failure.category]


if __name__ == "__main__":
    raise SystemExit(main())
