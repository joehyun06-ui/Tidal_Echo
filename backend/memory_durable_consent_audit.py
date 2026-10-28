"""Durable OBSERVED interaction audit experiment; not user consent authority.

Offline + default-OFF. There is NO HTTP route, human-click evidence, login
provider, memory write, or writer capability. The context and interaction
callbacks are trusted ONLY by the test harness. Do not use untrusted request
JSON, a boolean, or a dataclass as proof of real authorization.
"""
from __future__ import annotations

import hashlib
import hmac
import json
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

from .memory_browser_consent_review import TrustedBrowserReviewContext, _valid_origin
from .memory_explicit_actions import RememberExplicitMemoryRequest
from .memory_session_confirmation import CONFIRMATION_TEXT, _digest
from .memory_session_write_guard import verify_server_session_write
from .memory_shared_confirmation_ticket import TrustedTicketContext, _request_valid

_FLAG = "MEMORY_DURABLE_CONSENT_AUDIT_REVIEW_ENABLED"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z", re.ASCII)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_LOGIN = re.compile(r"[A-Za-z0-9_-]{24,96}\Z", re.ASCII)
_DIGEST = re.compile(r"[a-f0-9]{64}\Z", re.ASCII)
_TTL = 120
_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_consent_offer_audit_v1 (
    nonce_sha256 TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    login_id TEXT NOT NULL,
    epoch_sha256 TEXT NOT NULL,
    session_id TEXT NOT NULL,
    request_id_sha256 TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    origin_sha256 TEXT NOT NULL,
    csrf_sha256 TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    offer_hmac_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_consent_event_audit_v1 (
    nonce_sha256 TEXT PRIMARY KEY
        REFERENCES memory_consent_offer_audit_v1(nonce_sha256),
    observed_action TEXT NOT NULL CHECK(observed_action IN ('approve','decline')),
    event_ref_sha256 TEXT NOT NULL UNIQUE,
    observed_at REAL NOT NULL,
    event_hmac_sha256 TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS memory_consent_offer_no_update_v1
BEFORE UPDATE ON memory_consent_offer_audit_v1 BEGIN
    SELECT RAISE(ABORT, 'memory_consent_audit_immutable');
END;
CREATE TRIGGER IF NOT EXISTS memory_consent_offer_no_delete_v1
BEFORE DELETE ON memory_consent_offer_audit_v1 BEGIN
    SELECT RAISE(ABORT, 'memory_consent_audit_immutable');
END;
CREATE TRIGGER IF NOT EXISTS memory_consent_event_no_update_v1
BEFORE UPDATE ON memory_consent_event_audit_v1 BEGIN
    SELECT RAISE(ABORT, 'memory_consent_audit_immutable');
END;
CREATE TRIGGER IF NOT EXISTS memory_consent_event_no_delete_v1
BEFORE DELETE ON memory_consent_event_audit_v1 BEGIN
    SELECT RAISE(ABORT, 'memory_consent_audit_immutable');
END;
"""


class DurableConsentAuditError(RuntimeError):
    def __init__(self):
        super().__init__("memory_durable_consent_audit_unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class TrustedObservedInteraction:
    """Server-observed event abstraction; a dataclass cannot prove a click."""
    nonce: str = field(repr=False)
    action: str = field(repr=False)
    event_ref: str = field(repr=False)

    def __repr__(self):
        return "<TrustedObservedInteraction>"


@dataclass(frozen=True, slots=True, repr=False)
class DurableAuditPreview:
    nonce: str = field(repr=False)
    content_sha256: str = field(repr=False)
    preview_content: str = field(repr=False)
    expires_in_seconds: int = _TTL
    confirmation_text: str = CONFIRMATION_TEXT

    def __repr__(self):
        return "<DurableAuditPreview>"


@dataclass(frozen=True, slots=True, repr=False)
class ObservedAuditRecord:
    """Historical verification only, not permission to write memory."""
    request_id: str = field(repr=False)
    action: str = field(repr=False)

    def __repr__(self):
        return "<ObservedAuditRecord>"


def _enabled() -> bool:
    return (
        os.environ.get(_FLAG) == "true"
        and os.environ.get("MEMORY_TRUSTED_AUTH_EPOCH_ENABLED") == "true"
        and os.environ.get("MEMORY_BROWSER_CONSENT_REVIEW_ENABLED") == "true"
    )


def _valid(value: object, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DurableObservedConsentAudit:
    """Append-only SQLite offline audit, independent of every writer.

    The independent interaction observer must EVENTUALLY be supplied by
    authenticated middleware and audited UI. Here it is synthetic only.
    The HMAC key is supplied at runtime and must never be stored in SQLite.
    """

    __slots__ = ("_path", "_context", "_observer", "_key", "_clock")

    def __init__(
        self, *, path: str | Path,
        context_provider: Callable[[], TrustedBrowserReviewContext],
        interaction_provider: Callable[[], TrustedObservedInteraction],
        hmac_secret: bytes,
        clock: Callable[[], float] = time.time,
    ):
        if (
            not isinstance(path, (str, Path))
            or not Path(path).is_absolute()
            or not callable(context_provider)
            or not callable(interaction_provider)
            or not callable(clock)
            or type(hmac_secret) is not bytes or len(hmac_secret) < 32
        ):
            raise DurableConsentAuditError()
        self._path = Path(path).resolve()
        self._context = context_provider
        self._observer = interaction_provider
        self._key = hmac_secret
        self._clock = clock

    def _now(self) -> float:
        try:
            now = self._clock()
            if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
                raise DurableConsentAuditError()
            return float(now)
        except Exception:
            raise DurableConsentAuditError() from None

    def _sign(self, category: str, values: tuple) -> str:
        encoded = json.dumps(
            [category, *values], separators=(",", ":"), ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        return hmac.new(self._key, encoded, hashlib.sha256).hexdigest()

    def _write(self, callback):
        if not _enabled():
            raise DurableConsentAuditError()
        try:
            with closing(sqlite3.connect(
                str(self._path), timeout=5.0, isolation_level=None,
            )) as conn:
                conn.execute("PRAGMA foreign_keys=ON")
                # DDL before BEGIN IMMEDIATE: SQLite executescript commits any
                # existing transaction. This connection has no active txn yet.
                conn.executescript(_SCHEMA)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    result = callback(conn)
                    if not _enabled():
                        raise DurableConsentAuditError()
                    conn.execute("COMMIT")
                    return result
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
        except Exception:
            raise DurableConsentAuditError() from None

    def _trusted(
        self, conn: sqlite3.Connection,
        request: RememberExplicitMemoryRequest, *, origin: str, csrf_token: str,
    ) -> TrustedTicketContext:
        if not _enabled() or not _request_valid(request):
            raise DurableConsentAuditError()
        try:
            ctx = self._context()
            if type(ctx) is not TrustedBrowserReviewContext:
                raise DurableConsentAuditError()
            identity = ctx.identity
            if (
                type(identity) is not TrustedTicketContext
                or not _valid(identity.principal_id, _ID)
                or not _valid(identity.login_id, _LOGIN)
                or not _valid(identity.auth_epoch, _TOKEN)
                or type(identity.canonical_sessions) is not tuple
                or not _valid_origin(ctx.origin)
                or type(origin) is not str
                or not hmac.compare_digest(origin, ctx.origin)
                or not _valid(ctx.csrf_token, _TOKEN)
                or not _valid(csrf_token, _TOKEN)
                or not hmac.compare_digest(csrf_token, ctx.csrf_token)
            ):
                raise DurableConsentAuditError()
            verify_server_session_write(
                request, trusted_session_id=identity.session_id,
                canonical_sessions=identity.canonical_sessions,
            )
            row = conn.execute(
                """SELECT epoch_sha256,revoked FROM memory_auth_epoch_v1
                   WHERE principal_id=? AND login_id=?""",
                (identity.principal_id, identity.login_id),
            ).fetchone()
            if (
                row is None or row[1] != 0 or type(row[0]) is not str
                or not hmac.compare_digest(row[0], _sha(identity.auth_epoch))
            ):
                raise DurableConsentAuditError()
            return identity
        except Exception:
            raise DurableConsentAuditError() from None

    def _validated_offer(self, row: tuple) -> None:
        if (
            len(row) != 12 or type(row[11]) is not str
            or not hmac.compare_digest(
                row[11], self._sign("offer-v1", tuple(row[:11])),
            )
            or type(row[9]) not in (int, float)
            or type(row[10]) not in (int, float)
            or not math.isfinite(row[9]) or not math.isfinite(row[10])
            or row[9] < 0 or row[10] <= row[9]
        ):
            raise DurableConsentAuditError()

    def prepare(
        self, request: RememberExplicitMemoryRequest,
        *, origin: str, csrf_token: str,
    ) -> DurableAuditPreview:
        def operation(conn):
            identity = self._trusted(
                conn, request, origin=origin, csrf_token=csrf_token,
            )
            now = self._now()
            nonce = secrets.token_urlsafe(32)
            digest = _digest(request)
            values = (
                _sha(nonce), identity.principal_id, identity.login_id,
                _sha(identity.auth_epoch), identity.session_id,
                _sha(request.request_id), digest, _sha(origin),
                _sha(csrf_token), now, now + _TTL,
            )
            conn.execute(
                """INSERT INTO memory_consent_offer_audit_v1
                   (nonce_sha256,principal_id,login_id,epoch_sha256,
                    session_id,request_id_sha256,content_sha256,
                    origin_sha256,csrf_sha256,created_at,expires_at,
                    offer_hmac_sha256)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*values, self._sign("offer-v1", values)),
            )
            return DurableAuditPreview(
                nonce=nonce, content_sha256=digest,
                preview_content=request.content,
            )
        return self._write(operation)

    def observe(
        self, request: RememberExplicitMemoryRequest, *,
        nonce: str, content_sha256: str, confirmation_text: str,
        origin: str, csrf_token: str,
    ) -> ObservedAuditRecord:
        if not _valid(nonce, _TOKEN):
            raise DurableConsentAuditError()
        def operation(conn):
            identity = self._trusted(
                conn, request, origin=origin, csrf_token=csrf_token,
            )
            now = self._now()
            event = self._observer()
            if (
                type(event) is not TrustedObservedInteraction
                or not _valid(event.nonce, _TOKEN)
                or not hmac.compare_digest(event.nonce, nonce)
                or event.action not in ("approve", "decline")
                or not _valid(event.event_ref, _TOKEN)
                or not _valid(content_sha256, _DIGEST)
                or not hmac.compare_digest(content_sha256, _digest(request))
                or type(confirmation_text) is not str
                or not hmac.compare_digest(confirmation_text, CONFIRMATION_TEXT)
                or not _enabled()
            ):
                raise DurableConsentAuditError()
            row = conn.execute(
                "SELECT * FROM memory_consent_offer_audit_v1 WHERE nonce_sha256=?",
                (_sha(nonce),),
            ).fetchone()
            if row is None:
                raise DurableConsentAuditError()
            self._validated_offer(row)
            if (
                row[1] != identity.principal_id
                or row[2] != identity.login_id
                or not hmac.compare_digest(row[3], _sha(identity.auth_epoch))
                or row[4] != identity.session_id
                or not hmac.compare_digest(row[5], _sha(request.request_id))
                or not hmac.compare_digest(row[6], content_sha256)
                or not hmac.compare_digest(row[7], _sha(origin))
                or not hmac.compare_digest(row[8], _sha(csrf_token))
                or now >= row[10] or now < row[9]
            ):
                raise DurableConsentAuditError()
            event_values = (_sha(nonce), event.action, _sha(event.event_ref), now)
            conn.execute(
                """INSERT INTO memory_consent_event_audit_v1
                   (nonce_sha256,observed_action,event_ref_sha256,
                    observed_at,event_hmac_sha256) VALUES(?,?,?,?,?)""",
                (*event_values, self._sign("event-v1", tuple(row[:11]) + event_values)),
            )
            return ObservedAuditRecord(
                request_id=request.request_id, action=event.action,
            )
        return self._write(operation)

    def verify_historical(
        self, request: RememberExplicitMemoryRequest, *, nonce: str,
    ) -> ObservedAuditRecord:
        """Verify signed historical rows without trusting CURRENT login state.

        Never authorizes writing or discloses account state; this is an
        internal forensic test path only, using an explicit signed request.
        """
        if not _enabled() or not _valid(nonce, _TOKEN) or not _request_valid(request):
            raise DurableConsentAuditError()
        try:
            with closing(sqlite3.connect(
                self._path.as_uri() + "?mode=ro", uri=True, timeout=5.0,
            )) as conn:
                row = conn.execute(
                    "SELECT * FROM memory_consent_offer_audit_v1 WHERE nonce_sha256=?",
                    (_sha(nonce),),
                ).fetchone()
                event = conn.execute(
                    "SELECT * FROM memory_consent_event_audit_v1 WHERE nonce_sha256=?",
                    (_sha(nonce),),
                ).fetchone()
                if row is None or event is None:
                    raise DurableConsentAuditError()
                self._validated_offer(row)
                if (
                    not hmac.compare_digest(row[5], _sha(request.request_id))
                    or not hmac.compare_digest(row[6], _digest(request))
                    or event[0] != row[0]
                    or event[1] not in ("approve", "decline")
                    or type(event[3]) not in (int, float)
                    or not math.isfinite(event[3])
                    or not row[9] <= event[3] < row[10]
                    or type(event[4]) is not str
                    or not hmac.compare_digest(
                        event[4], self._sign(
                            "event-v1", tuple(row[:11]) + tuple(event[:4]),
                        ),
                    )
                ):
                    raise DurableConsentAuditError()
                return ObservedAuditRecord(
                    request_id=request.request_id, action=event[1],
                )
        except Exception:
            raise DurableConsentAuditError() from None
