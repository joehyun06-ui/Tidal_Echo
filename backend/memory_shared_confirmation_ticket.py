"""Offline, default-OFF SQLite one-use confirmation ticket claim experiment.

Does not authenticate a user, serve an HTTP endpoint, prove human consent, or
call memory writes. All providers must be trusted server-only components.
The claim and auth-epoch validation share a SQLite BEGIN IMMEDIATE transaction,
but any LATER memory write does not: production revoke-vs-write safety remains
unresolved. Never wire this experiment into production.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import memory_policy
from .memory_action_ledger import REQUEST_ID_PATTERN
from .memory_explicit_actions import RememberExplicitMemoryRequest
from .memory_session_confirmation import (
    ConfirmationOffer, MAX_CONTENT_CHARS, MAX_PENDING, TTL_SECONDS, _digest,
)
from .memory_session_write_guard import (
    SessionWriteBindingError, verify_server_session_write,
)

_FLAG = "MEMORY_SHARED_CONFIRMATION_TICKETS_ENABLED"
_PRINCIPAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_LOGIN = re.compile(r"[A-Za-z0-9_-]{24,96}\Z", re.ASCII)
_EPOCH = re.compile(r"[A-Za-z0-9_-]{24,96}\Z", re.ASCII)
_TICKET = re.compile(r"[A-Za-z0-9_-]{32,128}\Z", re.ASCII)
_DIGEST = re.compile(r"[a-f0-9]{64}\Z", re.ASCII)
_RETENTION_SECONDS = 86400
_DDL = """
CREATE TABLE IF NOT EXISTS memory_shared_ticket_v1 (
    ticket_sha256 TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    login_id TEXT NOT NULL,
    epoch_sha256 TEXT NOT NULL,
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    expires_at REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','consumed')),
    consumed_at REAL
)
"""


class SharedTicketError(RuntimeError):
    def __init__(self):
        super().__init__("memory_shared_confirmation_unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class TrustedTicketContext:
    """SERVER-OWNED snapshot; constructing this object is not authentication."""
    principal_id: str = field(repr=False)
    login_id: str = field(repr=False)
    auth_epoch: str = field(repr=False)
    session_id: str = field(repr=False)
    canonical_sessions: tuple[dict, ...] = field(repr=False)

    def __repr__(self):
        return "<TrustedTicketContext>"


@dataclass(frozen=True, slots=True, repr=False)
class TicketClaimReceipt:
    """Claim is consumed; NO memory write and NO human consent proof."""
    request_id: str = field(repr=False)

    def __repr__(self):
        return "<TicketClaimReceipt>"


def _enabled() -> bool:
    return (
        os.environ.get(_FLAG) == "true"
        and os.environ.get("MEMORY_TRUSTED_AUTH_EPOCH_ENABLED") == "true"
    )


def _valid(value: object, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _request_valid(request: object) -> bool:
    if (
        type(request) is not RememberExplicitMemoryRequest
        or not _valid(request.request_id, REQUEST_ID_PATTERN)
        or type(request.kind) is not str
        or request.kind not in memory_policy.KINDS - {"assistant_experience"}
        or type(request.content) is not str
        or not 1 <= len(request.content) <= MAX_CONTENT_CHARS
        or request.sensitivity != "normal"
    ):
        return False
    try:
        return memory_policy.normalize_content(
            request.content, max_chars=MAX_CONTENT_CHARS
        ) == request.content
    except Exception:
        return False


class SharedConfirmationTickets:
    """Prototype storage for one-use tickets across workers sharing one DB file.

    The DB path MUST be the SAME SQLite database used by AuthEpochRegistry;
    it locks the same file so claim and epoch revocation serialize. This
    component itself is not wired to the real memory action transaction.
    """

    __slots__ = ("_path", "_context_provider", "_clock")

    def __init__(
        self, *, path: str | Path,
        context_provider: Callable[[], TrustedTicketContext],
        clock: Callable[[], float] = time.time,
    ):
        if (
            not isinstance(path, (str, Path))
            or not Path(path).is_absolute()
            or not callable(context_provider)
            or not callable(clock)
        ):
            raise SharedTicketError()
        self._path = Path(path)
        self._context_provider = context_provider
        self._clock = clock

    def _now(self) -> float:
        try:
            now = self._clock()
            if type(now) not in (float, int) or not math.isfinite(now) or now < 0:
                raise SharedTicketError()
            return float(now)
        except Exception:
            raise SharedTicketError() from None

    def _context(self, request: object) -> TrustedTicketContext:
        try:
            context = self._context_provider()
            if (
                type(context) is not TrustedTicketContext
                or not _valid(context.principal_id, _PRINCIPAL)
                or not _valid(context.login_id, _LOGIN)
                or not _valid(context.auth_epoch, _EPOCH)
                or type(context.canonical_sessions) is not tuple
                or not _request_valid(request)
            ):
                raise SharedTicketError()
            verify_server_session_write(
                request,
                trusted_session_id=context.session_id,
                canonical_sessions=context.canonical_sessions,
            )
            return context
        except (SessionWriteBindingError, Exception):
            raise SharedTicketError() from None

    @staticmethod
    def _epoch_valid_locked(conn: sqlite3.Connection, context: TrustedTicketContext) -> bool:
        try:
            row = conn.execute(
                """SELECT epoch_sha256, revoked FROM memory_auth_epoch_v1
                   WHERE principal_id=? AND login_id=?""",
                (context.principal_id, context.login_id),
            ).fetchone()
            actual = hashlib.sha256(context.auth_epoch.encode("ascii")).hexdigest()
            return bool(
                row is not None and row[1] == 0 and type(row[0]) is str
                and hmac.compare_digest(actual, row[0])
            )
        except (sqlite3.Error, TypeError, ValueError):
            return False

    def _transaction(self, func):
        if not _enabled():
            raise SharedTicketError()
        try:
            conn = sqlite3.connect(str(self._path), timeout=5, isolation_level=None)
            try:
                conn.execute("BEGIN IMMEDIATE")
                # Recheck OFF gate after any wait for the SQLite write lock.
                if not _enabled():
                    raise SharedTicketError()
                conn.execute(_DDL)
                result = func(conn)
                conn.execute("COMMIT")
                return result
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
        except (sqlite3.Error, ValueError, TypeError):
            raise SharedTicketError() from None

    def prepare(self, request: RememberExplicitMemoryRequest) -> ConfirmationOffer:
        """Store only hashes + server identity metadata; zero memory writes."""
        if not _enabled():
            raise SharedTicketError()
        if not _request_valid(request):
            raise SharedTicketError()
        token = secrets.token_urlsafe(32)
        token_sha = hashlib.sha256(token.encode("ascii")).hexdigest()
        digest = _digest(request)

        def op(conn):
            # Re-read trusted session context only AFTER the SQLite lock.
            # A pre-lock snapshot can be stale after a blocked transaction.
            context = self._context(request)
            if not _enabled():
                raise SharedTicketError()
            now = self._now()
            if not self._epoch_valid_locked(conn, context):
                raise SharedTicketError()
            epoch_sha = hashlib.sha256(context.auth_epoch.encode("ascii")).hexdigest()
            conn.execute(
                """DELETE FROM memory_shared_ticket_v1
                   WHERE (status='pending' AND expires_at<=?)
                      OR (status='consumed' AND consumed_at<?)""",
                (now, now - _RETENTION_SECONDS),
            )
            count = conn.execute(
                "SELECT count(*) FROM memory_shared_ticket_v1 WHERE status='pending'"
            ).fetchone()[0]
            if count >= MAX_PENDING:
                raise SharedTicketError()
            conn.execute(
                """INSERT INTO memory_shared_ticket_v1
                   (ticket_sha256,principal_id,login_id,epoch_sha256,
                    session_id,request_id,content_sha256,expires_at,status,consumed_at)
                   VALUES (?,?,?,?,?,?,?,?, 'pending',NULL)""",
                (token_sha, context.principal_id, context.login_id, epoch_sha,
                 context.session_id, request.request_id, digest, now + TTL_SECONDS),
            )

        self._transaction(op)
        return ConfirmationOffer(
            ticket=token, preview_content=request.content, content_sha256=digest,
        )

    def claim(
        self, *, ticket: str, request: RememberExplicitMemoryRequest,
        content_sha256: str, confirmation_text: str,
    ) -> TicketClaimReceipt:
        """Atomically burn ticket even for wrong confirmation; NO memory write.

        Successful claim means only internal confirmation payload was
        consistent, not that user personally consented. Existing memory
        ledger is NOT called or modified.
        """
        if not _enabled() or not _valid(ticket, _TICKET):
            raise SharedTicketError()
        token_sha = hashlib.sha256(ticket.encode("ascii")).hexdigest()

        def op(conn):
            # Time and trusted session must be read after the write lock;
            # a session switch during lock contention must not be ignored.
            now = self._now()
            try:
                context = self._context(request)
            except SharedTicketError:
                context = None
            if not _enabled():
                raise SharedTicketError()
            supplied_digest = _digest(request) if context is not None else None
            row = conn.execute(
                """SELECT principal_id,login_id,epoch_sha256,session_id,
                          request_id,content_sha256,expires_at
                   FROM memory_shared_ticket_v1
                   WHERE ticket_sha256=? AND status='pending'""",
                (token_sha,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                """UPDATE memory_shared_ticket_v1 SET status='consumed',consumed_at=?
                   WHERE ticket_sha256=? AND status='pending'""",
                (now, token_sha),
            )
            return bool(
                context is not None
                and self._epoch_valid_locked(conn, context)
                and row[6] > now
                and row[0] == context.principal_id
                and row[1] == context.login_id
                and hmac.compare_digest(
                    row[2], hashlib.sha256(context.auth_epoch.encode("ascii")).hexdigest()
                )
                and row[3] == context.session_id
                and row[4] == request.request_id
                and type(row[5]) is str
                and hmac.compare_digest(row[5], supplied_digest)
                and _valid(content_sha256, _DIGEST)
                and hmac.compare_digest(row[5], content_sha256)
                and type(confirmation_text) is str
                and hmac.compare_digest(confirmation_text, "CONFIRM MEMORY")
            )

        if not self._transaction(op):
            raise SharedTicketError()
        return TicketClaimReceipt(request_id=request.request_id)
