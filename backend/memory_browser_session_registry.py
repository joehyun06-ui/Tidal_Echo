"""Offline per-user browser credential registry. Default OFF, no HTTP routes.

An independent authenticated LOGIN event must supply the trusted context.
The legacy relay-wide Bearer secret must NEVER issue one of these sessions.
A synthetic context callback is not real authentication.  Tokens are
random, stored only as SHA-256, checked with durable login epoch under one
SQLite BEGIN IMMEDIATE. This module cannot authorize memory writing.
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
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from starlette.requests import Request

from .memory_browser_consent_review import _valid_origin
from .memory_explicit_actions import RememberExplicitMemoryRequest
from .memory_session_write_guard import verify_server_session_write
from .memory_shared_confirmation_ticket import TrustedTicketContext

_FLAG = "MEMORY_BROWSER_SESSION_REGISTRY_REVIEW_ENABLED"
_COOKIE = "__Host-gtmem-review"
_PATH = "/__offline_review__/memory/session"
_TTL = 900
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z", re.ASCII)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_LOGIN = re.compile(r"[A-Za-z0-9_-]{24,96}\Z", re.ASCII)
_DDL = """
CREATE TABLE IF NOT EXISTS memory_browser_login_session_v1 (
    token_sha256 TEXT PRIMARY KEY,
    csrf_sha256 TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    login_id TEXT NOT NULL,
    epoch_sha256 TEXT NOT NULL,
    session_id TEXT NOT NULL,
    origin_sha256 TEXT NOT NULL,
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0,1))
)
"""


class BrowserLoginSessionError(RuntimeError):
    def __init__(self):
        super().__init__("memory_browser_login_session_unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class BrowserSessionSecrets:
    """Test-issued only. HTTP cookie transport is NOT implemented here."""
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)

    def __repr__(self):
        return "<BrowserSessionSecrets>"


@dataclass(frozen=True, slots=True, repr=False)
class BrowserSessionReview:
    """No reusable grant, no write path, no production authentication claim."""
    verified_for_review: bool = True
    may_write_memory: bool = False

    def __repr__(self):
        return "<BrowserSessionReview>"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _valid(value: object, regex: re.Pattern[str]) -> bool:
    return type(value) is str and regex.fullmatch(value) is not None


def _enabled() -> bool:
    return (
        os.environ.get(_FLAG) == "true"
        and os.environ.get("MEMORY_TRUSTED_AUTH_EPOCH_ENABLED") == "true"
    )


class BrowserLoginSessionRegistry:
    """Experimental same-database login/epoch session store, no web route.

    login_event_provider MUST originate in a separately authenticated login
    service; it is called ONLY when issuing a session. No default provider.
    canonical_sessions_provider MUST read server-owned canonical session rows.
    """

    __slots__ = (
        "_path", "_origin", "_login_event", "_sessions", "_clock",
    )

    def __init__(
        self, *, path: str | Path, expected_origin: str,
        login_event_provider: Callable[[], TrustedTicketContext] | None = None,
        canonical_sessions_provider: Callable[[], tuple[dict, ...]] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        if (
            not isinstance(path, (str, Path))
            or not Path(path).is_absolute()
            or not _valid_origin(expected_origin)
            or not callable(clock)
        ):
            raise BrowserLoginSessionError()
        self._path = Path(path).resolve()
        self._origin = expected_origin
        self._login_event = login_event_provider
        self._sessions = canonical_sessions_provider
        self._clock = clock

    def _now(self):
        try:
            now = self._clock()
            if type(now) not in (float, int) or not math.isfinite(now) or now < 0:
                raise BrowserLoginSessionError()
            return float(now)
        except Exception:
            raise BrowserLoginSessionError() from None

    def _transaction(self, func, *, create_table=False):
        if not _enabled():
            raise BrowserLoginSessionError()
        try:
            with closing(sqlite3.connect(
                str(self._path), isolation_level=None, timeout=5.0,
            )) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if create_table:
                        conn.execute(_DDL)
                    result = func(conn)
                    if not _enabled():
                        raise BrowserLoginSessionError()
                    conn.execute("COMMIT")
                    return result
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
        except Exception:
            raise BrowserLoginSessionError() from None

    def _epoch_current(self, conn, principal, login, epoch_hash):
        row = conn.execute(
            """SELECT epoch_sha256,revoked FROM memory_auth_epoch_v1
               WHERE principal_id=? AND login_id=?""",
            (principal, login),
        ).fetchone()
        if (
            row is None or row[1] != 0 or type(row[0]) is not str
            or not hmac.compare_digest(row[0], epoch_hash)
        ):
            raise BrowserLoginSessionError()

    def _canonical(self, session_id, request=None):
        if not callable(self._sessions):
            raise BrowserLoginSessionError()
        from .memory_session_write_guard import verify_trusted_active_session
        rows = self._sessions()
        if type(rows) is not tuple:
            raise BrowserLoginSessionError()
        verify_trusted_active_session(
            trusted_session_id=session_id, canonical_sessions=rows,
        )
        if request is not None:
            verify_server_session_write(
                request, trusted_session_id=session_id,
                canonical_sessions=rows,
            )

    def _insert(self, conn, *, principal, login, epoch_hash, session_id, now):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        conn.execute(
            """INSERT INTO memory_browser_login_session_v1
               (token_sha256,csrf_sha256,principal_id,login_id,epoch_sha256,
                session_id,origin_sha256,issued_at,expires_at,revoked)
               VALUES (?,?,?,?,?,?,?,?,?,0)""",
            (
                _sha(token), _sha(csrf), principal, login,
                epoch_hash, session_id,
                hashlib.sha256(self._origin.encode("ascii")).hexdigest(),
                now, now + _TTL,
            ),
        )
        return BrowserSessionSecrets(token=token, csrf_token=csrf)

    def issue_after_independently_authenticated_login(self):
        """ISSUER MUST be called only from externally verified login lifecycle."""
        if not callable(self._login_event) or not callable(self._sessions):
            raise BrowserLoginSessionError()

        def issue(conn):
            # Provider is intentionally sampled ONLY after acquiring DB lock.
            ctx = self._login_event()
            if (
                type(ctx) is not TrustedTicketContext
                or not _valid(ctx.principal_id, _ID)
                or not _valid(ctx.login_id, _LOGIN)
                or not _valid(ctx.auth_epoch, _TOKEN)
            ):
                raise BrowserLoginSessionError()
            self._canonical(ctx.session_id)
            epoch_hash = _sha(ctx.auth_epoch)
            self._epoch_current(conn, ctx.principal_id, ctx.login_id, epoch_hash)
            return self._insert(
                conn, principal=ctx.principal_id, login=ctx.login_id,
                epoch_hash=epoch_hash, session_id=ctx.session_id,
                now=self._now(),
            )
        return self._transaction(issue, create_table=True)

    @staticmethod
    def _cookie_token(request):
        if len(request.headers.getlist("cookie")) != 1:
            raise BrowserLoginSessionError()
        pairs = [
            chunk.strip().split("=", 1)
            for chunk in request.headers["cookie"].split(";")
        ]
        found = [
            pair[1] for pair in pairs
            if len(pair) == 2 and pair[0] == _COOKIE
        ]
        if len(found) != 1 or not _valid(found[0], _TOKEN):
            raise BrowserLoginSessionError()
        return found[0]

    def _incoming(self, request):
        if not isinstance(request, Request):
            raise BrowserLoginSessionError()
        headers = request.headers
        if (
            request.method != "POST"
            or request.url.path != _PATH
            or bool(request.query_params)
            or len(headers.getlist("origin")) != 1
            or len(headers.getlist("sec-fetch-site")) != 1
            or headers["sec-fetch-site"] != "same-origin"
            or len(headers.getlist("x-memory-csrf")) != 1
            or len(headers.getlist("cookie")) != 1
            or headers.get("content-type") != "application/json"
            or any(k in headers for k in (
                "x-principal-id", "x-user-id", "x-login-id",
                "x-session-id", "x-auth-epoch", "x-memory-identity",
            ))
            or not _valid_origin(headers["origin"])
            or not hmac.compare_digest(headers["origin"], self._origin)
            or not _valid(headers["x-memory-csrf"], _TOKEN)
        ):
            raise BrowserLoginSessionError()
        return self._cookie_token(request), headers["x-memory-csrf"]

    def _lookup(self, conn, token, csrf, memory_request=None):
        row = conn.execute(
            """SELECT csrf_sha256,principal_id,login_id,epoch_sha256,
                      session_id,origin_sha256,issued_at,expires_at,revoked
               FROM memory_browser_login_session_v1
               WHERE token_sha256=?""",
            (_sha(token),),
        ).fetchone()
        now = self._now()
        if (
            row is None or row[8] != 0
            or not hmac.compare_digest(row[0], _sha(csrf))
            or not hmac.compare_digest(
                row[5], hashlib.sha256(self._origin.encode("ascii")).hexdigest(),
            )
            or type(row[6]) not in (int, float)
            or type(row[7]) not in (int, float)
            or not math.isfinite(row[6]) or not math.isfinite(row[7])
            or now < row[6] or now >= row[7]
            or row[7] - row[6] != _TTL
        ):
            raise BrowserLoginSessionError()
        self._epoch_current(conn, row[1], row[2], row[3])
        self._canonical(row[4], request=memory_request)
        return row

    def verify_request_for_review(
        self, request: Request,
        memory_request: RememberExplicitMemoryRequest,
    ):
        if type(memory_request) is not RememberExplicitMemoryRequest:
            raise BrowserLoginSessionError()
        token, csrf = self._incoming(request)
        return self._transaction(lambda conn: (
            self._lookup(conn, token, csrf, memory_request),
            BrowserSessionReview(),
        )[1])

    def rotate(self, request: Request):
        """Atomic credential replacement. Old token cannot pass another worker."""
        token, csrf = self._incoming(request)

        def change(conn):
            row = self._lookup(conn, token, csrf)
            conn.execute(
                """UPDATE memory_browser_login_session_v1 SET revoked=1
                   WHERE token_sha256=? AND revoked=0""",
                (_sha(token),),
            )
            return self._insert(
                conn, principal=row[1], login=row[2],
                epoch_hash=row[3], session_id=row[4], now=self._now(),
            )
        return self._transaction(change)

    def logout(self, request: Request):
        """Revoke this browser credential; separate auth epoch logout required."""
        token, csrf = self._incoming(request)

        def change(conn):
            self._lookup(conn, token, csrf)
            changed = conn.execute(
                """UPDATE memory_browser_login_session_v1 SET revoked=1
                   WHERE token_sha256=? AND revoked=0""",
                (_sha(token),),
            ).rowcount
            if changed != 1:
                raise BrowserLoginSessionError()
        self._transaction(change)
