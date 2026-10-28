"""Real isolated OIDC ASGI Host/Origin/cookie/redirect boundary tests (OFF).

No sockets, Render calls, real IdP, real login or memory writes. This
intentionally exercises Starlette's real HTTP response and callback paths.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from backend.memory_oidc_browser_callback_review import OidcBrowserCallbackOfflineReview
from backend.memory_oidc_isolated_asgi_review import create_isolated_oidc_asgi_review
from backend.tests._support import NoNetworkMixin

APP = "https://app.example.test"
IDP = "https://login.example.test"
START = "/__offline_review__/oidc/start"
CALLBACK = "/callback/oidc"
DONE = "/__offline_review__/oidc/done"
COOKIE = "__Host-gt-oidc-review"
BODY = b"{}"


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class HostOriginCookieAsgiTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public = cls.key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db_path = Path(folder.name) / "oidc-boundary.sqlite"
        p = mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "",
            "MEMORY_OIDC_ISOLATED_ASGI_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_BROWSER_CALLBACK_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_CODE_PKCE_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_ID_TOKEN_VERIFIER_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_HTTPS_SENDER_REVIEW_ENABLED": "false",
            "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED": "false",
        })
        p.start()
        self.addCleanup(p.stop)
        self.clock = [100000.0]
        self.nonce = None
        self.exchanges = []
        self.review = OidcBrowserCallbackOfflineReview(
            path=str(self.db_path), issuer=IDP,
            client_id="synthetic-host-origin-review",
            authorization_endpoint=IDP + "/authorize",
            token_endpoint=IDP + "/token",
            redirect_uri=APP + CALLBACK,
            cookie_hmac_key=b"H" * 48,
            state_encryption_key=b"A" * 32,
            pinned_public_keys={"synthetic-kid": self.public},
            token_exchanger=self.exchange,
            clock=lambda: self.clock[0],
        )
        self.app = create_isolated_oidc_asgi_review(self.review)

    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url=APP, follow_redirects=False,
        )
        await self.client.__aenter__()

    async def asyncTearDown(self):
        await self.client.__aexit__(None, None, None)

    def exchange(self, code, verifier, redirect, endpoint):
        self.exchanges.append((code, verifier, redirect, endpoint))
        assert self.nonce is not None
        header = {"alg": "RS256", "kid": "synthetic-kid", "typ": "JWT"}
        claims = {
            "iss": IDP, "aud": "synthetic-host-origin-review",
            "sub": "synthetic-private-subject", "nonce": self.nonce,
            "iat": 99980, "exp": 100180, "auth_time": 99950,
        }
        data = ".".join(_b64(json.dumps(v, separators=(",", ":")).encode())
                        for v in (header, claims))
        sig = self.key.sign(data.encode(), padding.PKCS1v15(), hashes.SHA256())
        return data + "." + _b64(sig)

    @staticmethod
    def start_headers():
        return {
            "origin": APP,
            "sec-fetch-site": "same-origin",
            "content-type": "application/json",
        }

    async def start(self, *, headers=None, path=START):
        r = await self.client.post(
            path, headers=self.start_headers() if headers is None else headers,
            content=BODY,
        )
        fields = None
        if r.status_code == 303:
            fields = parse_qs(urlsplit(r.headers["location"]).query)
            self.nonce = fields["nonce"][0]
        return r, fields

    async def callback(self, fields, *, headers=None, suffix="", scheme=APP):
        q = urlencode({
            "state": fields["state"][0],
            "code": "SYNTHETIC-CODE-NOT-A-SECRET",
            "iss": IDP,
        })
        h = {
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
        } if headers is None else headers
        return await self.client.get(
            scheme + CALLBACK + "?" + q + suffix,
            headers=h,
        )

    def assert_no_cookie(self, response):
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.exchanges, [])

    def assert_terminal(self, response):
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], DONE)
        cookie = response.headers.get("set-cookie", "")
        self.assertIn(COOKIE + "=;", cookie)
        for expected in ("Path=/", "Max-Age=0", "Secure", "HttpOnly", "SameSite=Lax"):
            self.assertIn(expected, cookie)
        self.assertNotIn("Domain=", cookie)
        self.assertNotIn("synthetic-private-subject", str(response.headers))
        self.assertNotIn("SYNTHETIC-CODE-NOT-A-SECRET", str(response.headers))
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    async def test_start_redirect_is_pinned_and_not_user_selected(self):
        r, params = await self.start(headers={
            **self.start_headers(), "x-return-to": "https://evil.test/next",
        })
        self.assertEqual(r.status_code, 303)
        parsed = urlsplit(r.headers["location"])
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path),
                         ("https", "login.example.test", "/authorize"))
        self.assertEqual(params["redirect_uri"], [APP + CALLBACK])
        self.assertEqual(params["client_id"], ["synthetic-host-origin-review"])
        self.assertNotIn("evil.test", str(r.headers))
        self.assertEqual(self.exchanges, [])
        self.assertFalse(hasattr(self.app, "issue_browser_session"))

    async def test_host_only_cookie_exact_security_attributes(self):
        r, _ = await self.start()
        self.assertEqual(r.status_code, 303)
        cookie = r.headers["set-cookie"]
        self.assertTrue(cookie.startswith(COOKIE + "="))
        for field in ("Path=/", "Max-Age=180", "Secure", "HttpOnly", "SameSite=Lax"):
            self.assertIn(field, cookie)
        self.assertNotIn("Domain=", cookie)
        self.assertNotIn("SameSite=None", cookie)
        self.assertNotIn("Authorization", str(r.headers))
        self.assertEqual(r.headers["referrer-policy"], "no-referrer")
        self.assertIn("no-store", r.headers["cache-control"])

    async def test_start_rejects_host_authority_variants(self):
        for value in ("evil.test", "app.example.test:444", "APP.EXAMPLE.TEST",
                      "app.example.test.", "127.0.0.1",
                      "app.example.test@evil.test"):
            with self.subTest(host=value):
                r, _ = await self.start(headers={
                    **self.start_headers(), "host": value,
                })
                self.assertEqual(r.status_code, 400)
                self.assert_no_cookie(r)

    async def test_start_rejects_duplicate_host_even_identical(self):
        hdr = list(self.start_headers().items()) + [
            ("host", "app.example.test"), ("host", "app.example.test"),
        ]
        r, _ = await self.start(headers=hdr)
        self.assertEqual(r.status_code, 400)
        self.assert_no_cookie(r)

    async def test_start_rejects_missing_or_altered_origin(self):
        for origin in (None, "null", "http://app.example.test",
                       "https://evil.test", APP + "/", APP + ":443",
                       "https://app.example.test.evil.test"):
            with self.subTest(origin=origin):
                hdr = self.start_headers()
                if origin is None:
                    del hdr["origin"]
                else:
                    hdr["origin"] = origin
                r, _ = await self.start(headers=hdr)
                self.assertEqual(r.status_code, 400)
                self.assert_no_cookie(r)

    async def test_start_rejects_duplicate_origin_even_identical(self):
        hdr = list(self.start_headers().items()) + [("origin", APP)]
        r, _ = await self.start(headers=hdr)
        self.assertEqual(r.status_code, 400)
        self.assert_no_cookie(r)

    async def test_start_rejects_cross_site_fetch_metadata(self):
        for site in ("cross-site", "same-site", "none", "Same-Origin", ""):
            with self.subTest(site=site):
                r, _ = await self.start(headers={
                    **self.start_headers(), "sec-fetch-site": site,
                })
                self.assertEqual(r.status_code, 400)
                self.assert_no_cookie(r)

    async def test_start_rejects_forwarded_authority_and_identity(self):
        for key, value in (
            ("forwarded", "for=8.8.8.8;proto=https;host=app.example.test"),
            ("x-forwarded-host", "app.example.test"),
            ("x-forwarded-proto", "https"),
            ("x-original-url", START),
            ("x-rewrite-url", START),
            ("x-principal-id", "synthetic-admin"),
            ("x-user-id", "synthetic-admin"),
            ("x-memory-identity", "synthetic-logged-in"),
        ):
            with self.subTest(key=key):
                r, _ = await self.start(headers={
                    **self.start_headers(), key: value,
                })
                self.assertEqual(r.status_code, 400)
                self.assert_no_cookie(r)

    async def test_start_rejects_caller_selected_return_query(self):
        r, _ = await self.start(path=START + "?next=https%3A%2F%2Fevil.test")
        self.assertEqual(r.status_code, 400)
        self.assert_no_cookie(r)

    async def test_start_rejects_cookie_and_duplicate_content_type(self):
        r, _ = await self.start(headers={
            **self.start_headers(), "cookie": COOKIE + "=forged",
        })
        self.assertEqual(r.status_code, 400)
        self.assert_no_cookie(r)
        dup = list(self.start_headers().items()) + [
            ("content-type", "application/json"),
        ]
        r, _ = await self.start(headers=dup)
        self.assertEqual(r.status_code, 400)
        self.assert_no_cookie(r)

    async def test_cross_site_top_level_callback_can_complete_without_origin(self):
        started, fields = await self.start()
        self.assertEqual(started.status_code, 303)
        r = await self.callback(fields)
        self.assert_terminal(r)
        self.assertEqual(len(self.exchanges), 1)
        self.assertEqual(self.exchanges[0][2], APP + CALLBACK)
        self.assertNotIn(COOKIE, self.client.cookies)
        terminal = await self.client.get(DONE)
        self.assertEqual(terminal.status_code, 200)
        self.assertIn("No user login or memory write", terminal.text)

    async def test_callback_wrong_host_clears_cookie_without_token_exchange(self):
        _, fields = await self.start()
        r = await self.callback(fields, headers={
            "host": "evil.test", "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "navigate", "sec-fetch-dest": "document",
        })
        self.assert_terminal(r)
        self.assertEqual(self.exchanges, [])
        self.assertNotIn(COOKIE, self.client.cookies)

    async def test_callback_rejects_origin_even_when_claimed_same_origin(self):
        _, fields = await self.start()
        r = await self.callback(fields, headers={
            "origin": APP,
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
        })
        self.assert_terminal(r)
        self.assertEqual(self.exchanges, [])

    async def test_callback_rejects_proxy_authority_without_exchange(self):
        for name, value in (
            ("x-forwarded-host", "app.example.test"),
            ("x-forwarded-proto", "https"),
            ("forwarded", "for=8.8.8.8;proto=https;host=app.example.test"),
            ("x-principal-id", "synthetic-admin"),
        ):
            with self.subTest(name=name):
                _, fields = await self.start()
                r = await self.callback(fields, headers={
                    "sec-fetch-site": "cross-site",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-dest": "document",
                    name: value,
                })
                self.assert_terminal(r)
                self.assertEqual(self.exchanges, [])

    async def test_callback_rejects_duplicate_prelogin_cookie(self):
        first, fields = await self.start()
        cookie = first.headers["set-cookie"].split(";", 1)[0]
        r = await self.callback(fields, headers={
            "cookie": cookie + "; " + cookie,
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
        })
        self.assert_terminal(r)
        self.assertEqual(self.exchanges, [])

    async def test_callback_rejects_unexpected_next_parameter(self):
        _, fields = await self.start()
        r = await self.callback(
            fields, suffix="&next=https%3A%2F%2Fevil.test",
        )
        self.assert_terminal(r)
        self.assertNotIn("evil.test", str(r.headers))
        self.assertEqual(self.exchanges, [])

    async def test_callback_http_scheme_never_exchanges_token(self):
        _, fields = await self.start()
        r = await self.callback(fields, scheme="http://app.example.test")
        self.assert_terminal(r)
        self.assertEqual(self.exchanges, [])

    async def test_callback_rejects_duplicate_host(self):
        _, fields = await self.start()
        r = await self.callback(fields, headers=[
            ("host", "app.example.test"),
            ("host", "app.example.test"),
            ("sec-fetch-site", "cross-site"),
            ("sec-fetch-mode", "navigate"),
            ("sec-fetch-dest", "document"),
        ])
        self.assert_terminal(r)
        self.assertEqual(self.exchanges, [])

    async def test_terminal_done_never_reflects_query_or_creates_cookie(self):
        r = await self.client.get(DONE + "?code=SECRET&state=secret&next=evil")
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("set-cookie", r.headers)
        self.assertNotIn("SECRET", r.text)
        r = await self.client.get(DONE)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("set-cookie", r.headers)
        self.assertEqual(self.exchanges, [])

    async def test_review_flags_default_off_and_no_grant(self):
        os.environ["MEMORY_OIDC_ISOLATED_ASGI_REVIEW_ENABLED"] = "false"
        r, _ = await self.start()
        self.assertEqual(r.status_code, 404)
        self.assert_no_cookie(r)
        self.assertFalse(hasattr(self.app, "remember"))
        self.assertFalse(hasattr(self.app, "authorize_memory_write"))
        self.assertEqual(len(self.app.routes), 3)


if __name__ == "__main__":
    unittest.main()
