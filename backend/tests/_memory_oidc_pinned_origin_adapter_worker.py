"""Synthetic TLS-offload compatibility through an unprivileged ASGI bridge.

Real isolated OIDC Starlette review, encrypted state and mock RSA id_token;
no network. The *proof* is a controlled fixture only and NOT Render evidence.
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
from uvicorn import Config

from backend.memory_oidc_browser_callback_review import OidcBrowserCallbackOfflineReview
from backend.memory_oidc_isolated_asgi_review import create_isolated_oidc_asgi_review
from backend.memory_oidc_pinned_origin_adapter_offline import (
    PinnedOriginOfflineReviewError, SyntheticPinnedOriginAsgi,
)
from backend.tests._support import NoNetworkMixin
from scripts import render_start

PUBLIC = "https://app.example.test"
ISSUER = "https://login.example.test"
START = "/__offline_review__/oidc/start"
CALLBACK = "/callback/oidc"
DONE = "/__offline_review__/oidc/done"
FLAG = "MEMORY_OIDC_PINNED_ORIGIN_REHEARSAL_ENABLED"
HEADERS = {
    "origin": PUBLIC, "sec-fetch-site": "same-origin",
    "content-type": "application/json",
}


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class PinnedOriginAsgiTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public = cls.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def setUp(self):
        super().setUp()
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.db = Path(t.name) / "pinned-origin.sqlite"
        patch = mock.patch.dict(os.environ, {
            FLAG: "true",
            "MEMORY_OIDC_ISOLATED_ASGI_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_BROWSER_CALLBACK_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_CODE_PKCE_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_ID_TOKEN_VERIFIER_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_HTTPS_SENDER_REVIEW_ENABLED": "false",
            "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED": "false",
            "OPENAI_API_KEY": "",
        })
        patch.start()
        self.addCleanup(patch.stop)
        self.evidence = [True]  # synthetic fixture, NEVER an edge proof
        self.calls = []
        self.nonce = None
        self.review = OidcBrowserCallbackOfflineReview(
            path=str(self.db), issuer=ISSUER,
            client_id="synthetic-pinned-external-origin",
            authorization_endpoint=ISSUER + "/authorize",
            token_endpoint=ISSUER + "/token",
            redirect_uri=PUBLIC + CALLBACK,
            cookie_hmac_key=b"K" * 48,
            state_encryption_key=b"Q" * 32,
            pinned_public_keys={"offline-kid": self.public},
            token_exchanger=self.exchange,
            clock=lambda: 100000.0,
        )
        self.inner = create_isolated_oidc_asgi_review(self.review)
        self.adapter = self.make_adapter()

    def make_adapter(self, **changes):
        data = dict(
            isolated_app=self.inner, review=self.review,
            public_origin=PUBLIC,
            expected_internal_server=("app.example.test", None),
            synthetic_proof_provider=lambda: self.evidence[0],
        )
        data.update(changes)
        return SyntheticPinnedOriginAsgi(**data)

    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.adapter),
            base_url="http://app.example.test", follow_redirects=False,
        )
        await self.client.__aenter__()

    async def asyncTearDown(self):
        await self.client.__aexit__(None, None, None)

    def exchange(self, code, verifier, redirect, endpoint):
        self.calls.append((code, verifier, redirect, endpoint))
        self.assertIsNotNone(self.nonce)
        hdr = {"alg": "RS256", "kid": "offline-kid", "typ": "JWT"}
        claims = {
            "iss": ISSUER, "aud": "synthetic-pinned-external-origin",
            "sub": "synthetic-subject", "nonce": self.nonce,
            "iat": 99980, "exp": 100180, "auth_time": 99950,
        }
        signing = ".".join(b64(json.dumps(x, separators=(",", ":")).encode())
                           for x in (hdr, claims))
        signature = self.key.sign(
            signing.encode(), padding.PKCS1v15(), hashes.SHA256(),
        )
        return signing + "." + b64(signature)

    async def start(self, headers=None, client=None, path=START):
        result = await (client or self.client).post(
            path, content=b"{}", headers=HEADERS if headers is None else headers,
        )
        params = None
        if result.status_code == 303:
            params = parse_qs(urlsplit(result.headers["location"]).query)
            self.nonce = params["nonce"][0]
        return result, params

    async def callback(self, params, *, headers=None, client=None):
        q = urlencode({
            "code": "FAKE-CODE", "state": params["state"][0], "iss": ISSUER,
        })
        if headers is None:
            headers = {
                "cookie": self.prelogin,
                "sec-fetch-site": "cross-site",
                "sec-fetch-mode": "navigate",
                "sec-fetch-dest": "document",
            }
        return await (client or self.client).get(CALLBACK + "?" + q, headers=headers)

    async def ready(self):
        start, params = await self.start()
        self.assertEqual(start.status_code, 303)
        self.prelogin = start.headers["set-cookie"].split(";", 1)[0]
        return params

    def assert_denied(self, result, status=400):
        self.assertEqual(result.status_code, status)
        self.assertNotIn("set-cookie", result.headers)
        self.assertEqual(self.calls, [])

    async def test_off_by_default_before_proof_is_called(self):
        os.environ.pop(FLAG)
        self.evidence[0] = True
        r, _ = await self.start()
        self.assert_denied(r, status=404)
        self.assertFalse(self.db.exists())

    async def test_disabling_independent_review_flag_refuses(self):
        os.environ["MEMORY_OIDC_ISOLATED_ASGI_REVIEW_ENABLED"] = "false"
        r, _ = await self.start()
        self.assert_denied(r, status=404)

    async def test_real_network_approval_not_a_bypass(self):
        os.environ["MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED"] = "true"
        r, _ = await self.start()
        self.assert_denied(r, status=404)

    async def test_false_synthetic_proof_rejects_internal_http(self):
        self.evidence[0] = False
        r, _ = await self.start()
        self.assert_denied(r)
        self.assertFalse(self.db.exists())

    async def test_truthy_integer_proof_is_not_attestation(self):
        self.evidence[0] = 1
        r, _ = await self.start()
        self.assert_denied(r)

    async def test_missing_proof_rejects_internal_http(self):
        self.evidence[0] = None
        r, _ = await self.start()
        self.assert_denied(r)

    async def test_proof_revoked_during_second_check_refuses(self):
        counter = [0]
        def proof():
            counter[0] += 1
            return counter[0] == 1
        adapter = self.make_adapter(synthetic_proof_provider=proof)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=adapter),
            base_url="http://app.example.test",
        ) as client:
            r, _ = await self.start(client=client)
        self.assert_denied(r)

    async def test_pinned_origin_accepts_http_asgi_only_in_synthetic_fixture(self):
        r, q = await self.start()
        self.assertEqual(r.status_code, 303)
        self.assertEqual(q["redirect_uri"], [PUBLIC + CALLBACK])
        loc = urlsplit(r.headers["location"])
        self.assertEqual((loc.scheme, loc.netloc, loc.path),
                         ("https", "login.example.test", "/authorize"))
        self.assertEqual(self.calls, [])
        self.assertFalse(hasattr(self.adapter, "issue_browser_session"))
        self.assertFalse(hasattr(self.adapter, "authorize_memory_write"))

    async def test_secure_host_only_cookie_survives_synthetic_http_upstream(self):
        r, _ = await self.start()
        cookie = r.headers["set-cookie"]
        for field in ("__Host-gt-oidc-review=", "Secure", "HttpOnly",
                      "Path=/", "SameSite=Lax", "Max-Age=180"):
            self.assertIn(field, cookie)
        self.assertNotIn("Domain=", cookie)
        self.assertNotIn("synthetic-subject", str(r.headers))

    async def test_synthetic_callback_consumes_state_without_granting_login(self):
        q = await self.ready()
        r = await self.callback(q)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], DONE)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][2], PUBLIC + CALLBACK)
        self.assertIn("Max-Age=0", r.headers["set-cookie"])
        self.assertNotIn("FAKE-CODE", str(r.headers))
        self.assertFalse(hasattr(self.adapter, "remember"))

    async def test_callback_without_synthetic_proof_cannot_exchange(self):
        q = await self.ready()
        self.evidence[0] = False
        r = await self.callback(q)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.calls, [])

    async def test_wrong_host_is_not_pinned_origin(self):
        r, _ = await self.start(headers={**HEADERS, "host": "evil.example.test"})
        self.assert_denied(r)

    async def test_duplicate_identical_host_is_rejected(self):
        headers = list(HEADERS.items()) + [
            ("host", "app.example.test"), ("host", "app.example.test"),
        ]
        r, _ = await self.start(headers=headers)
        self.assert_denied(r)

    async def test_mismatched_origin_cannot_request_prelogin_cookie(self):
        for origin in ("http://app.example.test", "https://evil.example.test",
                       PUBLIC + ":443", "null"):
            with self.subTest(origin=origin):
                r, _ = await self.start(headers={**HEADERS, "origin": origin})
                self.assert_denied(r)

    async def test_duplicate_origin_cannot_start(self):
        h = list(HEADERS.items()) + [("origin", PUBLIC)]
        r, _ = await self.start(headers=h)
        self.assert_denied(r)

    async def test_forwarded_and_xff_injection_cannot_assert_edge(self):
        for key, value in (
            ("forwarded", "for=8.8.8.8;proto=https;host=app.example.test"),
            ("x-forwarded-proto", "https"),
            ("x-forwarded-host", "app.example.test"),
            ("x-forwarded-for", "8.8.8.8, 1.1.1.1"),
            ("x-real-ip", "127.0.0.1"),
            ("cf-connecting-ip", "8.8.8.8"),
            ("cf-ray", "fake-ray"),
            ("rndr-id", "fake-request"),
        ):
            with self.subTest(header=key):
                r, _ = await self.start(headers={**HEADERS, key: value})
                self.assert_denied(r)

    async def test_fake_identity_and_authorization_headers_rejected(self):
        for key in ("authorization", "x-user-id", "x-principal-id",
                    "x-memory-identity", "proxy-authorization"):
            with self.subTest(header=key):
                r, _ = await self.start(headers={**HEADERS, key: "admin"})
                self.assert_denied(r)

    async def test_internal_https_scope_is_not_accepted_as_http_upstream(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.adapter),
            base_url=PUBLIC,
        ) as client:
            r, _ = await self.start(client=client)
        self.assert_denied(r)

    async def test_other_internal_host_and_server_denied(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.adapter),
            base_url="http://evil.example.test",
        ) as client:
            r, _ = await self.start(client=client)
        self.assert_denied(r)

    async def test_caller_controlled_return_url_remains_rejected(self):
        r, _ = await self.start(path=START + "?next=https://evil.example.test")
        self.assert_denied(r)

    async def test_real_uvicorn_proxy_headers_disabled_allows_synthetic_flow(self):
        cfg = Config(
            self.adapter, interface="asgi3", log_config=None, lifespan="off",
            proxy_headers=False, forwarded_allow_ips="*",
        )
        cfg.load()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=cfg.loaded_app),
            base_url="http://app.example.test",
        ) as client:
            r, q = await self.start(client=client)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(q["redirect_uri"], [PUBLIC + CALLBACK])
        self.assertEqual(self.calls, [])

    async def test_real_uvicorn_enabled_spoofed_xfp_still_rejected(self):
        cfg = Config(
            self.adapter, interface="asgi3", log_config=None, lifespan="off",
            proxy_headers=True, forwarded_allow_ips="*",
        )
        cfg.load()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=cfg.loaded_app),
            base_url="http://app.example.test",
        ) as client:
            r, _ = await self.start(
                headers={**HEADERS, "x-forwarded-proto": "https"},
                client=client,
            )
        self.assert_denied(r)

    async def test_raw_original_scope_not_changed_by_adapter(self):
        scope = {
            "type": "http", "scheme": "http", "method": "POST",
            "path": START, "root_path": "", "query_string": b"",
            "server": ("app.example.test", None),
            "client": ("203.0.113.100", 1234),
            "headers": [
                (b"host", b"app.example.test"), (b"origin", PUBLIC.encode()),
                (b"sec-fetch-site", b"same-origin"),
                (b"content-type", b"application/json"),
            ],
        }
        before = dict(scope)
        before["headers"] = list(scope["headers"])
        events = []
        sent = [False]
        async def receive():
            if not sent[0]:
                sent[0] = True
                return {"type": "http.request", "body": b"{}", "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(event):
            events.append(event)
        await self.adapter(scope, receive, send)
        self.assertEqual(scope, before)
        self.assertEqual(scope["scheme"], "http")
        self.assertTrue(any(x.get("status") == 303 for x in events))

    def test_constructor_refuses_wrong_origin_or_wrong_callback(self):
        for origin in ("http://app.example.test", "https://evil.test",
                       PUBLIC + "/", PUBLIC + ":443",
                       "https://app.example.test@evil.test"):
            with self.subTest(origin=origin):
                with self.assertRaises(PinnedOriginOfflineReviewError):
                    self.make_adapter(public_origin=origin)

    def test_constructor_requires_explicit_separate_proof_callback(self):
        with self.assertRaises(PinnedOriginOfflineReviewError):
            self.make_adapter(synthetic_proof_provider=None)

    def test_render_supervisor_refuses_adapter_activation(self):
        with self.assertRaisesRegex(
            Exception, "unreviewed_memory_browser_identity_ingress",
        ):
            render_start.reject_unreviewed_browser_identity_ingress({
                FLAG: "true",
            })

    def test_production_entries_do_not_mount_adapter(self):
        root = Path(__file__).resolve().parents[2]
        for file in (
            root / "backend" / "app.py",
            root / "backend" / "p3_relay_app.py",
            root / "scripts" / "render_start_p3.py",
        ):
            self.assertNotIn(
                "memory_oidc_pinned_origin_adapter_offline",
                file.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
