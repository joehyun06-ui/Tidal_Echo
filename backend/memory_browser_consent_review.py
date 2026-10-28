"""Server-owned browser consent preflight rehearsal, OFF by default.

NO HTTP route, NO memory writes, NO cross-process consent authority.
The caller is responsible for authenticating each request and supplying a
trusted server-owned session context; this module cannot authenticate a user.
A client-side 'confirmed' boolean is NOT human-consent evidence. The returned
receipt is deliberately not usable by the memory writer or ticket gate.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .memory_explicit_actions import RememberExplicitMemoryRequest
from .memory_session_confirmation import CONFIRMATION_TEXT, _digest
from .memory_session_write_guard import verify_server_session_write
from .memory_shared_confirmation_ticket import (
    TrustedTicketContext, _request_valid,
)

_FLAG = "MEMORY_BROWSER_CONSENT_REVIEW_ENABLED"
_TTL_SECONDS = 120
_MAX_PENDING = 8
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z", re.ASCII)
_PRINCIPAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_LOGIN = re.compile(r"[A-Za-z0-9_-]{24,96}\Z", re.ASCII)
_DIGEST = re.compile(r"[a-f0-9]{64}\Z", re.ASCII)


class BrowserConsentReviewError(RuntimeError):
    def __init__(self):
        super().__init__("browser_memory_consent_unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class TrustedBrowserReviewContext:
    """SERVER-provided data. Constructing this dataclass is NOT authentication."""
    identity: TrustedTicketContext = field(repr=False)
    origin: str = field(repr=False)
    csrf_token: str = field(repr=False)

    def __repr__(self):
        return "<TrustedBrowserReviewContext>"


@dataclass(frozen=True, slots=True, repr=False)
class BrowserConsentPreview:
    """Only a synthetic review offer, NOT a write or authentication token."""
    nonce: str = field(repr=False)
    content_sha256: str = field(repr=False)
    preview_content: str = field(repr=False)
    confirmation_text: str = CONFIRMATION_TEXT
    expires_in_seconds: int = _TTL_SECONDS

    def __repr__(self):
        return "<BrowserConsentPreview>"


@dataclass(frozen=True, slots=True, repr=False)
class BrowserConsentReviewReceipt:
    """Non-transferable mock result. NOT a capability or evidence of a click."""
    request_id: str = field(repr=False)

    def __repr__(self):
        return "<BrowserConsentReviewReceipt>"


@dataclass(frozen=True, slots=True, repr=False)
class _Pending:
    principal: str
    login_id: str
    epoch_hash: str = field(repr=False)
    session_id: str
    request_id_hash: str = field(repr=False)
    request_digest: str = field(repr=False)
    csrf_hash: str = field(repr=False)
    origin: str
    expires_at: float


def _enabled() -> bool:
    return (
        os.environ.get(_FLAG) == "true"
        and os.environ.get("MEMORY_TRUSTED_AUTH_EPOCH_ENABLED") == "true"
        and os.environ.get("MEMORY_SHARED_CONFIRMATION_TICKETS_ENABLED") == "true"
    )


def _str_matches(value: object, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _valid_origin(origin: object) -> bool:
    if not isinstance(origin, str) or not origin or len(origin) > 256:
        return False
    if any(ord(ch) <= 32 or ord(ch) >= 127 for ch in origin):
        return False
    try:
        parsed = urlsplit(origin)
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment
            or not parsed.hostname or parsed.port is None and ":" in parsed.netloc
        ):
            return False
        allowed = parsed.scheme == "https" or (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
        )
        if not allowed:
            return False
        # Require one exact canonical representation; no casefold, suffix,
        # subdomain, wildcard, userinfo, URL path, or scheme confusion.
        default_port = "" if parsed.port is None else ":" + str(parsed.port)
        return origin == f"{parsed.scheme}://{parsed.hostname}{default_port}"
    except (TypeError, ValueError):
        return False


class BrowserConsentReview:
    """Process-local security *preflight*, not an authorization endpoint.

    For real deployment: a trusted, request-scoped authentication middleware
    must populate context_provider; epoch_checker MUST consult the durable
    login registry. Neither provider can accept client-supplied identities.
    Same-process review cannot replace the #142/#143 SQLite commit-time gate.
    """

    __slots__ = ("_provider", "_epoch_checker", "_clock", "_lock", "_pending")

    def __init__(
        self, *,
        context_provider: Callable[[], TrustedBrowserReviewContext],
        epoch_checker: Callable[[TrustedTicketContext], None],
        clock: Callable[[], float] = time.monotonic,
    ):
        if not all(callable(v) for v in (context_provider, epoch_checker, clock)):
            raise BrowserConsentReviewError()
        self._provider = context_provider
        self._epoch_checker = epoch_checker
        self._clock = clock
        self._lock = threading.RLock()
        self._pending: dict[str, _Pending] = {}

    def _now(self):
        try:
            now = self._clock()
            if type(now) not in (float, int) or not math.isfinite(now) or now < 0:
                raise BrowserConsentReviewError()
            return float(now)
        except Exception:
            raise BrowserConsentReviewError() from None

    def _trusted(self, request, *, origin: str, csrf_token: str):
        if not _enabled() or not _request_valid(request):
            raise BrowserConsentReviewError()
        try:
            ctx = self._provider()
            if type(ctx) is not TrustedBrowserReviewContext:
                raise BrowserConsentReviewError()
            identity = ctx.identity
            if (
                type(identity) is not TrustedTicketContext
                or not _str_matches(identity.principal_id, _PRINCIPAL)
                or not _str_matches(identity.login_id, _LOGIN)
                or not _str_matches(identity.auth_epoch, _TOKEN)
                or type(identity.canonical_sessions) is not tuple
                or not _valid_origin(ctx.origin)
                or not _str_matches(ctx.csrf_token, _TOKEN)
                or not _str_matches(csrf_token, _TOKEN)
                or type(origin) is not str
                or not hmac.compare_digest(origin, ctx.origin)
                or not hmac.compare_digest(csrf_token, ctx.csrf_token)
            ):
                raise BrowserConsentReviewError()
            verify_server_session_write(
                request,
                trusted_session_id=identity.session_id,
                canonical_sessions=identity.canonical_sessions,
            )
            self._epoch_checker(identity)
            if not _enabled():
                raise BrowserConsentReviewError()
            return ctx
        except Exception:
            raise BrowserConsentReviewError() from None

    def prepare(
        self, request: RememberExplicitMemoryRequest, *,
        origin: str, csrf_token: str,
    ) -> BrowserConsentPreview:
        ctx = self._trusted(request, origin=origin, csrf_token=csrf_token)
        with self._lock:
            if not _enabled():
                raise BrowserConsentReviewError()
            now = self._now()
            for key, pending in tuple(self._pending.items()):
                if pending.expires_at <= now:
                    del self._pending[key]
            if len(self._pending) >= _MAX_PENDING:
                raise BrowserConsentReviewError()
            nonce = secrets.token_urlsafe(32)
            nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
            if nonce_hash in self._pending:
                raise BrowserConsentReviewError()
            identity = ctx.identity
            digest = _digest(request)
            self._pending[nonce_hash] = _Pending(
                principal=identity.principal_id,
                login_id=identity.login_id,
                epoch_hash=hashlib.sha256(identity.auth_epoch.encode("ascii")).hexdigest(),
                session_id=identity.session_id,
                request_id_hash=hashlib.sha256(request.request_id.encode("ascii")).hexdigest(),
                request_digest=digest,
                csrf_hash=hashlib.sha256(ctx.csrf_token.encode("ascii")).hexdigest(),
                origin=ctx.origin,
                expires_at=now + _TTL_SECONDS,
            )
            return BrowserConsentPreview(
                nonce=nonce,
                content_sha256=digest,
                preview_content=request.content,
            )

    def confirm(
        self, request: RememberExplicitMemoryRequest, *,
        nonce: str, content_sha256: str, confirmation_text: str,
        origin: str, csrf_token: str, confirmed: bool,
    ) -> BrowserConsentReviewReceipt:
        if not _enabled() or not _str_matches(nonce, _TOKEN):
            raise BrowserConsentReviewError()
        # One-use and reject-on-invalid: every known nonce is removed at most
        # once. Keep all decisions process-local and never grant writer access.
        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        with self._lock:
            pending = self._pending.pop(nonce_hash, None)
            if pending is None:
                raise BrowserConsentReviewError()
            now = self._now()
            ctx = self._trusted(request, origin=origin, csrf_token=csrf_token)
            identity = ctx.identity
            if (
                not _enabled()
                or pending.expires_at <= now
                or confirmed is not True
                or not _str_matches(content_sha256, _DIGEST)
                or type(confirmation_text) is not str
                or not hmac.compare_digest(confirmation_text, CONFIRMATION_TEXT)
                or not hmac.compare_digest(content_sha256, _digest(request))
                or not hmac.compare_digest(pending.request_digest, content_sha256)
                or not hmac.compare_digest(
                    pending.request_id_hash,
                    hashlib.sha256(request.request_id.encode("ascii")).hexdigest(),
                )
                or pending.principal != identity.principal_id
                or pending.login_id != identity.login_id
                or pending.session_id != identity.session_id
                or not hmac.compare_digest(
                    pending.epoch_hash,
                    hashlib.sha256(identity.auth_epoch.encode("ascii")).hexdigest(),
                )
                or not hmac.compare_digest(
                    pending.csrf_hash,
                    hashlib.sha256(ctx.csrf_token.encode("ascii")).hexdigest(),
                )
                or pending.origin != ctx.origin
            ):
                raise BrowserConsentReviewError()
            # This receipt cannot be used as a writer capability.
            return BrowserConsentReviewReceipt(request_id=request.request_id)

    def clear_for_logout(self) -> None:
        # Must be called alongside durable AuthEpochRegistry revocation.
        with self._lock:
            self._pending.clear()
