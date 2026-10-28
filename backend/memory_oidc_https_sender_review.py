"""Explicitly disabled by default: pinned OAuth HTTPS transport rehearsal.

This is a REAL Python HTTPS sender behind THREE deliberate permissions:
two environment switches and an injected runtime approval callback. There
are NO production callers, no token endpoint config, and no live requests in
CI. DNS results must ALL be public, and the connection is bound to a vetted
numeric IP while TLS SNI/hostname verification uses the pinned DNS name.
"""
from __future__ import annotations

import http.client
import ipaddress
import os
import re
import socket
import ssl
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from .memory_oidc_pkce_flow_review import _endpoint
from .memory_oidc_token_exchange_review import (
    OfflineTokenRequest, OfflineTokenResponse, _MAX_BODY,
)

_FLAG = "MEMORY_OIDC_HTTPS_SENDER_REVIEW_ENABLED"
_APPROVED = "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED"
_HOST = re.compile(
    r"(?=.{4,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z",
    re.ASCII,
)
_REQUIRED_HEADERS = (
    ("accept", "application/json"),
    ("content-type", "application/x-www-form-urlencoded"),
    ("cache-control", "no-store"),
)
_RESPONSE_HEADER_LIMIT = 32
_MAX_REQUEST_BODY = 4096
_MAX_HEADER_VALUE = 512
_MAX_WALL_TIME = 8.0


class OidcHttpsSenderReviewError(RuntimeError):
    def __init__(self):
        super().__init__("memory_oidc_https_sender_unavailable")


def _is_public_ip(value: object) -> bool:
    try:
        if type(value) is not str or "%" in value:
            return False
        ip = ipaddress.ip_address(value)
        return ip.is_global and not (
            isinstance(ip, ipaddress.IPv6Address) and
            ip.ipv4_mapped is not None
        )
    except ValueError:
        return False


def _validated_addresses(host: str) -> tuple[str, ...]:
    results = socket.getaddrinfo(
        host, 443, family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
    )
    if not 1 <= len(results) <= 16:
        raise OidcHttpsSenderReviewError()
    addresses = []
    for result in results:
        if type(result) is not tuple or len(result) != 5:
            raise OidcHttpsSenderReviewError()
        family, kind, proto, _, address = result
        if (
            family not in (socket.AF_INET, socket.AF_INET6)
            or kind != socket.SOCK_STREAM
            or proto not in (0, socket.IPPROTO_TCP)
            or type(address) is not tuple or len(address) < 2
            or address[1] != 443 or not _is_public_ip(address[0])
        ):
            raise OidcHttpsSenderReviewError()
        if address[0] not in addresses:
            addresses.append(address[0])
    return tuple(addresses)


class _PinnedHttpsConnection(http.client.HTTPSConnection):
    """Numeric-IP dial, original host retained for SNI + certificate checks."""

    def __init__(self, host: str, vetted_ip: str, *, context: ssl.SSLContext):
        super().__init__(host, port=443, timeout=3.0, context=context)
        if not _is_public_ip(vetted_ip):
            raise OidcHttpsSenderReviewError()
        self._vetted_ip = vetted_ip
        self._create_connection = self._dial_numeric

    def _dial_numeric(self, address, timeout, source_address):
        if address != (self.host, 443):
            raise OidcHttpsSenderReviewError()
        # Numeric IP skips a second hostname DNS lookup: the vetted address
        # cannot change via an intervening hostname DNS rebinding.
        return socket.create_connection(
            (self._vetted_ip, 443), timeout, source_address,
        )


class PinnedOidcHttpsSenderReview:
    """No external callers; must be injected into #150 after independent review.

    The only mutable authority is process-local approval, which must be True
    exactly, and two explicit process flags. This does NOT provide actual
    browser login, token verification, or permission to write memory.
    """

    __slots__ = ("_endpoint", "_host", "_approval")

    def __init__(
        self, *, approved_token_endpoint: str, approved_dns_hostname: str,
        network_approval_provider: Callable[[], bool] | None = None,
    ):
        if (
            not _endpoint(approved_token_endpoint)
            or type(approved_dns_hostname) is not str
            or not _HOST.fullmatch(approved_dns_hostname)
            or approved_dns_hostname.endswith((".local", ".internal", ".localhost"))
            or urlsplit(approved_token_endpoint).hostname != approved_dns_hostname
            or (
                network_approval_provider is not None
                and not callable(network_approval_provider)
            )
        ):
            raise OidcHttpsSenderReviewError()
        try:
            ipaddress.ip_address(approved_dns_hostname)
        except ValueError:
            pass
        else:
            raise OidcHttpsSenderReviewError()
        self._endpoint = approved_token_endpoint
        self._host = approved_dns_hostname
        self._approval = network_approval_provider

    def _permitted(self) -> bool:
        return (
            os.environ.get(_FLAG) == "true"
            and os.environ.get(_APPROVED) == "true"
            and callable(self._approval)
            and self._approval() is True
        )

    def __call__(self, request: OfflineTokenRequest) -> OfflineTokenResponse:
        if not self._permitted():
            raise OidcHttpsSenderReviewError()
        conn = None
        try:
            if (
                type(request) is not OfflineTokenRequest
                or request.method != "POST"
                or request.url != self._endpoint
                or request.headers != _REQUIRED_HEADERS
                or type(request.body) is not bytes
                or not 1 <= len(request.body) <= _MAX_REQUEST_BODY
                or b"\x00" in request.body or b"\r" in request.body
                or b"\n" in request.body
            ):
                raise OidcHttpsSenderReviewError()
            endpoint = urlsplit(self._endpoint)
            if not _endpoint(self._endpoint):
                raise OidcHttpsSenderReviewError()
            deadline = time.monotonic() + _MAX_WALL_TIME
            addresses = _validated_addresses(self._host)
            if not addresses or not self._permitted():
                raise OidcHttpsSenderReviewError()
            context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
                raise OidcHttpsSenderReviewError()
            conn = _PinnedHttpsConnection(
                self._host, addresses[0], context=context,
            )
            if not self._permitted():
                raise OidcHttpsSenderReviewError()
            conn.request(
                "POST", endpoint.path,
                body=request.body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cache-Control": "no-store",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            def remaining():
                value = deadline - time.monotonic()
                if value <= 0:
                    raise OidcHttpsSenderReviewError()
                return min(3.0, value)
            conn.sock.settimeout(remaining())
            response = conn.getresponse()
            headers = response.getheaders()
            if len(headers) > _RESPONSE_HEADER_LIMIT:
                raise OidcHttpsSenderReviewError()
            canonical = []
            for key, val in headers:
                if (
                    type(key) is not str or type(val) is not str
                    or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", key, re.ASCII)
                    or len(val) > _MAX_HEADER_VALUE
                    or any(ord(c) < 32 or ord(c) > 126 for c in val)
                ):
                    raise OidcHttpsSenderReviewError()
                canonical.append((key.lower(), val))
            length = [
                value for key, value in canonical if key == "content-length"
            ]
            if len(length) > 1 or (
                length and (
                    not length[0].isdigit() or int(length[0]) > _MAX_BODY
                )
            ):
                raise OidcHttpsSenderReviewError()
            body = bytearray()
            while len(body) <= _MAX_BODY:
                conn.sock.settimeout(remaining())
                chunk = response.read(min(2048, _MAX_BODY + 1 - len(body)))
                if not chunk:
                    break
                if type(chunk) is not bytes:
                    raise OidcHttpsSenderReviewError()
                body.extend(chunk)
            if len(body) > _MAX_BODY or not self._permitted():
                raise OidcHttpsSenderReviewError()
            return OfflineTokenResponse(
                status_code=response.status,
                final_url=self._endpoint,
                headers=tuple(canonical),
                body=bytes(body),
            )
        except Exception:
            raise OidcHttpsSenderReviewError() from None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
