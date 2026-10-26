"""Default-OFF same-transaction confirmation gate for a future memory action.

Review-only integration seam; no production memory write call is wired. The
gate validates a still-pending ticket and live auth epoch *inside* the real
_MemoryActionUnitOfWork's SQLite write transaction and marks the ticket
consumed there. The existing UOW owns commit/rollback, never this module.

IMPORTANT: This dataclass provider is NOT authentication or human consent.
A successful receipt alone must never grant a memory write. Requires separate
review of the surrounding writer, crash recovery, and request provenance.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .memory_action_ledger import _MemoryActionUnitOfWork
from .memory_explicit_actions import RememberExplicitMemoryRequest
from .memory_session_confirmation import CONFIRMATION_TEXT, _digest
from .memory_session_write_guard import verify_server_session_write
from .memory_shared_confirmation_ticket import TrustedTicketContext

_FLAG = "MEMORY_CONFIRMED_UOW_GATE_ENABLED"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_LOGIN = re.compile(r"[A-Za-z0-9_-]{24,96}\Z", re.ASCII)
_EPOCH = re.compile(r"[A-Za-z0-9_-]{24,96}\Z", re.ASCII)
_TICKET = re.compile(r"[A-Za-z0-9_-]{32,128}\Z", re.ASCII)
_DIGEST = re.compile(r"[a-f0-9]{64}\Z", re.ASCII)


class UowConfirmationGateError(RuntimeError):
    def __init__(self):
        super().__init__("memory_uow_confirmation_unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class CheckedInTransaction:
    """Test-only return marker; not a commit authorization capability."""
    request_id: str = field(repr=False)

    def __repr__(self):
        return "<CheckedInTransaction>"


def _enabled() -> bool:
    return (
        os.environ.get(_FLAG) == "true"
        and os.environ.get("MEMORY_SHARED_CONFIRMATION_TICKETS_ENABLED") == "true"
        and os.environ.get("MEMORY_TRUSTED_AUTH_EPOCH_ENABLED") == "true"
    )


def _matches(obj: object, pattern: re.Pattern[str]) -> bool:
    return type(obj) is str and pattern.fullmatch(obj) is not None


class MemoryUowConfirmationGate:
    """No public route; no memory write; no stand-alone transaction.

    Inject only from trusted server composition, with provider values derived
    from an already verified and active login/session. Never construct from
    request JSON/HTTP headers. This component can consume a ticket inside a
    real _MemoryActionUnitOfWork, but cannot itself commit a memory.
    """

    __slots__ = ("_path", "_context_provider", "_clock")

    def __init__(
        self, *, path: str | Path,
        context_provider: Callable[[], TrustedTicketContext],
        clock: Callable[[], float] = time.time,
    ):
        if (
            not isinstance(path, (str, Path)) or not Path(path).is_absolute()
            or not callable(context_provider) or not callable(clock)
        ):
            raise UowConfirmationGateError()
        self._path = Path(path).resolve()
        self._context_provider = context_provider
        self._clock = clock

    def _context(self, request: RememberExplicitMemoryRequest) -> TrustedTicketContext:
        try:
            ctx = self._context_provider()
            if (
                type(ctx) is not TrustedTicketContext
                or not _matches(ctx.principal_id, _ID)
                or not _matches(ctx.login_id, _LOGIN)
                or not _matches(ctx.auth_epoch, _EPOCH)
                or type(ctx.canonical_sessions) is not tuple
            ):
                raise UowConfirmationGateError()
            verify_server_session_write(
                request,
                trusted_session_id=ctx.session_id,
                canonical_sessions=ctx.canonical_sessions,
            )
            return ctx
        except Exception:
            raise UowConfirmationGateError() from None

    def _now(self) -> float:
        try:
            now = self._clock()
            if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
                raise UowConfirmationGateError()
            return float(now)
        except Exception:
            raise UowConfirmationGateError() from None

    def consume_in_existing_uow(
        self, *, uow: _MemoryActionUnitOfWork,
        request: RememberExplicitMemoryRequest,
        ticket: str, content_sha256: str,
        confirmation_text: str,
    ) -> CheckedInTransaction:
        """Validate and consume before future canonical writes, within UOW.

        Caller must use this EXACT UOW for the subsequent write/commit.
        This method does not do so, and is not production-authorized.
        Invalid confirmation rolls back with the enclosing UOW; unlike the
        old two-transaction prototype, failure here does not durably burn
        a ticket unless the owner independently commits a terminal.
        """
        if (
            not _enabled()
            or type(uow) is not _MemoryActionUnitOfWork
            or type(request) is not RememberExplicitMemoryRequest
            or not _matches(ticket, _TICKET)
            or not _matches(content_sha256, _DIGEST)
            or type(confirmation_text) is not str
        ):
            raise UowConfirmationGateError()
        try:
            # Never trust a foreign UOW pointing to a different SQLite file.
            if Path(uow._store.path).resolve() != self._path:
                raise UowConfirmationGateError()
            uow._require_active()  # must already hold BEGIN IMMEDIATE
            # Security-critical checks occur only after acquiring the lock.
            if not _enabled():
                raise UowConfirmationGateError()
            ctx = self._context(request)
            current = self._now()
            if (
                not hmac.compare_digest(content_sha256, _digest(request))
                or not hmac.compare_digest(confirmation_text, CONFIRMATION_TEXT)
            ):
                raise UowConfirmationGateError()

            ticket_sha = hashlib.sha256(ticket.encode("ascii")).hexdigest()
            epoch_sha = hashlib.sha256(ctx.auth_epoch.encode("ascii")).hexdigest()
            # Both rows are inspected on the SAME UOW sqlite connection.
            row = uow._execute(
                """SELECT t.principal_id,t.login_id,t.epoch_sha256,
                          t.session_id,t.request_id,t.content_sha256,
                          t.expires_at,t.status,e.epoch_sha256,e.revoked
                   FROM memory_shared_ticket_v1 AS t
                   LEFT JOIN memory_auth_epoch_v1 AS e
                     ON t.principal_id=e.principal_id AND t.login_id=e.login_id
                   WHERE t.ticket_sha256=?""",
                (ticket_sha,),
            ).fetchone()
            if not (
                row is not None
                and row[0] == ctx.principal_id and row[1] == ctx.login_id
                and type(row[2]) is str and hmac.compare_digest(row[2], epoch_sha)
                and row[3] == ctx.session_id and row[4] == request.request_id
                and type(row[5]) is str and hmac.compare_digest(row[5], content_sha256)
                and type(row[6]) in (int, float) and row[6] > current
                and row[7] == "pending"
                and type(row[8]) is str and hmac.compare_digest(row[8], epoch_sha)
                and row[9] == 0
            ):
                raise UowConfirmationGateError()
            updated = uow._execute(
                """UPDATE memory_shared_ticket_v1
                   SET status='consumed',consumed_at=?
                   WHERE ticket_sha256=? AND status='pending'""",
                (current, ticket_sha),
            )
            if updated.rowcount != 1:
                raise UowConfirmationGateError()
            return CheckedInTransaction(request_id=request.request_id)
        except Exception:
            raise UowConfirmationGateError() from None
