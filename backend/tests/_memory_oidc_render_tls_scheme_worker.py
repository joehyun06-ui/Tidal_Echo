"""Offline Render HTTPS-edge -> HTTP-ASGI OIDC compatibility proof.

Uses real isolated Starlette OIDC review, HTTPX ASGITransport and actual
Uvicorn Config-loaded app stack. No listener, IdP traffic, login, deployment,
secrets, network access or memory writes. This intentionally proves a BLOCKER:
legitimate browser HTTPS to Render maps to internal HTTP with --no-proxy-headers,
which the current isolated reviewer denies.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from uvicorn import Config

from backend.memory_oidc_browser_callback_review import OidcBrowserCallbackOfflineReview
from backend.memory_oidc_isolated_asgi_review import create_isolated_oidc_asgi_review
from backend.tests._support import NoNetworkMixin
from scripts import render_start, render_start_p3

PUBLIC = "https://app.example.test"
ISSUER = "https://login.example.test"
START = "/__offline_review__/oidc/start"
CALLBACK = "/callback/oidc"
DONE = "/__offline_review__/oidc/done"
COOKIE = "__Host-gt-oidc-review"
FLAGS = {
    "OPENAI_API_KEY": "",
    "MEMORY_OIDC_ISOLATED_ASGI_REVIEW_ENABLED": "true",
    "MEMORY_OIDC_BROWSER_CALLBACK_REVIEW_ENABLED": "true",
    "MEMORY_OIDC_CODE_PKCE_REVIEW_ENABLED": "true",
    "MEMORY_OIDC_ID_TOKEN_VERIFIER_REVIEW_ENABLED": "true",
    "MEMORY_OIDC_HTTPS_SENDER_REVIEW_ENABLED": "false",
    "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED": "false",
    "MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED": "false",
    "MEMORY_OIDC_RENDER_DRIFT_REVIEW_ENABLED": "false",
    "CODEX_GENERATION_ENABLED": "false",
    "CODEX_CANARY_ENTRYPOINTS_ENABLED": "false",
}
HEADERS = {
    "origin": PUBLIC,
    "sec-fetch-site": "same-origin",
    "content-type": "application/json",
}


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class RenderTlsSchemeCompatibilityTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public = cls.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "tls-scheme-state.sqlite"
        patch = mock.patch.dict(os.environ, FLAGS)
        patch.start()
        self.addCleanup(patch.stop)
        self.exchanges = []
        self.browser_nonce = None
        self.review = OidcBrowserCallbackOfflineReview(
            path=str(self.db), issuer=ISSUER,
            client_id="synthetic-tls-edge-client",
            authorization_endpoint=ISSUER + "/authorize",
            token_endpoint=ISSUER + "/token",
            redirect_uri=PUBLIC + CALLBACK,
            cookie_hmac_key=b"H" * 48,
            state_encryption_key=b"A" * 32,
            pinned_public_keys={"fixture-kid": self.public},
            token_exchanger=self._exchange,
            clock=lambda: 100000.0,
        )
        self.app = create_isolated_oidc_asgi_review(self.review)

    async def asyncSetUp(self):
        self.http_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://app.example.test", follow_redirects=False,
        )
        self.https_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url=PUBLIC, follow_redirects=False,
        )
        await self.http_client.__aenter__()
        await self.https_client.__aenter__()

    async def asyncTearDown(self):
        await self.http_client.__aexit__(None, None, None)
        await self.https_client.__aexit__(None, None, None)

    def _exchange(self, code, verifier, redirect, endpoint):
        self.exchanges.append((code, verifier, redirect, endpoint))
        self.assertIsNotNone(self.browser_nonce)
        head = {"alg": "RS256", "kid": "fixture-kid", "typ": "JWT"}
        claims = {
            "iss": ISSUER, "aud": "synthetic-tls-edge-client",
            "sub": "synthetic-opaque-subject", "nonce": self.browser_nonce,
            "iat": 99980, "exp": 100180, "auth_time": 99950,
        }
        payload = ".".join(_b64(json.dumps(x, separators=(",", ":")).encode())
                           for x in (head, claims))
        signature = self.key.sign(
            payload.encode(), padding.PKCS1v15(), hashes.SHA256(),
        )
        return payload + "." + _b64(signature)

    async def _start(self, client=None, *, headers=None):
        return await (client or self.http_client).post(
            START, headers=HEADERS if headers is None else headers,
            content=b"{}",
        )

    async def _https_positive_start(self):
        response = await self._start(self.https_client)
        self.assertEqual(response.status_code, 303)
        query = parse_qs(urlsplit(response.headers["location"]).query)
        self.browser_nonce = query["nonce"][0]
        self.assertEqual(query["redirect_uri"], [PUBLIC + CALLBACK])
        return response, query

    @staticmethod
    def _callback_query(fields):
        return urlencode({
            "state": fields["state"][0],
            "code": "SYNTHETIC-CODE-NOT-REAL",
            "iss": ISSUER,
        })

    def _assert_no_start_grant(self, result):
        self.assertEqual(result.status_code, 400)
        self.assertNotIn("set-cookie", result.headers)
        self.assertEqual(self.exchanges, [])
        self.assertFalse(self.db.exists())

    async def test_https_to_internal_http_without_proxy_rewrite_blocks_start(self):
        r = await self._start()
        self._assert_no_start_grant(r)

    async def test_browser_https_origin_does_not_change_internal_asgi_scheme(self):
        r = await self._start(headers={**HEADERS, "referer": PUBLIC + "/"})
        self._assert_no_start_grant(r)

    async def test_spoofed_forwarded_proto_cannot_fix_internal_scheme(self):
        r = await self._start(headers={
            **HEADERS, "x-forwarded-proto": "https",
        })
        self._assert_no_start_grant(r)

    async def test_rfc_forwarded_proto_cannot_fix_internal_scheme(self):
        r = await self._start(headers={
            **HEADERS, "forwarded": "for=8.8.8.8;proto=https;host=app.example.test",
        })
        self._assert_no_start_grant(r)

    async def test_cf_identity_headers_cannot_override_internal_scheme(self):
        r = await self._start(headers={
            **HEADERS, "cf-connecting-ip": "8.8.8.8", "cf-ray": "synthetic-ray",
        })
        self._assert_no_start_grant(r)

    async def test_multihop_xff_cannot_override_internal_scheme(self):
        r = await self._start(headers={
            **HEADERS, "x-forwarded-for": "8.8.8.8, 1.1.1.1",
            "x-forwarded-proto": "https",
        })
        self._assert_no_start_grant(r)

    async def test_https_internal_asgi_positive_control_issues_only_prelogin(self):
        response, _ = await self._https_positive_start()
        self.assertIn("Secure", response.headers["set-cookie"])
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=Lax", response.headers["set-cookie"])
        self.assertNotIn("Domain=", response.headers["set-cookie"])
        self.assertEqual(self.exchanges, [])
        self.assertFalse(hasattr(self.app, "issue_browser_session"))
        self.assertFalse(hasattr(self.app, "remember"))

    async def test_http_callback_with_valid_cookie_does_not_exchange_code(self):
        first, params = await self._https_positive_start()
        pair = first.headers["set-cookie"].split(";", 1)[0]
        result = await self.http_client.get(
            CALLBACK + "?" + self._callback_query(params),
            headers={
                "cookie": pair, "sec-fetch-site": "cross-site",
                "sec-fetch-mode": "navigate", "sec-fetch-dest": "document",
            },
        )
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.headers["location"], DONE)
        self.assertIn("Max-Age=0", result.headers["set-cookie"])
        self.assertEqual(self.exchanges, [])
        self.assertNotIn("SYNTHETIC-CODE-NOT-REAL", str(result.headers))

    async def test_http_callback_with_forwarded_proto_stays_unprivileged(self):
        first, params = await self._https_positive_start()
        result = await self.http_client.get(
            CALLBACK + "?" + self._callback_query(params),
            headers={
                "cookie": first.headers["set-cookie"].split(";", 1)[0],
                "x-forwarded-proto": "https",
                "sec-fetch-site": "cross-site",
                "sec-fetch-mode": "navigate", "sec-fetch-dest": "document",
            },
        )
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.headers["location"], DONE)
        self.assertEqual(self.exchanges, [])

    async def test_https_callback_positive_control_is_unprivileged(self):
        _, params = await self._https_positive_start()
        result = await self.https_client.get(
            CALLBACK + "?" + self._callback_query(params),
            headers={
                "sec-fetch-site": "cross-site",
                "sec-fetch-mode": "navigate", "sec-fetch-dest": "document",
            },
        )
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.headers["location"], DONE)
        self.assertEqual(len(self.exchanges), 1)
        self.assertIn("Max-Age=0", result.headers["set-cookie"])
        self.assertFalse(hasattr(self.app, "issue_browser_session"))

    async def test_real_uvicorn_disabled_keeps_internal_http_in_actual_app(self):
        cfg = Config(
            self.app, interface="asgi3", log_config=None, lifespan="off",
            proxy_headers=False, forwarded_allow_ips="*",
        )
        cfg.load()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=cfg.loaded_app),
            base_url="http://app.example.test", follow_redirects=False,
        ) as client:
            r = await self._start(client, headers={
                **HEADERS, "x-forwarded-proto": "https",
            })
        self._assert_no_start_grant(r)

    async def test_real_uvicorn_enabled_does_not_make_spoofed_proxy_header_safe(self):
        cfg = Config(
            self.app, interface="asgi3", log_config=None, lifespan="off",
            proxy_headers=True, forwarded_allow_ips="*",
        )
        cfg.load()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=cfg.loaded_app),
            base_url="http://app.example.test", follow_redirects=False,
        ) as client:
            r = await self._start(client, headers={
                **HEADERS, "x-forwarded-proto": "https",
            })
        self._assert_no_start_grant(r)

    def test_actual_p3_launch_keeps_proxy_headers_off(self):
        config = SimpleNamespace(
            deployment=SimpleNamespace(loop_port=3020),
            relay_port=10000,
            autonomous_wake_enabled=False,
        )
        args = render_start_p3.child_commands(
            config, executable="python-review",
            environ={
                "CODEX_CANARY_ENTRYPOINTS_ENABLED": "false",
                "CODEX_GENERATION_ENABLED": "false",
                "MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED": "false",
            },
        )
        self.assertEqual(set(args), {"api_loop", "relay"})
        for cmd in args.values():
            self.assertEqual(cmd.count("--no-proxy-headers"), 1)
            self.assertNotIn("--proxy-headers", cmd)
        self.assertIn("--no-proxy-headers", render_start.child_commands(
            config, executable="python-review",
        )["relay"])

    async def test_default_off_still_hides_oidc_review_routes(self):
        os.environ["MEMORY_OIDC_ISOLATED_ASGI_REVIEW_ENABLED"] = "false"
        r = await self._start()
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("set-cookie", r.headers)
        self.assertEqual(self.exchanges, [])
        self.assertFalse(self.db.exists())

    async def test_http_terminal_route_never_issues_browser_session(self):
        r = await self.http_client.get(DONE)
        self.assertEqual(r.status_code, 200)
        self.assertIn("No user login or memory write", r.text)
        self.assertNotIn("set-cookie", r.headers)
        self.assertEqual(self.exchanges, [])


if __name__ == "__main__":
    unittest.main()
