"""Offline cryptographic verification of pinned OIDC ID tokens, default OFF.

No OAuth redirect, token exchange, HTTP route, login cookie, real IdP or memory
write is installed. The expected nonce MUST be issued/retained by an
independent server-side authorization-code + PKCE flow (not implemented).
A cryptographically valid ID token alone does NOT constitute user consent,
a revocable local session, or a write capability.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_FLAG = "MEMORY_OIDC_ID_TOKEN_VERIFIER_REVIEW_ENABLED"
_B64 = re.compile(r"[A-Za-z0-9_-]+\Z", re.ASCII)
_KID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z", re.ASCII)
_NONCE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z", re.ASCII)
_SUB = re.compile(r"[\x21-\x7e]{1,128}\Z", re.ASCII)
_AUDIENCE = re.compile(r"[A-Za-z0-9._:-]{3,128}\Z", re.ASCII)
_MAX_AGE_SECONDS = 300
_MAX_TOKEN_TTL = 300
_CLOCK_SKEW = 30
_MAX_TOKEN_LENGTH = 8192


class OidcTokenReviewError(RuntimeError):
    def __init__(self):
        super().__init__("memory_oidc_identity_unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedOidcSubject:
    """Verified signature and claims only; NOT an authenticated local login."""
    issuer: str = field(repr=False)
    subject: str = field(repr=False)
    authenticated_at: int = field(repr=False)
    may_issue_browser_session: bool = False
    may_write_memory: bool = False

    def __repr__(self):
        return "<VerifiedOidcSubject review_only>"


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise OidcTokenReviewError()
        result[key] = value
    return result


def _b64decode(part: str, limit: int) -> bytes:
    if (
        type(part) is not str or len(part) > limit
        or not _B64.fullmatch(part) or len(part) % 4 == 1
    ):
        raise OidcTokenReviewError()
    raw = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != part:
        raise OidcTokenReviewError()
    return raw


def _object(part: str, limit: int) -> dict:
    raw = _b64decode(part, limit)
    result = json.loads(
        raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
        parse_constant=lambda _v: (_ for _ in ()).throw(OidcTokenReviewError()),
    )
    if type(result) is not dict:
        raise OidcTokenReviewError()
    return result


def _valid_issuer(value: object) -> bool:
    if type(value) is not str or not 8 <= len(value) <= 256:
        return False
    try:
        split = urlsplit(value)
        return (
            split.scheme == "https"
            and bool(split.hostname)
            and split.username is None and split.password is None
            and not split.query and not split.fragment
            and split.port in (None, 443)
            and split.hostname == split.hostname.lower()
            and not value.endswith("/")
            and value == "https://" + split.netloc + split.path
        )
    except (TypeError, ValueError):
        return False


class PinnedOidcIdTokenVerifier:
    """Review-only RS256 verification with offline pinned public PEM keys.

    Key IDs and public keys are trusted server configuration, never obtained
    from JWT 'jku', 'jwk', 'x5u' or a network call. No remote JWKS auto-fetch.
    OIDC tokens with multiple audiences are intentionally rejected by this
    conservative subset until a production client/azp policy is reviewed.
    """

    __slots__ = ("_issuer", "_audience", "_keys", "_nonce", "_clock")

    def __init__(
        self, *, expected_issuer: str, client_id: str,
        pinned_public_keys: Mapping[str, bytes],
        expected_nonce_provider: Callable[[], str] | None,
        clock: Callable[[], float] = time.time,
    ):
        if (
            not _valid_issuer(expected_issuer)
            or type(client_id) is not str or not _AUDIENCE.fullmatch(client_id)
            or not isinstance(pinned_public_keys, Mapping)
            or not 1 <= len(pinned_public_keys) <= 4
            or not callable(clock)
        ):
            raise OidcTokenReviewError()
        keys = {}
        for kid, pem in pinned_public_keys.items():
            if (
                type(kid) is not str or not _KID.fullmatch(kid)
                or type(pem) is not bytes or len(pem) > 4096
            ):
                raise OidcTokenReviewError()
            try:
                key = serialization.load_pem_public_key(pem)
                if (
                    not isinstance(key, rsa.RSAPublicKey)
                    or key.key_size not in (2048, 3072, 4096)
                    or key.public_numbers().e != 65537
                ):
                    raise OidcTokenReviewError()
                keys[kid] = key
            except Exception:
                raise OidcTokenReviewError() from None
        self._issuer = expected_issuer
        self._audience = client_id
        self._keys = keys
        self._nonce = expected_nonce_provider
        self._clock = clock

    def verify(self, id_token: str) -> VerifiedOidcSubject:
        if (
            os.environ.get(_FLAG) != "true"
            or not callable(self._nonce)
            or type(id_token) is not str
            or not 16 <= len(id_token) <= _MAX_TOKEN_LENGTH
        ):
            raise OidcTokenReviewError()
        try:
            parts = id_token.split(".")
            if len(parts) != 3:
                raise OidcTokenReviewError()
            header = _object(parts[0], 1024)
            if (
                set(header) - {"typ", "alg", "kid"}
                or header.get("alg") != "RS256"
                or header.get("typ", "JWT") != "JWT"
                or type(header.get("kid")) is not str
                or not _KID.fullmatch(header["kid"])
                or header["kid"] not in self._keys
            ):
                raise OidcTokenReviewError()
            signature = _b64decode(parts[2], 1024)
            key = self._keys[header["kid"]]
            if len(signature) != key.key_size // 8:
                raise OidcTokenReviewError()
            signing_input = (parts[0] + "." + parts[1]).encode("ascii")
            key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
            payload = _object(parts[1], 6144)
            # No 'azp'/multi-aud adaptation without a reviewed provider policy.
            expected = self._nonce()
            if (
                type(expected) is not str or not _NONCE.fullmatch(expected)
                or type(payload.get("nonce")) is not str
                or not hmac.compare_digest(expected, payload["nonce"])
                or payload.get("iss") != self._issuer
                or type(payload.get("aud")) is not str
                or not hmac.compare_digest(payload["aud"], self._audience)
                or "azp" in payload
                or type(payload.get("sub")) is not str
                or not _SUB.fullmatch(payload["sub"])
            ):
                raise OidcTokenReviewError()
            now = self._clock()
            if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
                raise OidcTokenReviewError()
            # Explicitly reject bools and floats for security-relevant NumericDates.
            iat, exp, auth = (
                payload.get("iat"), payload.get("exp"), payload.get("auth_time"),
            )
            if (
                any(type(x) is not int for x in (iat, exp, auth))
                or iat < 0 or auth < 0
                or exp <= now or iat > now + _CLOCK_SKEW
                or auth > iat + _CLOCK_SKEW
                or now - auth > _MAX_AGE_SECONDS
                or exp <= iat or exp - iat > _MAX_TOKEN_TTL
                or now - iat > _MAX_TOKEN_TTL
            ):
                raise OidcTokenReviewError()
            if "nbf" in payload and (
                type(payload["nbf"]) is not int
                or payload["nbf"] > now + _CLOCK_SKEW
                or payload["nbf"] < 0
            ):
                raise OidcTokenReviewError()
            if os.environ.get(_FLAG) != "true":
                raise OidcTokenReviewError()
            return VerifiedOidcSubject(
                issuer=self._issuer, subject=payload["sub"],
                authenticated_at=auth,
            )
        except Exception:
            raise OidcTokenReviewError() from None
