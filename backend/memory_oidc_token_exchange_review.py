"""Pinned OAuth token endpoint wire-protocol review; NETWORK ALWAYS DISABLED.

An injected offline transport observes a strict form-encoded token request and
returns a bounded response. NO httpx/urllib sending, no HTTP route, no real IdP.
The parsed ID token MUST still be passed through the existing pinned RSA
verifier and original one-use state/nonce gate in #149.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode

from .memory_oidc_pkce_flow_review import _endpoint

_FLAG = "MEMORY_OIDC_TOKEN_EXCHANGE_REVIEW_ENABLED"
_MAX_BODY = 12288
_CODE = re.compile(r"[A-Za-z0-9._~-]{8,512}\Z", re.ASCII)
_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}\Z", re.ASCII)
_CLIENT = re.compile(r"[A-Za-z0-9._:-]{3,128}\Z", re.ASCII)
_ACCESS_TOKEN = re.compile(r"[\x21-\x7e]{16,4096}\Z", re.ASCII)
_JWT = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\Z", re.ASCII)
_SAFE_HEADER = re.compile(r"[a-z0-9-]{1,64}\Z", re.ASCII)


class OidcTokenExchangeReviewError(RuntimeError):
    def __init__(self):
        super().__init__("memory_oidc_token_exchange_unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class OfflineTokenRequest:
    method: str
    url: str = field(repr=False)
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    body: bytes = field(repr=False)

    def __repr__(self):
        return "<OfflineTokenRequest sensitive_review_only>"


@dataclass(frozen=True, slots=True, repr=False)
class OfflineTokenResponse:
    status_code: int
    final_url: str = field(repr=False)
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    body: bytes = field(repr=False)

    def __repr__(self):
        return "<OfflineTokenResponse redacted>"


def _reject_duplicates(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise OidcTokenExchangeReviewError()
        result[k] = v
    return result


def _headers(values):
    if type(values) is not tuple or len(values) > 32:
        raise OidcTokenExchangeReviewError()
    items = {}
    for pair in values:
        if type(pair) is not tuple or len(pair) != 2:
            raise OidcTokenExchangeReviewError()
        name, value = pair
        if (
            type(name) is not str or not _SAFE_HEADER.fullmatch(name)
            or type(value) is not str or len(value) > 512
            or any(ord(ch) < 32 or ord(ch) > 126 for ch in value)
            or name in items
        ):
            raise OidcTokenExchangeReviewError()
        items[name] = value
    return items


class PinnedOidcTokenExchangeReview:
    """Public-client code redemption wire framing, NOT an HTTP sender.

    In a real implementation, a separately security-reviewed transport must
    guarantee TLS, DNS/SSRF protection, no redirects, no proxies, timeouts,
    limited body bytes and no logging. This review never creates a client.
    """

    __slots__ = ("_token_url", "_redirect_uri", "_client", "_transport")

    def __init__(
        self, *, token_endpoint: str, redirect_uri: str, client_id: str,
        offline_transport: Callable[[OfflineTokenRequest], OfflineTokenResponse] | None = None,
    ):
        if (
            not _endpoint(token_endpoint) or not _endpoint(redirect_uri)
            or type(client_id) is not str or not _CLIENT.fullmatch(client_id)
            or (offline_transport is not None and not callable(offline_transport))
        ):
            raise OidcTokenExchangeReviewError()
        self._token_url = token_endpoint
        self._redirect_uri = redirect_uri
        self._client = client_id
        self._transport = offline_transport

    def exchange(self, code: str, code_verifier: str,
                 redirect_uri: str, token_endpoint: str) -> str:
        """Matches #149 injected callback; returns ONLY a raw ID token.

        The caller MUST run the pinned RS256 ID token verifier and enforce
        original nonce after this call; token response alone grants nothing.
        """
        if (
            os.environ.get(_FLAG) != "true"
            or not callable(self._transport)
            or type(code) is not str or not _CODE.fullmatch(code)
            or type(code_verifier) is not str
            or not _VERIFIER.fullmatch(code_verifier)
            or type(redirect_uri) is not str
            or redirect_uri != self._redirect_uri
            or type(token_endpoint) is not str
            or token_endpoint != self._token_url
        ):
            raise OidcTokenExchangeReviewError()
        try:
            payload = urlencode({
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_uri,
                "client_id": self._client,
                "code_verifier": code_verifier,
            }).encode("ascii")
            req = OfflineTokenRequest(
                method="POST", url=self._token_url,
                headers=(
                    ("accept", "application/json"),
                    ("content-type", "application/x-www-form-urlencoded"),
                    ("cache-control", "no-store"),
                ),
                body=payload,
            )
            response = self._transport(req)
            if (
                os.environ.get(_FLAG) != "true"
                or type(response) is not OfflineTokenResponse
                or type(response.status_code) is not int
                or response.status_code != 200
                or type(response.final_url) is not str
                or response.final_url != self._token_url
                or type(response.body) is not bytes
                or not 1 <= len(response.body) <= _MAX_BODY
            ):
                raise OidcTokenExchangeReviewError()
            headers = _headers(response.headers)
            if (
                headers.get("content-type") not in (
                    "application/json", "application/json; charset=utf-8",
                )
                or headers.get("cache-control") != "no-store"
                or headers.get("pragma") != "no-cache"
                or any(name in headers for name in (
                    "location", "set-cookie", "content-encoding",
                    "transfer-encoding", "www-authenticate",
                ))
                or ("content-length" in headers and
                    (not headers["content-length"].isdigit()
                     or int(headers["content-length"]) != len(response.body)))
            ):
                raise OidcTokenExchangeReviewError()
            data = json.loads(
                response.body.decode("utf-8"), object_pairs_hook=_reject_duplicates,
                parse_constant=lambda _x: (_ for _ in ()).throw(
                    OidcTokenExchangeReviewError()
                ),
            )
            if type(data) is not dict or set(data) != {
                "access_token", "token_type", "expires_in", "id_token"
            }:
                raise OidcTokenExchangeReviewError()
            if (
                type(data["access_token"]) is not str
                or not _ACCESS_TOKEN.fullmatch(data["access_token"])
                or data["token_type"] != "Bearer"
                or type(data["expires_in"]) is not int
                or not 1 <= data["expires_in"] <= 3600
                or type(data["id_token"]) is not str
                or len(data["id_token"]) > 8192
                or not _JWT.fullmatch(data["id_token"])
                or os.environ.get(_FLAG) != "true"
            ):
                raise OidcTokenExchangeReviewError()
            # Access/refresh tokens MUST NOT leave this boundary. The returned
            # ID token is still UNTRUSTED until #148 verifies its RSA signature.
            return data["id_token"]
        except Exception:
            raise OidcTokenExchangeReviewError() from None
