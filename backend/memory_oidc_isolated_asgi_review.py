"""Isolated in-memory ASGI browser transport for the OIDC review protocol.

NOT imported or mounted by backend.app; MUST NOT be used as production login.
This factory returns a separate Starlette application solely for ASGITransport
unit tests. All flags default OFF. No real IdP, network or memory write.
"""
from __future__ import annotations

import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .memory_oidc_browser_callback_review import (
    OidcBrowserCallbackOfflineReview,
    OidcBrowserCallbackReviewError,
)

_FLAG = "MEMORY_OIDC_ISOLATED_ASGI_REVIEW_ENABLED"
_START = "/__offline_review__/oidc/start"
_DONE = "/__offline_review__/oidc/done"
_CALLBACK_COOKIE = "__Host-gt-oidc-review"
_TERMINAL = (
    f"{_CALLBACK_COOKIE}=; Path=/; Max-Age=0; "
    "Expires=Thu, 01 Jan 1970 00:00:00 GMT; "
    "Secure; HttpOnly; SameSite=Lax"
)
_SAFE_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


def _enabled() -> bool:
    return (
        os.environ.get(_FLAG) == "true"
        and os.environ.get("MEMORY_OIDC_BROWSER_CALLBACK_REVIEW_ENABLED") == "true"
        and os.environ.get("MEMORY_OIDC_CODE_PKCE_REVIEW_ENABLED") == "true"
        and os.environ.get("MEMORY_OIDC_ID_TOKEN_VERIFIER_REVIEW_ENABLED") == "true"
        and os.environ.get("MEMORY_OIDC_HTTPS_SENDER_REVIEW_ENABLED") != "true"
        and os.environ.get("MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED") != "true"
    )


def _generic(status: int) -> Response:
    return Response(status_code=status, headers=_SAFE_HEADERS)


def create_isolated_oidc_asgi_review(
    review: OidcBrowserCallbackOfflineReview,
) -> Starlette:
    """Explicit test-only factory. Never add these routes to backend.app.

    No endpoint returns a principal, ID token, session cookie, access token,
    state, PKCE verifier or code. A terminal redirect strips OAuth query from
    the resulting navigation URL even for rejected callbacks.
    """
    if type(review) is not OidcBrowserCallbackOfflineReview:
        raise ValueError("isolated_oidc_review_invalid")

    async def begin(request: Request) -> Response:
        if not _enabled():
            return _generic(404)
        try:
            # No client-provided identity/grant and no unbounded body reads.
            collected = bytearray()
            async for chunk in request.stream():
                if type(chunk) is not bytes or len(collected) + len(chunk) > 16:
                    return _generic(400)
                collected.extend(chunk)
            if bytes(collected) != b"{}":
                return _generic(400)
            details = review.begin(request)
            if not _enabled():
                return _generic(404)
            response = RedirectResponse(
                url=details.authorization_url, status_code=303,
                headers=_SAFE_HEADERS,
            )
            response.headers.append("Set-Cookie", details.set_cookie)
            return response
        except Exception:
            return _generic(400)

    async def callback(request: Request) -> Response:
        if not _enabled():
            return _generic(404)
        try:
            # Both success and error erase the anonymous prelogin cookie.
            # Do NOT expose whether a particular identity assertion passed.
            review.complete(request)
        except Exception:
            pass
        response = RedirectResponse(
            url=_DONE, status_code=303, headers=_SAFE_HEADERS,
        )
        response.headers.append("Set-Cookie", _TERMINAL)
        return response

    async def done(request: Request) -> Response:
        if not _enabled():
            return _generic(404)
        if request.query_params or request.scope.get("query_string", b""):
            return _generic(400)
        return PlainTextResponse(
            "Offline OIDC review only. No user login or memory write.",
            status_code=200, headers=_SAFE_HEADERS,
        )

    return Starlette(
        debug=False,
        routes=[
            Route(_START, endpoint=begin, methods=["POST"]),
            Route(review._path, endpoint=callback, methods=["GET"]),
            Route(_DONE, endpoint=done, methods=["GET"]),
        ],
    )
