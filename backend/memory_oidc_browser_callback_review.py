"""Strict offline browser start/callback binding for OIDC. No ASGI route.

Start is same-origin POST. Callback is top-level cross-site GET (therefore
Origin/Fetch-Site cannot be used as identity). Both use a short-lived,
server-HMAC-signed __Host- cookie; #149 independently binds its one-use state
to the cookie nonce in SQLite and #148 checks pinned RSA/nonce. This remains
synthetic: no real provider, login issuance, HTTP route or memory writes.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from starlette.requests import Request

from .memory_oidc_pkce_flow_review import OidcCodePkceOfflineReview, _endpoint
from .memory_oidc_id_token_review import VerifiedOidcSubject, _valid_issuer

_FLAG = "MEMORY_OIDC_BROWSER_CALLBACK_REVIEW_ENABLED"
_COOKIE = "__Host-gt-oidc-review"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)
_TIME = re.compile(r"[0-9]{1,12}\Z", re.ASCII)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]", re.ASCII)
_TTL = 180
_MAX_QUERY_BYTES = 2048
_BLOCKED_HEADERS = (
    "forwarded", "x-forwarded-host", "x-forwarded-proto", "x-original-url",
    "x-rewrite-url", "x-principal-id", "x-user-id", "x-login-id",
    "x-auth-epoch", "x-memory-identity",
)


class OidcBrowserCallbackReviewError(RuntimeError):
    def __init__(self):
        super().__init__("memory_oidc_browser_callback_unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class OidcBrowserStartReview:
    """Synthetic browser instructions; not an issued HTTP response."""
    authorization_url: str = field(repr=False)
    set_cookie: str = field(repr=False)
    may_issue_browser_session: bool = False
    may_write_memory: bool = False

    def __repr__(self):
        return "<OidcBrowserStartReview review_only>"


@dataclass(frozen=True, slots=True, repr=False)
class OidcBrowserCallbackReviewResult:
    """Verified signature claims only; cannot establish a local login."""
    verified_subject: VerifiedOidcSubject = field(repr=False)
    may_issue_browser_session: bool = False
    may_write_memory: bool = False

    def __repr__(self):
        return "<OidcBrowserCallbackReviewResult review_only>"


class OidcBrowserCallbackOfflineReview:
    """No registered HTTP route. Uses request-scoped state, never globals.

    The HMAC key and AES state key must be server-only runtime configuration.
    token_exchanger is SYNTHETIC; no real OAuth code is sent in this review.
    """

    __slots__ = (
        "_db", "_issuer", "_client", "_auth_url", "_token_url", "_redirect",
        "_host", "_path", "_cookie_key", "_aes_key", "_keys", "_exchange",
        "_clock",
    )

    def __init__(
        self, *, path: str | Path, issuer: str, client_id: str,
        authorization_endpoint: str, token_endpoint: str, redirect_uri: str,
        cookie_hmac_key: bytes, state_encryption_key: bytes,
        pinned_public_keys: Mapping[str, bytes],
        token_exchanger: Callable[[str, str, str, str], str] | None,
        clock: Callable[[], float] = time.time,
    ):
        try:
            target = urlsplit(redirect_uri)
            if (
                not isinstance(path, (str, Path)) or not Path(path).is_absolute()
                or not _valid_issuer(issuer)
                or not _endpoint(authorization_endpoint)
                or not _endpoint(token_endpoint)
                or not _endpoint(redirect_uri)
                or type(cookie_hmac_key) is not bytes
                or len(cookie_hmac_key) < 32
                or type(state_encryption_key) is not bytes
                or len(state_encryption_key) != 32
                or not callable(clock)
                or target.port not in (None, 443)
                or target.hostname is None
            ):
                raise OidcBrowserCallbackReviewError()
            self._db = Path(path).resolve()
            self._issuer = issuer
            self._client = client_id
            self._auth_url = authorization_endpoint
            self._token_url = token_endpoint
            self._redirect = redirect_uri
            self._host = target.netloc
            self._path = target.path
            self._cookie_key = cookie_hmac_key
            self._aes_key = state_encryption_key
            self._keys = dict(pinned_public_keys)
            self._exchange = token_exchanger
            self._clock = clock
            # Validate all pinned OAuth config/public keys now, before any
            # browser challenge or cookie can be emitted.
            self._flow("B" * 43)
        except Exception:
            raise OidcBrowserCallbackReviewError() from None

    def _enabled(self) -> bool:
        return (
            os.environ.get(_FLAG) == "true"
            and os.environ.get("MEMORY_OIDC_CODE_PKCE_REVIEW_ENABLED") == "true"
            and os.environ.get("MEMORY_OIDC_ID_TOKEN_VERIFIER_REVIEW_ENABLED") == "true"
        )

    def _now(self) -> int:
        value = self._clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise OidcBrowserCallbackReviewError()
        return int(value)

    def _flow(self, browser_nonce: str) -> OidcCodePkceOfflineReview:
        return OidcCodePkceOfflineReview(
            path=self._db, issuer=self._issuer, client_id=self._client,
            authorization_endpoint=self._auth_url,
            token_endpoint=self._token_url, redirect_uri=self._redirect,
            browser_binding_provider=lambda: browser_nonce,
            token_exchanger=self._exchange,
            pinned_public_keys=self._keys,
            at_rest_key=self._aes_key,
            clock=self._clock,
        )

    def _mac(self, nonce: str, issued_at: str) -> str:
        payload = (
            "gt-oidc-browser-v1\n" + self._issuer + "\n" + self._redirect
            + "\n" + nonce + "\n" + issued_at
        ).encode("ascii")
        import base64
        digest = hmac.new(self._cookie_key, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def _basic_request(self, request: Request, method: str, path: str) -> None:
        if not self._enabled() or not isinstance(request, Request):
            raise OidcBrowserCallbackReviewError()
        headers = request.headers
        if (
            request.method != method
            or request.url.scheme != "https"
            or request.url.path != path
            or len(headers.getlist("host")) != 1
            or headers["host"] != self._host
            or any(k in headers for k in _BLOCKED_HEADERS)
            or len(headers.getlist("authorization")) != 0
            or len(headers.getlist("sec-fetch-site")) > 1
            or len(headers.getlist("origin")) > 1
            or len(headers.getlist("referer")) > 1
            or len(headers.getlist("cookie")) > 1
        ):
            raise OidcBrowserCallbackReviewError()

    def _issue_cookie(self, nonce: str, issued_at: int) -> str:
        ts = str(issued_at)
        signature = self._mac(nonce, ts)
        return (
            f"{_COOKIE}={nonce}.{ts}.{signature}; Path=/; Max-Age={_TTL}; "
            "Secure; HttpOnly; SameSite=Lax"
        )

    def _cookie_nonce(self, request: Request) -> str:
        headers = request.headers
        if len(headers.getlist("cookie")) != 1:
            raise OidcBrowserCallbackReviewError()
        fields = request.headers["cookie"].split(";")
        matched = []
        for field in fields:
            part = field.strip().split("=", 1)
            if len(part) != 2 or not part[0] or _CONTROL.search(part[1]):
                raise OidcBrowserCallbackReviewError()
            if part[0] == _COOKIE:
                matched.append(part[1])
        if len(matched) != 1:
            raise OidcBrowserCallbackReviewError()
        parts = matched[0].split(".")
        if (
            len(parts) != 3 or not _TOKEN.fullmatch(parts[0])
            or not _TIME.fullmatch(parts[1])
            or not _TOKEN.fullmatch(parts[2])
            or parts[1] != str(int(parts[1]))
            or not hmac.compare_digest(
                parts[2], self._mac(parts[0], parts[1])
            )
        ):
            raise OidcBrowserCallbackReviewError()
        now, issue = self._now(), int(parts[1])
        if issue > now or now - issue >= _TTL:
            raise OidcBrowserCallbackReviewError()
        return parts[0]

    def begin(self, request: Request) -> OidcBrowserStartReview:
        """Review a same-origin browser POST, no registered ASGI endpoint."""
        self._basic_request(request, "POST", "/__offline_review__/oidc/start")
        headers = request.headers
        if (
            request.query_params or request.scope.get("query_string", b"")
            or len(headers.getlist("origin")) != 1
            or headers["origin"] != f"https://{self._host}"
            or len(headers.getlist("sec-fetch-site")) != 1
            or headers["sec-fetch-site"] != "same-origin"
            or len(headers.getlist("content-type")) != 1
            or headers["content-type"] != "application/json"
            or len(headers.getlist("cookie")) != 0
        ):
            raise OidcBrowserCallbackReviewError()
        try:
            nonce = secrets.token_urlsafe(32)
            if not _TOKEN.fullmatch(nonce):
                raise OidcBrowserCallbackReviewError()
            issued_at = self._now()
            url = self._flow(nonce).prepare().authorize_url
            if not self._enabled():
                raise OidcBrowserCallbackReviewError()
            return OidcBrowserStartReview(
                authorization_url=url,
                set_cookie=self._issue_cookie(nonce, issued_at),
            )
        except Exception:
            raise OidcBrowserCallbackReviewError() from None

    def complete(self, request: Request) -> OidcBrowserCallbackReviewResult:
        """Cross-site top-level GET. State already single-use in #149.

        Do NOT insist on same-origin Fetch metadata or browser Origin on
        callback: the authorization server navigates across sites. Query
        'iss' is compared exactly before any code exchange (RFC 9207).
        """
        self._basic_request(request, "GET", self._path)
        if (
            len(request.headers.getlist("content-type")) > 0
            or len(request.headers.getlist("sec-fetch-mode")) > 1
            or (request.headers.get("sec-fetch-mode") not in (
                None, "navigate",
            ))
            or len(request.headers.getlist("sec-fetch-dest")) > 1
            or request.headers.get("sec-fetch-dest") not in (
                None, "document",
            )
            or len(request.headers.getlist("origin")) != 0
        ):
            raise OidcBrowserCallbackReviewError()
        try:
            raw = request.scope.get("query_string", b"")
            if (
                type(raw) is not bytes or not 1 <= len(raw) <= _MAX_QUERY_BYTES
                or any(b < 33 or b > 126 for b in raw)
            ):
                raise OidcBrowserCallbackReviewError()
            query = raw.decode("ascii")
            if re.search(r"%(?![a-fA-F0-9]{2})", query):
                raise OidcBrowserCallbackReviewError()
            pairs = parse_qsl(
                query, keep_blank_values=True, strict_parsing=True,
                encoding="utf-8", errors="strict", max_num_fields=4,
            )
            if len(pairs) != 3 or {k for k, _ in pairs} != {
                "state", "code", "iss"
            }:
                raise OidcBrowserCallbackReviewError()
            parameters = dict(pairs)
            if parameters["iss"] != self._issuer:
                raise OidcBrowserCallbackReviewError()
            nonce = self._cookie_nonce(request)
            result = self._flow(nonce).complete(
                state=parameters["state"], code=parameters["code"],
            )
            if (
                type(result) is not VerifiedOidcSubject
                or result.may_issue_browser_session is not False
                or result.may_write_memory is not False
                or not self._enabled()
            ):
                raise OidcBrowserCallbackReviewError()
            return OidcBrowserCallbackReviewResult(verified_subject=result)
        except Exception:
            raise OidcBrowserCallbackReviewError() from None
