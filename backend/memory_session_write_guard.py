"""Preflight for an opt-in *server-owned* canonical session write.

Not an HTTP endpoint and not a user-input authorizer. The caller MUST have
already authenticated the requester, obtained trusted_session_id from server
context (never request JSON), and read canonical_sessions from the same server.
"""
from __future__ import annotations

from collections.abc import Iterable

from .memory_evidence_readonly import SESSION_PATTERN


class SessionWriteBindingError(RuntimeError):
    def __init__(self, category: str = "session_write_binding_invalid"):
        super().__init__(category)


MAX_CANONICAL_SESSIONS = 512


def verify_server_session_write(
    request: object,
    *,
    trusted_session_id: str,
    canonical_sessions: Iterable[dict],
) -> str:
    """Verify session identity and immutable explicit-memory scope preflight.

    Preflight is deliberately restrictive: session-scoped remember only,
    no global, correction, deletion, project/channel scope, or untrusted role.
    Caller owns the service authentication boundary.
    """
    from .memory_explicit_actions import RememberExplicitMemoryRequest
    if (
        type(request) is not RememberExplicitMemoryRequest
        or type(trusted_session_id) is not str
        or SESSION_PATTERN.fullmatch(trusted_session_id) is None
        or request.scope_type != "session"
        or type(request.scope_ref) is not str
        or request.scope_ref != trusted_session_id
    ):
        raise SessionWriteBindingError()
    try:
        if isinstance(canonical_sessions, (str, bytes, dict)):
            raise SessionWriteBindingError()
        iterator = iter(canonical_sessions)
        seen: set[str] = set()
        count = 0
        for entry in iterator:
            count += 1
            if count > MAX_CANONICAL_SESSIONS:
                raise SessionWriteBindingError()
            if (
                type(entry) is not dict or type(entry.get("id")) is not str
                or SESSION_PATTERN.fullmatch(entry["id"]) is None
                or entry["id"] in seen
            ):
                raise SessionWriteBindingError()
            seen.add(entry["id"])
    except (TypeError, ValueError, RuntimeError) as error:
        raise SessionWriteBindingError() from None
    if trusted_session_id not in seen:
        raise SessionWriteBindingError()
    return trusted_session_id
