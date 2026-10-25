"""Experimental, process-local two-stage confirmation for session memories.

THIS IS NOT AN AUTHENTICATION PROVIDER OR AN HTTP ENDPOINT.
Only a trusted server composition root may create and invoke this coordinator.
User confirmation must be obtained by a future separately reviewed UI/relay
workflow. A protocol confirmation string cannot prove a human clicked.

No DB or network access during prepare; commit delegates to the existing
HMAC-authenticated SQLite action ledger, which owns atomicity/idempotency.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from . import memory_policy
from .memory_action_ledger import REQUEST_ID_PATTERN
from .memory_explicit_actions import (
    ExplicitMemoryActionError,
    RememberExplicitMemoryRequest,
)
from .memory_session_write_guard import (
    SessionWriteBindingError,
    verify_server_session_write,
)

TTL_SECONDS = 120
MAX_PENDING = 8
MAX_CONTENT_CHARS = 1000
CONFIRMATION_TEXT = "CONFIRM MEMORY"


class SessionConfirmationError(RuntimeError):
    def __init__(self, category: str = "session_confirmation_invalid"):
        super().__init__(category)


@dataclass(frozen=True, slots=True, repr=False)
class ConfirmationOffer:
    ticket: str = field(repr=False)
    preview_content: str = field(repr=False)
    content_sha256: str = field(repr=False)
    expires_in_seconds: int = TTL_SECONDS
    confirmation_text: str = CONFIRMATION_TEXT

    def __repr__(self):
        return "<ConfirmationOffer>"


@dataclass(slots=True, repr=False)
class _Pending:
    request: RememberExplicitMemoryRequest = field(repr=False)
    session_id: str = field(repr=False)
    content_sha256: str = field(repr=False)
    expires_at: float = field(repr=False)


def _digest(request: RememberExplicitMemoryRequest) -> str:
    # Used for preview equality, NOT user authorization. The existing ledger
    # independently HMAC-binds the request/terminal with a runtime secret.
    canonical = json.dumps(
        [
            request.request_id,
            request.kind,
            request.scope_type,
            request.scope_ref,
            request.content,
            request.sensitivity,
        ],
        ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _enabled() -> bool:
    return (
        os.environ.get("MEMORY_SESSION_BOUND_WRITE_ENABLED") == "true"
        and os.environ.get("MEMORY_SESSION_CONFIRMATION_ENABLED") == "true"
    )


class SessionWriteConfirmation:
    """In-memory short-lived one-use acknowledgement, fail closed.

    session_provider must be an authorized, server-owned callable returning
    (explicitly_configured_active_session, canonical_session_rows). It must
    NEVER be wired to untrusted body/query/header/session claims.
    """

    __slots__ = ("_service", "_session_provider", "_clock", "_lock", "_pending")

    def __init__(
        self,
        *,
        service: object,
        session_provider: Callable[[], tuple[str, Iterable[dict]]],
        clock: Callable[[], float] = time.monotonic,
    ):
        if not callable(session_provider) or not callable(clock):
            raise SessionConfirmationError()
        self._service = service
        self._session_provider = session_provider
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}

    def _context(self) -> tuple[str, Iterable[dict]]:
        try:
            data = self._session_provider()
            if type(data) is not tuple or len(data) != 2:
                raise SessionConfirmationError()
            return data
        except SessionConfirmationError:
            raise
        except Exception:
            raise SessionConfirmationError() from None

    def prepare(self, request: RememberExplicitMemoryRequest) -> ConfirmationOffer:
        """Prepare a preview but perform NO write or memory action ledger claim."""
        if not _enabled():
            self.clear_pending()
            raise SessionConfirmationError("session_confirmation_disabled")
        if (
            type(request) is not RememberExplicitMemoryRequest
            or type(request.request_id) is not str
            or REQUEST_ID_PATTERN.fullmatch(request.request_id) is None
            or type(request.kind) is not str
            or request.kind not in memory_policy.KINDS - {"assistant_experience"}
            or type(request.content) is not str
            or not (1 <= len(request.content) <= MAX_CONTENT_CHARS)
            or request.sensitivity != "normal"
        ):
            raise SessionConfirmationError()
        try:
            if memory_policy.normalize_content(
                request.content, max_chars=MAX_CONTENT_CHARS
            ) != request.content:
                raise SessionConfirmationError()
            session_id, canonical_sessions = self._context()
            verify_server_session_write(
                request,
                trusted_session_id=session_id,
                canonical_sessions=canonical_sessions,
            )
        except (
            SessionWriteBindingError,
            memory_policy.MemoryPolicyError,
        ):
            raise SessionConfirmationError() from None
        digest = _digest(request)
        with self._lock:
            current = self._clock()
            for ticket, item in list(self._pending.items()):
                if item.expires_at <= current:
                    del self._pending[ticket]
            if len(self._pending) >= MAX_PENDING:
                raise SessionConfirmationError("session_confirmation_capacity")
            ticket = secrets.token_urlsafe(32)
            while ticket in self._pending:
                ticket = secrets.token_urlsafe(32)
            self._pending[ticket] = _Pending(
                request=request,
                session_id=session_id,
                content_sha256=digest,
                expires_at=current + TTL_SECONDS,
            )
        return ConfirmationOffer(
            ticket=ticket,
            preview_content=request.content,
            content_sha256=digest,
        )

    def confirm(
        self,
        *,
        ticket: str,
        content_sha256: str,
        confirmation_text: str,
    ):
        """Consume ticket BEFORE invoking the write, preventing double execution.

        If any step fails the ticket is not reusable. An uncertain write outcome
        can only be investigated with the original request_id in the existing
        action ledger; the coordinator never automatically retries.
        """
        if not _enabled():
            self.clear_pending()
            raise SessionConfirmationError("session_confirmation_disabled")
        if (
            type(ticket) is not str
            or len(ticket) > 128
            or type(content_sha256) is not str
            or len(content_sha256) != 64
            or type(confirmation_text) is not str
        ):
            raise SessionConfirmationError()
        with self._lock:
            pending = self._pending.pop(ticket, None)
        if pending is None:
            raise SessionConfirmationError()
        if (
            pending.expires_at <= self._clock()
            or not hmac.compare_digest(content_sha256, pending.content_sha256)
            or not hmac.compare_digest(confirmation_text, CONFIRMATION_TEXT)
        ):
            raise SessionConfirmationError()
        try:
            session_id, canonical_sessions = self._context()
            if session_id != pending.session_id:
                raise SessionConfirmationError()
            verify_server_session_write(
                pending.request,
                trusted_session_id=session_id,
                canonical_sessions=canonical_sessions,
            )
        except SessionWriteBindingError:
            raise SessionConfirmationError() from None
        # Fail closed if either control gate changed during session checking.
        if not _enabled():
            raise SessionConfirmationError("session_confirmation_disabled")
        # Preserve existing SQLite transaction and HMAC replay semantics.
        # Service's own flag/origin guard is checked again at commit time.
        return self._service.remember_explicit_user_memory_for_server_session(
            pending.request,
            trusted_session_id=session_id,
            canonical_sessions=canonical_sessions,
        )

    def clear_pending(self) -> None:
        """Discard every unconfirmed offer (e.g. on logout), no database write."""
        with self._lock:
            self._pending.clear()
