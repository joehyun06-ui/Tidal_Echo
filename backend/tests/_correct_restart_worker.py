"""Isolated test worker for authenticated Correct process replay."""

from __future__ import annotations

import json
import os
import re
import socket
import sys

from backend import (
    memory_explicit_actions,
    memory_operator_composition,
    telegram_integration,
)


_MAX_STDIN_BYTES = 16 * 1024
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{32,96}\Z")
_MEMORY_KEY = re.compile(r"[A-Za-z0-9_-]{32,96}\Z")
_FIELDS = frozenset(
    {
        "request_id",
        "memory_key",
        "replacement_content",
        "sensitivity",
    }
)


def _payload_error() -> None:
    raise SystemExit("correct_restart_payload_invalid")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _payload_error()
        result[key] = value
    return result


def _bounded_payload() -> dict[str, str]:
    raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
    if not raw or len(raw) > _MAX_STDIN_BYTES or raw.startswith(b"\xef\xbb\xbf"):
        _payload_error()
    try:
        payload = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=lambda _value: _payload_error(),
        )
    except (TypeError, UnicodeError, ValueError, json.JSONDecodeError):
        _payload_error()
    if (
        type(payload) is not dict
        or frozenset(payload) != _FIELDS
        or any(type(payload[name]) is not str for name in _FIELDS)
        or _REQUEST_ID.fullmatch(payload["request_id"]) is None
        or _MEMORY_KEY.fullmatch(payload["memory_key"]) is None
        or not payload["replacement_content"]
        or len(payload["replacement_content"]) > 4096
        or payload["sensitivity"] not in {"normal", "sensitive", "restricted"}
    ):
        _payload_error()
    return payload


def _network_blocked(*_args, **_kwargs):
    raise AssertionError("correct_restart_network_disabled")


def _run() -> None:
    payload = _bounded_payload()
    socket.socket.connect = _network_blocked
    socket.socket.connect_ex = _network_blocked
    socket.create_connection = _network_blocked
    socket.getaddrinfo = _network_blocked
    environ = dict(os.environ)
    telegram_config = telegram_integration.TelegramConfig.from_env(environ)
    service = (
        memory_operator_composition
        .compose_operator_memory_service_from_environment(
            telegram_config,
            environ,
        )
    )
    request = memory_explicit_actions.CorrectExplicitMemoryRequest(
        payload["request_id"],
        payload["memory_key"],
        payload["replacement_content"],
        payload["sensitivity"],
    )
    result = service.correct_explicit_user_memory(request)
    output = {
        "category": result.category,
        "memory_key": result.memory_key,
        "replayed": result.replayed,
        "result_repr": repr(result),
        "service_repr": repr(service),
    }
    sys.stdout.buffer.write(
        (
            json.dumps(output, ensure_ascii=True, separators=(",", ":"))
            + "\n"
        ).encode("ascii")
    )


if __name__ == "__main__":
    try:
        _run()
    except SystemExit:
        raise
    except memory_operator_composition.MemoryOperatorCompositionError:
        raise SystemExit("correct_restart_composition_failed") from None
    except memory_explicit_actions.ExplicitMemoryActionError:
        raise SystemExit("correct_restart_action_failed") from None
    except Exception:
        raise SystemExit("correct_restart_worker_failed") from None
