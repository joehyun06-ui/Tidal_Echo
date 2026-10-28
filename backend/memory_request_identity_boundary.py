"""Default-DENY request-scoped user-identity boundary, offline review only.

The legacy relay's shared Bearer secret is ACCESS AUTHENTICATION, not a
per-person authenticated login. No production per-user authenticator exists.
This module does NOT derive a principal from that secret, HTTP headers, a
cookie, session selectors, or request.state. An independently trusted
authenticator MUST be injected by a future reviewed composition root.

No HTTP route, memory write, durable consent, or grant for the memory UOW.
"""
from __future__ import annotations

import hmac
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from starlette.requests import Request

from .memory_browser_consent_review import _valid_origin
from .memory_explicit_actions import RememberExplicitMemoryRequest
from .memory_session_write_guard import verify_server_session_write
from .memory_shared_confirmation_ticket import TrustedTicketContext, _request_valid

_FLAG = "MEMORY_PER_USER_AUTH_REVIEW_ENABLED"
_REVIEW_PATH = "/__offline_review__/memory/identity"
_PRINCIPAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_LOGIN = re.compile(r"[A-Za-z0-9_-]{24,96}\Z", re.ASCII)
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z", re.ASCII)
_UNTRUSTED_ID_HEADERS = frozenset({
    "x-principal-id", "x-user-id", "x-account-id", "x-auth-epoch",
    "x-login-id", "x-session-id", "x-memory-identity",
})


class MemoryRequestIdentityError(RuntimeError):
    def __init__(self):
        super().__init__("memory_per_user_auth_not_ready")


@dataclass(frozen=True, slots=True, repr=False)
class ServerAuthenticatedRequest:
    """Must be produced by independent per-user login middleware.

    Constructing this record or passing a mock provider is NOT authentication.
    The provider cannot simply transform relay Bearer tokens into principals.
    """
    identity: TrustedTicketContext = field(repr=False)
    csrf_token: str = field(repr=False)
    browser_origin: str = field(repr=False)

    def __repr__(self):
        return "<ServerAuthenticatedRequest>"


@dataclass(frozen=True, slots=True, repr=False)
class RequestIdentityReviewResult:
    """No identities or credentials in the returned non-authorizing result."""
    accepted_for_review: bool = True
    may_write_memory: bool = False

    def __repr__(self):
        return "<RequestIdentityReviewResult review_only>"


class MemoryRequestIdentityBoundary:
    """Request-scoped, default-deny; no actual authentication implementation.

    Designed for a hypothetical POST browser consent review. The trust
    callback is deliberately REQUIRED; passing check_auth alone (or a relay
    Bearer) cannot populate identity. The test provider is synthetic only.
    """

    __slots__ = ("_authenticator", "_epoch_checker", "_expected_origin")

    def __init__(
        self, *,
        authenticator: Callable[[Request], ServerAuthenticatedRequest] | None = None,
        epoch_checker: Callable[[TrustedTicketContext], None] | None = None,
        expected_origin: str,
    ):
        if not _valid_origin(expected_origin):
            raise MemoryRequestIdentityError()
        self._authenticator = authenticator
        self._epoch_checker = epoch_checker
        self._expected_origin = expected_origin

    def review(
        self, request: Request,
        memory_request: RememberExplicitMemoryRequest,
    ) -> RequestIdentityReviewResult:
        if (
            os.environ.get(_FLAG) != "true"
            or not isinstance(request, Request)
            or not _request_valid(memory_request)
            or self._authenticator is None
            or self._epoch_checker is None
        ):
            raise MemoryRequestIdentityError()
        try:
            headers = request.headers
            if (
                request.method != "POST"
                or request.url.path != _REVIEW_PATH
                or bool(request.query_params)
                or len(headers.getlist("origin")) != 1
                or len(headers.getlist("x-memory-csrf")) != 1
                or len(headers.getlist("sec-fetch-site")) != 1
                or headers["sec-fetch-site"] != "same-origin"
                or any(header in headers for header in _UNTRUSTED_ID_HEADERS)
                or headers.get("content-type") != "application/json"
            ):
                raise MemoryRequestIdentityError()
            supplied_origin = headers["origin"]
            supplied_csrf = headers["x-memory-csrf"]
            if (
                not _valid_origin(supplied_origin)
                or not hmac.compare_digest(
                    supplied_origin, self._expected_origin,
                )
                or not _TOKEN.fullmatch(supplied_csrf)
            ):
                raise MemoryRequestIdentityError()

            # No use of headers['authorization'] as a user identity.
            # Independent authenticated middleware MUST validate the login.
            verified = self._authenticator(request)
            if type(verified) is not ServerAuthenticatedRequest:
                raise MemoryRequestIdentityError()
            identity = verified.identity
            if (
                type(identity) is not TrustedTicketContext
                or type(identity.principal_id) is not str
                or not _PRINCIPAL.fullmatch(identity.principal_id)
                or type(identity.login_id) is not str
                or not _LOGIN.fullmatch(identity.login_id)
                or type(identity.auth_epoch) is not str
                or not _TOKEN.fullmatch(identity.auth_epoch)
                or type(identity.canonical_sessions) is not tuple
                or type(verified.csrf_token) is not str
                or not _TOKEN.fullmatch(verified.csrf_token)
                or not _valid_origin(verified.browser_origin)
                or not hmac.compare_digest(
                    verified.browser_origin, self._expected_origin,
                )
                or not hmac.compare_digest(
                    supplied_csrf, verified.csrf_token,
                )
            ):
                raise MemoryRequestIdentityError()
            verify_server_session_write(
                memory_request,
                trusted_session_id=identity.session_id,
                canonical_sessions=identity.canonical_sessions,
            )
            # A real checker MUST consult the durable login registry, not
            # a cached browser/session dataclass.
            self._epoch_checker(identity)
            if os.environ.get(_FLAG) != "true":
                raise MemoryRequestIdentityError()
            return RequestIdentityReviewResult()
        except Exception:
            raise MemoryRequestIdentityError() from None
