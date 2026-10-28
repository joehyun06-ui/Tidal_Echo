"""Default-OFF strict one-hop trusted reverse-proxy provenance review.

NOT a production middleware, socket authenticator or login grant. The source
socket peer MUST be obtained from a separately reviewed, immutable server-side
transport boundary: ASGI scope['client'] is deliberately NEVER trusted here.
The proxy must strip incoming forwarding headers and issue exactly one fresh
header family. No rewriting the ASGI scope or authorizing user memory writes.
"""
from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from starlette.requests import Request

_FLAG = "MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED"
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z", re.ASCII)
_BAD_HEADERS = frozenset({
    "x-real-ip", "x-forwarded-port", "x-forwarded-prefix",
    "x-forwarded-server", "x-original-url", "x-rewrite-url",
    "x-user-id", "x-principal-id", "x-account-id", "x-login-id",
    "x-auth-epoch", "x-session-id", "x-memory-identity",
    "authorization", "proxy-authorization",
})
_XFAMILY = frozenset({"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"})
_ALLOWED = frozenset({"host", "forwarded", *_XFAMILY})
_MAX_HEADERS = 48
_MAX_HEADER_BYTES = 1024


class TrustedProxyReviewError(RuntimeError):
    def __init__(self):
        super().__init__("memory_oidc_trusted_proxy_not_ready")


@dataclass(frozen=True, slots=True, repr=False)
class ReviewedProxyProvenance:
    """Unprivileged review result, never a principal or memory capability."""
    external_origin: str
    client_ip: str = field(repr=False)
    proxy_peer: str = field(repr=False)
    family: str
    may_issue_browser_session: bool = False
    may_write_memory: bool = False

    def __repr__(self):
        return "<ReviewedProxyProvenance unprivileged>"


def _ip(value: object, *, require_global: bool) -> str:
    if type(value) is not str or len(value) > 45 or "%" in value:
        raise TrustedProxyReviewError()
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        raise TrustedProxyReviewError() from None
    if parsed.version == 6 and parsed.ipv4_mapped is not None:
        raise TrustedProxyReviewError()
    if value != parsed.compressed:
        raise TrustedProxyReviewError()
    if require_global and not parsed.is_global:
        raise TrustedProxyReviewError()
    return value


def _host(value: object) -> str:
    if (
        type(value) is not str or not 4 <= len(value) <= 253
        or not _HOST.fullmatch(value) or value.endswith(
            (".local", ".internal", ".localhost")
        )
    ):
        raise TrustedProxyReviewError()
    return value


class TrustedOidcProxyProvenanceReview:
    """Accept ONE explicitly configured proxy, stripped one-hop headers.

    The server-only peer and ingress attestation providers are NOT implemented
    here. Real operation must prove an untampered socket peer, a proxy that
    overwrites incoming headers and an app server with proxy-header rewriting
    disabled. Neither provider may derive its output from user HTTP headers,
    request.state or already-normalized ASGI client/scheme fields.
    """

    __slots__ = (
        "_peer", "_proof", "_trusted", "_public", "_internal", "_path",
    )

    def __init__(
        self, *, approved_peer_ips: tuple[str, ...],
        public_host: str, internal_host: str,
        review_path: str,
        raw_socket_peer_provider: Callable[[], str] | None,
        ingress_header_sanitization_proof: Callable[[], bool] | None,
    ):
        try:
            if (
                type(approved_peer_ips) is not tuple
                or not 1 <= len(approved_peer_ips) <= 4
                or len(set(approved_peer_ips)) != len(approved_peer_ips)
                or type(internal_host) is not str or not _HOST.fullmatch(internal_host)
                or type(review_path) is not str
                or not review_path.startswith("/") or len(review_path) > 128
                or "//" in review_path or "?" in review_path
                or not re.fullmatch(r"/[A-Za-z0-9/_-]+", review_path, re.ASCII)
                or not callable(raw_socket_peer_provider)
                or not callable(ingress_header_sanitization_proof)
            ):
                raise TrustedProxyReviewError()
            trusted = frozenset(_ip(x, require_global=False) for x in approved_peer_ips)
            public = _host(public_host)
            if public == internal_host:
                raise TrustedProxyReviewError()
            self._trusted = trusted
            self._public = public
            self._internal = internal_host
            self._path = review_path
            self._peer = raw_socket_peer_provider
            self._proof = ingress_header_sanitization_proof
        except Exception:
            raise TrustedProxyReviewError() from None

    def _peer_provenance(self) -> str:
        if os.environ.get(_FLAG) != "true" or self._proof() is not True:
            raise TrustedProxyReviewError()
        value = _ip(self._peer(), require_global=False)
        if value not in self._trusted:
            raise TrustedProxyReviewError()
        return value

    def evaluate(self, request: Request) -> ReviewedProxyProvenance:
        if os.environ.get(_FLAG) != "true" or type(request) is not Request:
            raise TrustedProxyReviewError()
        try:
            peer = self._peer_provenance()
            scope = request.scope
            if (
                scope.get("type") != "http"
                or scope.get("scheme") != "http"
                or scope.get("path") != self._path
                or scope.get("root_path", "") != ""
                or scope.get("server") != ("127.0.0.1", 8081)
                or type(scope.get("query_string", b"")) is not bytes
                or scope.get("query_string", b"") != b""
            ):
                raise TrustedProxyReviewError()

            # Read uncombined ASGI byte pairs, not convenience headers which
            # may hide duplicated case-insensitive header names.
            raw = scope.get("headers")
            if type(raw) is not list or not 1 <= len(raw) <= _MAX_HEADERS:
                raise TrustedProxyReviewError()
            headers = {}
            for entry in raw:
                if (
                    type(entry) is not tuple or len(entry) != 2
                    or type(entry[0]) is not bytes or type(entry[1]) is not bytes
                    or not 1 <= len(entry[0]) <= 64
                    or not 1 <= len(entry[1]) <= _MAX_HEADER_BYTES
                ):
                    raise TrustedProxyReviewError()
                key = entry[0].decode("ascii").lower()
                value = entry[1].decode("ascii")
                if (
                    not re.fullmatch(r"[a-z0-9-]+", key, re.ASCII)
                    or any(ord(c) < 33 or ord(c) > 126 for c in value)
                    or key in headers or key in _BAD_HEADERS
                    or key.startswith("x-forwarded-") and key not in _XFAMILY
                ):
                    raise TrustedProxyReviewError()
                headers[key] = value
            if headers.get("host") != self._internal:
                raise TrustedProxyReviewError()
            if "forwarded" in headers:
                if any(x in headers for x in _XFAMILY):
                    raise TrustedProxyReviewError()
                # Restricted one-hop RFC 7239 subset; no quoted/IPv6 values
                # or multiple proxy entries. Unsupported formats reject.
                parts = headers["forwarded"].split(";")
                if len(parts) != 3:
                    raise TrustedProxyReviewError()
                options = {}
                for part in parts:
                    if part.count("=") != 1:
                        raise TrustedProxyReviewError()
                    k, v = part.split("=", 1)
                    if k in options:
                        raise TrustedProxyReviewError()
                    options[k] = v
                if set(options) != {"for", "proto", "host"}:
                    raise TrustedProxyReviewError()
                client = _ip(options["for"], require_global=True)
                if ":" in client:
                    raise TrustedProxyReviewError()
                if options["proto"] != "https" or options["host"] != self._public:
                    raise TrustedProxyReviewError()
                family = "forwarded"
            else:
                if set(headers).intersection(_XFAMILY) != _XFAMILY:
                    raise TrustedProxyReviewError()
                client = _ip(headers["x-forwarded-for"], require_global=True)
                if (
                    headers["x-forwarded-proto"] != "https"
                    or headers["x-forwarded-host"] != self._public
                ):
                    raise TrustedProxyReviewError()
                family = "x-forwarded"
            if os.environ.get(_FLAG) != "true":
                raise TrustedProxyReviewError()
            if self._proof() is not True or self._peer_provenance() != peer:
                raise TrustedProxyReviewError()
            return ReviewedProxyProvenance(
                external_origin="https://" + self._public,
                client_ip=client, proxy_peer=peer, family=family,
            )
        except Exception:
            raise TrustedProxyReviewError() from None
