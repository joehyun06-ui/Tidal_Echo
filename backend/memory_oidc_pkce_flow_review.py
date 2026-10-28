"""Default-OFF OIDC code+PKCE transaction rehearsal; NO network/login routes.

The independent request-scoped browser binding callback is provided by a
synthetic test only. A real server must implement secure pre-login browser
cookie, callback origin/state handling, authorization-code exchange over TLS,
provider discovery review, and verified login lifecycle. This module cannot
issue a browser session, consent, or memory write capability.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .memory_oidc_id_token_review import (
    OidcTokenReviewError, PinnedOidcIdTokenVerifier, VerifiedOidcSubject,
    _valid_issuer,
)

_FLAG = "MEMORY_OIDC_CODE_PKCE_REVIEW_ENABLED"
_TTL = 180
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z", re.ASCII)
_CODE = re.compile(r"[A-Za-z0-9._~-]{8,512}\Z", re.ASCII)
_CLIENT = re.compile(r"[A-Za-z0-9._:-]{3,128}\Z", re.ASCII)
_DDL = """
CREATE TABLE IF NOT EXISTS memory_oidc_pkce_attempt_review_v1 (
  state_sha256 TEXT PRIMARY KEY,
  binding_sha256 TEXT NOT NULL,
  nonce_sha256 TEXT NOT NULL,
  sealed_secret BLOB,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  consumed_at REAL,
  CHECK (consumed_at IS NULL OR sealed_secret IS NULL)
)
"""


class OidcPkceReviewError(RuntimeError):
    def __init__(self):
        super().__init__("memory_oidc_pkce_review_unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class OidcAuthorizationReviewPlan:
    """Authorize URL only, not a verifier, authenticated login or credential."""
    authorize_url: str = field(repr=False)
    expires_in_seconds: int = _TTL
    may_issue_browser_session: bool = False
    may_write_memory: bool = False

    def __repr__(self):
        return "<OidcAuthorizationReviewPlan>"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _endpoint(value: str, *, allow_path=True) -> bool:
    if type(value) is not str or not 12 <= len(value) <= 256:
        return False
    try:
        x = urlsplit(value)
        if (
            x.scheme != "https" or not x.hostname or x.hostname.lower() != x.hostname
            or x.hostname in {"localhost", "127.0.0.1"} or x.username is not None
            or x.password is not None or x.port not in (None, 443)
            or x.query or x.fragment or not x.path.startswith("/")
            or (not allow_path and x.path not in ("", "/"))
        ):
            return False
        return value == f"https://{x.netloc}{x.path}"
    except (TypeError, ValueError):
        return False


class OidcCodePkceOfflineReview:
    """Persist challenge before redirect; atomically consume before exchange.

    The encrypted nonce/verifier are stored only in AES-256-GCM ciphertext.
    AES key is supplied from runtime-only review secrets and never persisted.
    Callback browser binding provider must NOT read a client-asserted identity.
    The token exchanger is injected offline and MUST return a signed ID token
    which is then verified internally with pinned RSA keys. No network client.
    """

    __slots__ = (
        "_path", "_issuer", "_client", "_auth_url", "_token_url", "_redirect",
        "_binding", "_exchange", "_pubkeys", "_key", "_clock",
    )

    def __init__(
        self, *, path: str | Path, issuer: str, client_id: str,
        authorization_endpoint: str, token_endpoint: str, redirect_uri: str,
        browser_binding_provider: Callable[[], str] | None,
        token_exchanger: Callable[[str, str, str, str], str] | None,
        pinned_public_keys: Mapping[str, bytes],
        at_rest_key: bytes,
        clock: Callable[[], float] = time.time,
    ):
        if (
            not isinstance(path, (str, Path)) or not Path(path).is_absolute()
            or not _valid_issuer(issuer)
            or not _endpoint(authorization_endpoint)
            or not _endpoint(token_endpoint)
            or not _endpoint(redirect_uri)
            or type(client_id) is not str or not _CLIENT.fullmatch(client_id)
            or type(at_rest_key) is not bytes or len(at_rest_key) != 32
            or not callable(clock)
        ):
            raise OidcPkceReviewError()
        issuer_host = urlsplit(issuer).netloc
        if (
            urlsplit(authorization_endpoint).netloc != issuer_host
            or urlsplit(token_endpoint).netloc != issuer_host
        ):
            raise OidcPkceReviewError()
        self._path = Path(path).resolve()
        self._issuer = issuer
        self._client = client_id
        self._auth_url = authorization_endpoint
        self._token_url = token_endpoint
        self._redirect = redirect_uri
        self._binding = browser_binding_provider
        self._exchange = token_exchanger
        self._pubkeys = dict(pinned_public_keys)
        self._key = at_rest_key
        self._clock = clock
        # Validate pinned key configuration NOW, before storing a challenge.
        try:
            PinnedOidcIdTokenVerifier(
                expected_issuer=issuer, client_id=client_id,
                pinned_public_keys=self._pubkeys,
                expected_nonce_provider=lambda: "N" * 43,
                clock=clock,
            )
        except Exception:
            raise OidcPkceReviewError() from None

    def _enabled(self):
        return (
            os.environ.get(_FLAG) == "true"
            and os.environ.get(
                "MEMORY_OIDC_ID_TOKEN_VERIFIER_REVIEW_ENABLED"
            ) == "true"
        )

    def _now(self):
        now = self._clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise OidcPkceReviewError()
        return float(now)

    def _browser_binding(self):
        if not callable(self._binding):
            raise OidcPkceReviewError()
        binding = self._binding()
        if type(binding) is not str or not _TOKEN.fullmatch(binding):
            raise OidcPkceReviewError()
        return binding

    def _aad(self, state_sha, binding_sha):
        return json.dumps(
            ["oidc-pkce-v1", state_sha, binding_sha, self._issuer,
             self._client, self._redirect],
            separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii")

    def _transaction(self, operation, *, create=False):
        if not self._enabled():
            raise OidcPkceReviewError()
        try:
            with closing(sqlite3.connect(
                str(self._path), timeout=5, isolation_level=None,
            )) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if create:
                        conn.execute(_DDL)
                    result = operation(conn)
                    if not self._enabled():
                        raise OidcPkceReviewError()
                    conn.execute("COMMIT")
                    return result
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
        except Exception:
            raise OidcPkceReviewError() from None

    def prepare(self) -> OidcAuthorizationReviewPlan:
        """Create state, nonce and RFC 7636 S256 PKCE pair; pin in SQLite."""
        def operation(conn):
            binding = self._browser_binding()
            state = secrets.token_urlsafe(32)
            nonce = secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(48)
            state_sha, binding_sha = _sha(state), _sha(binding)
            now = self._now()
            plaintext = json.dumps(
                [nonce, verifier], separators=(",", ":"),
            ).encode("ascii")
            seal_nonce = secrets.token_bytes(12)
            sealed = seal_nonce + AESGCM(self._key).encrypt(
                seal_nonce, plaintext, self._aad(state_sha, binding_sha),
            )
            conn.execute(
                """INSERT INTO memory_oidc_pkce_attempt_review_v1
                   (state_sha256,binding_sha256,nonce_sha256,sealed_secret,
                    created_at,expires_at,consumed_at)
                   VALUES (?,?,?,?,?,?,NULL)""",
                (state_sha, binding_sha, _sha(nonce), sealed, now, now + _TTL),
            )
            challenge = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
            qs = urlencode({
                "response_type": "code", "client_id": self._client,
                "redirect_uri": self._redirect, "scope": "openid",
                "state": state, "nonce": nonce, "code_challenge": challenge,
                "code_challenge_method": "S256",
            })
            return OidcAuthorizationReviewPlan(
                authorize_url=self._auth_url + "?" + qs,
            )
        return self._transaction(operation, create=True)

    def complete(
        self, *, state: str, code: str,
    ) -> VerifiedOidcSubject:
        """Single-use claim COMMITTED before any provider token exchange.

        Every failed code exchange or signature check burns this state; caller
        must start a new login. This is intentionally NOT a web callback.
        """
        if (
            not self._enabled() or not callable(self._exchange)
            or type(state) is not str or not _TOKEN.fullmatch(state)
            or type(code) is not str or not _CODE.fullmatch(code)
        ):
            raise OidcPkceReviewError()

        def claim(conn):
            binding = self._browser_binding()
            state_sha, binding_sha = _sha(state), _sha(binding)
            row = conn.execute(
                """SELECT binding_sha256,nonce_sha256,sealed_secret,
                          created_at,expires_at,consumed_at
                   FROM memory_oidc_pkce_attempt_review_v1
                   WHERE state_sha256=?""",
                (state_sha,),
            ).fetchone()
            now = self._now()
            if (
                row is None or row[5] is not None
                or not hmac.compare_digest(row[0], binding_sha)
                or type(row[2]) is not bytes or len(row[2]) < 28
                or type(row[3]) not in (int, float)
                or type(row[4]) not in (int, float)
                or not math.isfinite(row[3]) or not math.isfinite(row[4])
                or row[3] < 0 or row[4] - row[3] != _TTL
                or now < row[3] or now >= row[4]
            ):
                raise OidcPkceReviewError()
            sealed = row[2]
            plaintext = AESGCM(self._key).decrypt(
                sealed[:12], sealed[12:], self._aad(state_sha, binding_sha),
            )
            data = json.loads(plaintext.decode("ascii"))
            if (
                type(data) is not list or len(data) != 2
                or any(type(x) is not str or not _TOKEN.fullmatch(x) for x in data)
                or not hmac.compare_digest(row[1], _sha(data[0]))
            ):
                raise OidcPkceReviewError()
            # This is the replay barrier. Do not send code to the remote IdP
            # until this change is committed; disable/failed signature leaves
            # it permanently consumed.
            changed = conn.execute(
                """UPDATE memory_oidc_pkce_attempt_review_v1
                   SET consumed_at=?,sealed_secret=NULL
                   WHERE state_sha256=? AND consumed_at IS NULL""",
                (now, state_sha),
            ).rowcount
            if changed != 1:
                raise OidcPkceReviewError()
            return data[0], data[1]

        try:
            nonce, verifier = self._transaction(claim)
            if not self._enabled():
                raise OidcPkceReviewError()
            id_token = self._exchange(
                code, verifier, self._redirect, self._token_url,
            )
            if not self._enabled():
                raise OidcPkceReviewError()
            verifier_obj = PinnedOidcIdTokenVerifier(
                expected_issuer=self._issuer, client_id=self._client,
                pinned_public_keys=self._pubkeys,
                expected_nonce_provider=lambda: nonce, clock=self._clock,
            )
            result = verifier_obj.verify(id_token)
            if type(result) is not VerifiedOidcSubject or not self._enabled():
                raise OidcPkceReviewError()
            return result
        except Exception:
            raise OidcPkceReviewError() from None
