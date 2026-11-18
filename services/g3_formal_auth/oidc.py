"""GM-02AS staging relying party. The independent issuer owns login and signing keys."""

from base64 import b64encode, urlsafe_b64encode
from dataclasses import dataclass
from hashlib import sha256
from secrets import token_urlsafe
import ssl
from threading import Lock
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPSHandler
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import json

import jwt

CLIENT_ID = "gm02as-test-rp"


class InvalidLogin(ValueError):
    pass


def _private_origin(value, host):
    parsed = urlsplit(value)
    return (parsed.scheme == "https" and parsed.hostname == host and parsed.port
            and not any((parsed.username, parsed.password, parsed.path, parsed.query, parsed.fragment)))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


@dataclass(frozen=True)
class VerifiedLogin:
    issuer: str
    subject: str


class OidcLogin:
    def __init__(self, *, issuer, gateway, public_key, client_secret, ca):
        if not _private_origin(issuer, "127.0.0.2") or not _private_origin(gateway, "127.0.0.1"):
            raise InvalidLogin
        self.issuer, self.callback = issuer, gateway + "/login/callback"
        self._public_key = public_key
        self._transactions = {}
        self._lock = Lock()
        self._authorization = "Basic " + b64encode((CLIENT_ID + ":" + client_secret).encode()).decode()
        self._http = build_opener(ProxyHandler({}), _NoRedirect(),
                                  HTTPSHandler(context=ssl.create_default_context(cafile=str(ca))))

    def start(self):
        state, nonce, verifier, interaction = (token_urlsafe(32) for _ in range(4))
        challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).decode().rstrip("=")
        with self._lock:
            now = time.monotonic()
            self._transactions = {key: value for key, value in self._transactions.items() if value[-1] > now}
            if len(self._transactions) >= 64:
                raise InvalidLogin
            self._transactions[state] = (interaction, nonce, verifier, now + 120)
        params = {"client_id": CLIENT_ID, "redirect_uri": self.callback, "scope": "openid",
                  "response_type": "code", "state": state, "nonce": nonce,
                  "code_challenge": challenge, "code_challenge_method": "S256", "max_age": "0"}
        return self.issuer + "/auth?" + urlencode(params), interaction

    def complete(self, *, code, state, interaction):
        if not 1 <= len(code) <= 512 or len(state) != 43 or not interaction:
            raise InvalidLogin
        with self._lock:
            value = self._transactions.get(state)
            if value is None or value[0] != interaction:
                raise InvalidLogin
            self._transactions.pop(state)
        _, nonce, verifier, expires = value
        if expires <= time.monotonic():
            raise InvalidLogin
        form = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                "redirect_uri": self.callback}
        try:
            req = Request(self.issuer + "/token", data=urlencode(form).encode(),
                          headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": self._authorization})
            with self._http.open(req, timeout=5) as response:
                token = json.loads(response.read(32769))["id_token"]
            claims = jwt.decode(token, self._public_key, algorithms=["RS256"], audience=CLIENT_ID,
                                issuer=self.issuer, options={"strict_aud": True, "require": [
                                    "iss", "aud", "sub", "exp", "iat", "auth_time", "nonce",
                                ]})
            authentic = claims["auth_time"]
            if (claims["nonce"] != nonce or type(authentic) is not int or not 0 <= time.time() - authentic <= 120
                    or not 0 < claims["exp"] - claims["iat"] <= 180 or "azp" in claims):
                raise InvalidLogin
            return VerifiedLogin(self.issuer, claims["sub"])
        except (jwt.InvalidTokenError, OSError, KeyError, ValueError, TypeError) as error:
            raise InvalidLogin(type(error).__name__) from None
