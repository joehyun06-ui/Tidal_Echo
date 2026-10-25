"""Default-OFF server-only login epoch registry for the memory confirmation review.

This is NOT user authentication, an HTTP endpoint, a credential issuer, or
an authority to save a memory. Only a future trusted authentication adapter
may supply AuthenticatedLogin and retain ServerIssuedEpoch on the server.
The DB stores SHA-256 of epochs, never the returned epoch value.

Epoch verification and the existing SQLite memory commit are currently
separate transactions: production revocation/commit race safety is NOT met.
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

_FLAG = "MEMORY_TRUSTED_AUTH_EPOCH_ENABLED"
_PRINCIPAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_LOGIN = re.compile(r"[A-Za-z0-9_-]{24,96}\Z", re.ASCII)
_EPOCH = re.compile(r"[A-Za-z0-9_-]{24,96}\Z", re.ASCII)
_DDL = """
CREATE TABLE IF NOT EXISTS memory_auth_epoch_v1 (
    principal_id TEXT NOT NULL,
    login_id TEXT NOT NULL,
    epoch_sha256 TEXT,
    generation INTEGER NOT NULL CHECK(generation > 0),
    revoked INTEGER NOT NULL CHECK(revoked IN (0, 1)),
    updated_at REAL NOT NULL,
    PRIMARY KEY (principal_id, login_id)
)
"""


class AuthEpochError(RuntimeError):
    def __init__(self):
        super().__init__("memory_auth_epoch_unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class AuthenticatedLogin:
    """Authenticated server-side login identity. A dataclass is NOT proof."""
    principal_id: str = field(repr=False)
    login_id: str = field(repr=False)

    def __repr__(self):
        return "<AuthenticatedLogin>"


@dataclass(frozen=True, slots=True, repr=False)
class ServerIssuedEpoch:
    """Opaque login generation; NOT a bearer login credential."""
    value: str = field(repr=False)

    def __repr__(self):
        return "<ServerIssuedEpoch>"


def _enabled() -> bool:
    return os.environ.get(_FLAG) == "true"


def _valid_string(value: object, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


class AuthEpochRegistry:
    """SQLite-backed lifecycle for *already-authenticated* server logins.

    The login provider MUST read a server-validated login; never take a
    principal, login_id or epoch from untrusted JSON, headers or URL params.
    A separate trusted login/session service owns authentication, logout and
    retention of the issued epoch. No route or memory write is added here.
    """

    __slots__ = ("_path", "_identity_provider", "_clock")

    def __init__(
        self,
        *,
        path: str | Path,
        identity_provider: Callable[[], AuthenticatedLogin],
        clock: Callable[[], float] = time.time,
    ):
        if (
            not isinstance(path, (str, Path))
            or not str(path)
            or not Path(path).is_absolute()
            or not callable(identity_provider)
            or not callable(clock)
        ):
            raise AuthEpochError()
        self._path = Path(path)
        self._identity_provider = identity_provider
        self._clock = clock

    def _identity(self) -> AuthenticatedLogin:
        if not _enabled():
            raise AuthEpochError()
        try:
            identity = self._identity_provider()
            if (
                type(identity) is not AuthenticatedLogin
                or not _valid_string(identity.principal_id, _PRINCIPAL)
                or not _valid_string(identity.login_id, _LOGIN)
            ):
                raise AuthEpochError()
            return identity
        except Exception:
            raise AuthEpochError() from None

    def _now(self) -> float:
        try:
            value = self._clock()
            if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
                raise AuthEpochError()
            return float(value)
        except Exception:
            raise AuthEpochError() from None

    def _change(self, statement: str, args: tuple) -> int:
        try:
            conn = sqlite3.connect(str(self._path), timeout=3, isolation_level=None)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(_DDL)
                cursor = conn.execute(statement, args)
                conn.execute("COMMIT")
                return cursor.rowcount
            finally:
                conn.close()
        except sqlite3.Error:
            raise AuthEpochError() from None

    def issue_after_authenticated_login(self) -> ServerIssuedEpoch:
        """New login or re-login ALWAYS rotates; requires trusted login event."""
        identity = self._identity()
        now = self._now()
        epoch = secrets.token_urlsafe(32)
        digest = hashlib.sha256(epoch.encode("ascii")).hexdigest()
        self._change(
            """INSERT INTO memory_auth_epoch_v1
                 (principal_id, login_id, epoch_sha256, generation, revoked, updated_at)
               VALUES (?, ?, ?, 1, 0, ?)
               ON CONFLICT(principal_id, login_id) DO UPDATE SET
                 epoch_sha256=excluded.epoch_sha256,
                 generation=memory_auth_epoch_v1.generation+1,
                 revoked=0, updated_at=excluded.updated_at""",
            (identity.principal_id, identity.login_id, digest, now),
        )
        return ServerIssuedEpoch(epoch)

    def assert_current(self, epoch: ServerIssuedEpoch) -> None:
        """Fail closed if rotated/revoked/missing, including in another worker.

        The epoch MUST come from the authenticated server session, not a
        client-supplied assertion. This preflight is not a commit-time lock.
        """
        identity = self._identity()
        if type(epoch) is not ServerIssuedEpoch or not _valid_string(epoch.value, _EPOCH):
            raise AuthEpochError()
        try:
            conn = sqlite3.connect(self._path.as_uri() + "?mode=ro", uri=True, timeout=3)
            try:
                row = conn.execute(
                    """SELECT epoch_sha256, revoked FROM memory_auth_epoch_v1
                       WHERE principal_id=? AND login_id=?""",
                    (identity.principal_id, identity.login_id),
                ).fetchone()
            finally:
                conn.close()
            digest = hashlib.sha256(epoch.value.encode("ascii")).hexdigest()
            if (
                row is None or row[1] != 0 or type(row[0]) is not str
                or not hmac.compare_digest(digest, row[0])
            ):
                raise AuthEpochError()
        except (sqlite3.Error, ValueError, TypeError):
            raise AuthEpochError() from None

    def revoke_before_logout(self) -> bool:
        """Invalidate this login; caller MUST invoke before clearing identity."""
        identity = self._identity()
        changed = self._change(
            """UPDATE memory_auth_epoch_v1
               SET epoch_sha256=NULL, revoked=1, generation=generation+1,
                   updated_at=?
               WHERE principal_id=? AND login_id=? AND revoked=0""",
            (self._now(), identity.principal_id, identity.login_id),
        )
        return changed == 1

    def revoke_all_for_authenticated_principal(self) -> int:
        """Security logout-all for the trusted, authenticated principal."""
        identity = self._identity()
        return self._change(
            """UPDATE memory_auth_epoch_v1
               SET epoch_sha256=NULL, revoked=1, generation=generation+1,
                   updated_at=?
               WHERE principal_id=? AND revoked=0""",
            (self._now(), identity.principal_id),
        )

    def make_confirmation_provider(
        self,
        *,
        epoch_provider: Callable[[], ServerIssuedEpoch],
        session_provider: Callable[[], tuple[str, tuple[dict, ...]]],
    ) -> Callable[[], object]:
        """Server-only adapter to the existing AuthorizedSessionContext type.

        Both providers must be derived from the SAME authenticated request,
        not a user-controlled body/header/query. No actual login is wired.
        """
        if not callable(epoch_provider) or not callable(session_provider):
            raise AuthEpochError()

        def provide():
            try:
                identity = self._identity()
                epoch = epoch_provider()
                self.assert_current(epoch)
                # Check identity has not changed during the lookup.
                checked = self._identity()
                if identity != checked:
                    raise AuthEpochError()
                snapshot = session_provider()
                if (
                    type(snapshot) is not tuple or len(snapshot) != 2
                    or type(snapshot[0]) is not str
                    or type(snapshot[1]) is not tuple
                ):
                    raise AuthEpochError()
                from .memory_session_confirmation import AuthorizedSessionContext
                return AuthorizedSessionContext(
                    principal_id=checked.principal_id,
                    auth_epoch=epoch.value,
                    session_id=snapshot[0],
                    canonical_sessions=snapshot[1],
                )
            except Exception:
                raise AuthEpochError() from None

        return provide
