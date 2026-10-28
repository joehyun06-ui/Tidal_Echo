"""Synthetic pinned external-origin bridge for isolated OIDC ASGI review.

NOT a production trusted ingress. The proof callback is deliberately a
TEST-ONLY seam; it CANNOT attest Render edge, TLS or raw socket provenance.
The only allowed inner app is the existing three-route isolated OIDC review.
Neither this adapter nor the inner reviewer can grant login or memory writes.
Not imported by backend.app / backend.p3_relay_app / Render supervisor.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.responses import Response

from .memory_oidc_browser_callback_review import OidcBrowserCallbackOfflineReview

_FLAG = "MEMORY_OIDC_PINNED_ORIGIN_REHEARSAL_ENABLED"
_HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z",
    re.ASCII,
)
_PATHS = frozenset({
    "/__offline_review__/oidc/start", "/callback/oidc",
    "/__offline_review__/oidc/done",
})
_DISALLOWED = frozenset({
    "forwarded", "x-real-ip", "x-original-url", "x-rewrite-url",
    "proxy-authorization", "authorization", "x-user-id", "x-principal-id",
    "x-memory-identity", "x-login-id", "x-auth-epoch", "cf-connecting-ip",
    "cf-ray", "rndr-id", "true-client-ip", "x-client-ip",
})
_UNIQUE = frozenset({"host", "origin", "cookie", "content-type", "referer",
                     "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest"})


class PinnedOriginOfflineReviewError(RuntimeError):
    def __init__(self):
        super().__init__("memory_oidc_pinned_origin_unavailable")


class SyntheticPinnedOriginAsgi:
    """Only an offline compatibility rehearsal, never production middleware.

    A synthetic_proof_provider() result of True is purely TEST FIXTURE input:
    it is not evidence about the HTTP request, Render, TLS or an authenticated
    user. A real release requires a separately reviewed immutable ingress
    boundary; do not wire this callback into a public service.
    """
    __slots__ = ("_inner", "_origin", "_host", "_server", "_proof")

    def __init__(
        self, *, isolated_app: Starlette,
        review: OidcBrowserCallbackOfflineReview,
        public_origin: str, expected_internal_server: tuple[str, int | None],
        synthetic_proof_provider: Callable[[], bool] | None,
    ) -> None:
        try:
            if (
                type(isolated_app) is not Starlette
                or type(review) is not OidcBrowserCallbackOfflineReview
                or type(public_origin) is not str
                or type(expected_internal_server) is not tuple
                or len(expected_internal_server) != 2
                or type(expected_internal_server[0]) is not str
                or (
                    expected_internal_server[1] is not None
                    and (type(expected_internal_server[1]) is not int
                         or not 1 <= expected_internal_server[1] <= 65535)
                )
                or not callable(synthetic_proof_provider)
                or len(isolated_app.routes) != 3
                or {route.path for route in isolated_app.routes} != _PATHS
            ):
                raise PinnedOriginOfflineReviewError()
            parsed = urlsplit(public_origin)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or not _HOST.fullmatch(parsed.hostname)
                or parsed.netloc != parsed.hostname
                or parsed.path or parsed.query or parsed.fragment
                or parsed.username is not None or parsed.password is not None
                or public_origin != "https://" + parsed.hostname
                or review._redirect != public_origin + review._path
                or review._path != "/callback/oidc"
            ):
                raise PinnedOriginOfflineReviewError()
            self._inner = isolated_app
            self._origin = public_origin
            self._host = parsed.hostname
            self._server = expected_internal_server
            self._proof = synthetic_proof_provider
        except Exception:
            raise PinnedOriginOfflineReviewError() from None

    @staticmethod
    def _enabled() -> bool:
        return (
            os.environ.get(_FLAG) == "true"
            and os.environ.get("MEMORY_OIDC_ISOLATED_ASGI_REVIEW_ENABLED")
                == "true"
            and os.environ.get("MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED")
                != "true"
            and os.environ.get("MEMORY_OIDC_HTTPS_SENDER_REVIEW_ENABLED")
                != "true"
        )

    def _scope_ok(self, scope: dict) -> bool:
        if (
            type(scope) is not dict
            or scope.get("type") != "http"
            or scope.get("scheme") != "http"
            or scope.get("server") != self._server
            or scope.get("path") not in _PATHS
            or scope.get("root_path", "") != ""
            or type(scope.get("query_string", b"")) is not bytes
        ):
            return False
        raw = scope.get("headers")
        if type(raw) is not list or not 1 <= len(raw) <= 48:
            return False
        seen: set[str] = set()
        host: str | None = None
        for item in raw:
            if (
                type(item) is not tuple or len(item) != 2
                or type(item[0]) is not bytes or type(item[1]) is not bytes
                or not 1 <= len(item[0]) <= 64
                or not 1 <= len(item[1]) <= 1024
            ):
                return False
            try:
                name = item[0].decode("ascii").lower()
                value = item[1].decode("ascii")
            except UnicodeDecodeError:
                return False
            if (
                not re.fullmatch(r"[a-z0-9-]+", name, re.ASCII)
                or any(ord(ch) < 32 or ord(ch) > 126 for ch in value)
                or name in _DISALLOWED or name.startswith("x-forwarded-")
                or name.startswith("cf-")
                or (name in _UNIQUE and name in seen)
            ):
                return False
            seen.add(name)
            if name == "host":
                host = value
            if name == "origin" and value != self._origin:
                return False
        return host == self._host

    async def __call__(self, scope, receive, send) -> None:
        if not self._enabled():
            await Response(status_code=404)(scope, receive, send)
            return
        try:
            if not self._scope_ok(scope):
                raise PinnedOriginOfflineReviewError()
            # TEST SIMULATION ONLY: independent of user-controlled header
            # fields, ASGI client address and request.state.
            if self._proof() is not True:
                raise PinnedOriginOfflineReviewError()
            if not self._enabled() or self._proof() is not True:
                raise PinnedOriginOfflineReviewError()
        except Exception:
            await Response(status_code=400)(scope, receive, send)
            return
        # Only the newly copied ASGI scope is altered. Never change original
        # scope, client, server, Host or forwarded headers. Inner app owns
        # its own exception handling and ASGI response-start lifecycle.
        synthetic_scope = dict(scope, scheme="https")
        await self._inner(synthetic_scope, receive, send)

    def __repr__(self):
        return "<SyntheticPinnedOriginAsgi offline_unprivileged>"
