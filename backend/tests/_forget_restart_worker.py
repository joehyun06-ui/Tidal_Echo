"""Isolated test worker for completed Forget process-restart replay."""

from __future__ import annotations

import importlib
import json
import socket
import sys
import tempfile
from pathlib import Path
from unittest import mock

from backend import channel_store, memory_action_ledger
from backend.tests.test_memory_service import bootstrap_runtime, memory_config


_MAX_STDIN_BYTES = 16 * 1024
_MAX_PATH_CHARS = 1024
_MAX_CONTENT_CHARS = 4096
_COMPLETE_FIELDS = frozenset({
    "phase",
    "path",
    "secret",
    "content",
    "remember_request_id",
    "forget_request_id",
})
_REPLAY_FIELDS = frozenset({
    "phase",
    "path",
    "secret",
    "forget_request_id",
    "memory_key",
})
_TABLES = (
    "messages",
    "memory_action_requests",
    "memory_evidence_events",
    "memory_items",
    "memory_sources",
    "memory_suppressions",
)


def _payload_error() -> None:
    raise SystemExit("restart_payload_invalid")


def _bounded_payload() -> dict[str, str]:
    raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
    if not raw or len(raw) > _MAX_STDIN_BYTES:
        _payload_error()
    try:
        text = raw.decode("utf-8")
        payload, end = json.JSONDecoder().raw_decode(text)
    except (TypeError, UnicodeError, ValueError, json.JSONDecodeError):
        _payload_error()
    if end != len(text) or type(payload) is not dict:
        _payload_error()
    phase = payload.get("phase")
    if type(phase) is not str or phase not in {"complete", "replay"}:
        _payload_error()
    expected = _COMPLETE_FIELDS if phase == "complete" else _REPLAY_FIELDS
    if frozenset(payload) != expected:
        _payload_error()
    if any(type(payload[name]) is not str for name in expected):
        _payload_error()
    if (
        memory_action_ledger.REQUEST_ID_PATTERN.fullmatch(
            payload["forget_request_id"]
        )
        is None
        or (
            phase == "complete"
            and memory_action_ledger.REQUEST_ID_PATTERN.fullmatch(
                payload["remember_request_id"]
            )
            is None
        )
        or not memory_action_ledger._valid_secret(payload["secret"])
    ):
        _payload_error()
    raw_path = payload["path"]
    if not raw_path or len(raw_path) > _MAX_PATH_CHARS:
        _payload_error()
    try:
        resolved = Path(raw_path).resolve(strict=False)
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
        resolved.relative_to(temp_root)
    except (OSError, RuntimeError, ValueError):
        _payload_error()
    if (
        not Path(raw_path).is_absolute()
        or resolved.suffix != ".sqlite3"
        or not resolved.parent.is_dir()
        or (phase == "replay" and not resolved.is_file())
    ):
        _payload_error()
    payload["path"] = str(resolved)
    if phase == "complete":
        content = payload["content"]
        if (
            not content
            or len(content) > _MAX_CONTENT_CHARS
            or len(content.encode("utf-8")) > _MAX_STDIN_BYTES
        ):
            _payload_error()
    elif (
        memory_action_ledger.MEMORY_KEY_PATTERN.fullmatch(payload["memory_key"])
        is None
    ):
        _payload_error()
    return payload


def _network_blocked(*_args, **_kwargs):
    raise AssertionError("restart_test_network_disabled")


def _counts(path: str, connect) -> dict[str, int]:
    with connect(path) as connection:
        return {
            table: int(connection.execute(
                f"SELECT count(*) FROM {table}"
            ).fetchone()[0])
            for table in _TABLES
        }


def _run() -> None:
    payload = _bounded_payload()
    socket.socket.connect = _network_blocked
    socket.socket.connect_ex = _network_blocked
    socket.create_connection = _network_blocked
    socket.getaddrinfo = _network_blocked

    path = payload["path"]
    phase = payload["phase"]
    secret = payload["secret"]
    if phase == "complete":
        with channel_store.connect(path) as connection:
            connection.execute(channel_store.RELAY_TABLE_DDL["messages"])
        channel_store.run_migrations(path)

    runtime = bootstrap_runtime(path, memory_config(secret=secret))
    explicit = importlib.import_module("backend.memory_explicit_actions")
    explicit = importlib.reload(explicit)
    backend = explicit.create_entry_backend(runtime.privileged_actions)
    service = explicit.bind_operator_cli(backend)

    if phase == "complete":
        before = _counts(path, channel_store.connect)
        remembered = service.remember_explicit_user_memory(
            explicit.RememberExplicitMemoryRequest(
                payload["remember_request_id"],
                "project",
                "global_user",
                "",
                payload["content"],
                "normal",
            )
        )
        request = explicit.ForgetExplicitMemoryRequest(
            payload["forget_request_id"],
            remembered.memory_key,
        )
        result = service.forget_explicit_user_memory(request)
        output = {
            "request_id": payload["forget_request_id"],
            "memory_key": remembered.memory_key,
            "category": result.category,
            "replayed": result.replayed,
            "counts_before": before,
            "counts_after": _counts(path, channel_store.connect),
            "result_repr": repr(result),
            "service_repr": repr(service),
        }
    else:
        from backend.tests.test_memory_explicit_actions import (
            ExplicitMemoryActionBackendTests,
            _ForgetSqlAuthorizerGate,
        )

        store = backend._store
        actions = backend._actions
        store_type = type(store)
        action_type = type(actions)
        store_module = importlib.import_module(store_type.__module__)
        ledger_module = store_module.memory_action_ledger
        runtime_module = importlib.import_module(
            type(actions._authority).__module__
        )
        original_connect = store_module.channel_store.connect
        original_lookup = (
            ledger_module._MemoryActionUnitOfWork.lookup_forget_terminal
        )
        gate = _ForgetSqlAuthorizerGate(
            ExplicitMemoryActionBackendTests._forget_sql_fingerprints(),
            ExplicitMemoryActionBackendTests._forget_sql_write_fingerprints(),
            ExplicitMemoryActionBackendTests._sql_fingerprint,
        )
        registration_absent = {"before": False, "after": False}

        def gated_connect(*args, **kwargs):
            return gate.wrap(original_connect(*args, **kwargs))

        def inspect_lookup(uow, **kwargs):
            registration_absent["before"] = (
                uow._forget_target_metadata_identity is None
                and uow._forget_target_registration is None
            )
            value = original_lookup(uow, **kwargs)
            registration_absent["after"] = (
                uow._forget_target_metadata_identity is None
                and uow._forget_target_registration is None
            )
            return value

        before = _counts(path, original_connect)
        request = explicit.ForgetExplicitMemoryRequest(
            payload["forget_request_id"],
            payload["memory_key"],
        )
        with (
            mock.patch.object(
                store_module.channel_store,
                "connect",
                new=gated_connect,
            ),
            mock.patch.object(
                ledger_module._MemoryActionUnitOfWork,
                "lookup_forget_terminal",
                new=inspect_lookup,
            ),
            mock.patch.object(
                ledger_module._MemoryActionUnitOfWork,
                "_register_forget_target",
                side_effect=AssertionError("restart_replay_registered_target"),
            ),
            mock.patch.object(
                ledger_module._MemoryActionUnitOfWork,
                "_seal_registered_forget_target",
                side_effect=AssertionError("restart_replay_sealed_target"),
            ),
            mock.patch.object(
                store_type,
                "_get_forget_target_metadata",
                side_effect=AssertionError("restart_replay_executed_A"),
            ),
            mock.patch.object(
                action_type,
                "forget_explicit_user_memory",
                side_effect=AssertionError("restart_replay_executed_store"),
            ),
            mock.patch.object(
                runtime_module,
                "issue_action_envelope",
                side_effect=AssertionError("restart_replay_issued_capability"),
            ),
            mock.patch.object(
                store_type,
                "_finish_action",
                side_effect=AssertionError("restart_replay_consumed_capability"),
            ),
        ):
            result = service.forget_explicit_user_memory(request)
        output = {
            "category": result.category,
            "replayed": result.replayed,
            "sequence": gate.sequence,
            "write_sequence": gate.write_sequence,
            "gate_violation": gate.violation,
            "registration_absent": registration_absent,
            "counts_before": before,
            "counts_after": _counts(path, original_connect),
            "result_repr": repr(result),
            "service_repr": repr(service),
        }
    json.dump(output, sys.stdout, sort_keys=True)


if __name__ == "__main__":
    try:
        _run()
    except SystemExit:
        raise
    except Exception:
        raise SystemExit("restart_worker_failed") from None
