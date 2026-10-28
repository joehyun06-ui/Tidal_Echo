"""Real Chromium/WebKit cookie flow over local TLS -> simulated HTTP edge.

Strict localhost-only CONNECT tunnel + ephemeral TLS certificate, synthetic
two-site IdP, mock issuer/RSA and the separate #166 pinned-origin adapter.
The test's proof flag is UNTRUSTED TEST FIXTURE DATA. This is never a
Render edge attestation, user login or production deployment.
"""
from __future__ import annotations

import os
import re
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import uvicorn
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.datastructures import Headers

from backend.memory_oidc_browser_callback_review import OidcBrowserCallbackOfflineReview
from backend.memory_oidc_pinned_origin_adapter_offline import SyntheticPinnedOriginAsgi
from backend.tests._memory_oidc_real_browser_worker import (
    APP_HOST, APP_ORIGIN, COOKIE_NAME, IDP_ORIGIN, ISSUER, START,
    _LoopbackOnlyProxy, _SyntheticTwoSiteApp, _make_ephemeral_certificate,
    RealBrowserCrossSiteCookieTests,
)

CALLBACK = "/callback/oidc"
DONE = "/__offline_review__/oidc/done"
FLAG = "MEMORY_OIDC_PINNED_ORIGIN_REHEARSAL_ENABLED"


class _SyntheticTlsEdgeToHttp:
    """Locally terminate real browser TLS and pass only review ASGI as HTTP."""
    def __init__(self, site, port):
        self.site = site
        self.port = port
        self.seen = []

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") == "http"
            and Headers(scope=scope).get("host") == APP_HOST
            and scope.get("path") in {
                START, CALLBACK, DONE,
            }
        ):
            self.seen.append((scope.get("scheme"), scope.get("path")))
            # This is exactly the *synthetic* TLS termination boundary.
            # It is not an ingress credential, and does not rewrite headers.
            upstream = dict(scope, scheme="http",
                            server=("127.0.0.1", self.port))
            await self.site(upstream, receive, send)
            return
        await self.site(scope, receive, send)


class PinnedOriginActualBrowserTests(unittest.IsolatedAsyncioTestCase):
    # Reuse existing hardened browser setup and navigation assertions.
    asyncSetUp = RealBrowserCrossSiteCookieTests.asyncSetUp
    asyncTearDown = RealBrowserCrossSiteCookieTests.asyncTearDown
    _start = RealBrowserCrossSiteCookieTests._start
    _complete = RealBrowserCrossSiteCookieTests._complete

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        env = mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "",
            FLAG: "true",
            "MEMORY_OIDC_ISOLATED_ASGI_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_BROWSER_CALLBACK_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_CODE_PKCE_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_ID_TOKEN_VERIFIER_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_HTTPS_SENDER_REVIEW_ENABLED": "false",
            "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED": "false",
            "MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED": "false",
        })
        env.start()
        self.addCleanup(env.stop)
        self.proof = [True]  # synthetic test switch, NO Render/TLS evidence
        self.clock = [100000.0]
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.review = OidcBrowserCallbackOfflineReview(
            path=str(folder / "browser-http-upstream-state.sqlite"),
            issuer=ISSUER, client_id="synthetic-browser-client",
            authorization_endpoint=IDP_ORIGIN + "/authorize",
            token_endpoint=IDP_ORIGIN + "/token",
            redirect_uri=APP_ORIGIN + CALLBACK,
            cookie_hmac_key=b"H" * 48,
            state_encryption_key=b"A" * 32,
            pinned_public_keys={"fixture-kid": public},
            token_exchanger=self._exchange,
            clock=lambda: self.clock[0],
        )
        self.site = _SyntheticTwoSiteApp(self.review, key)
        certfile, keyfile = _make_ephemeral_certificate(folder)

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(32)
        port = listener.getsockname()[1]

        self.site.app = SyntheticPinnedOriginAsgi(
            isolated_app=self.site.app,
            review=self.review,
            public_origin=APP_ORIGIN,
            expected_internal_server=("127.0.0.1", port),
            synthetic_proof_provider=lambda: self.proof[0],
        )
        self.edge = _SyntheticTlsEdgeToHttp(self.site, port)
        config = uvicorn.Config(
            self.edge, host="127.0.0.1", port=port,
            ssl_certfile=str(certfile), ssl_keyfile=str(keyfile),
            log_level="critical", access_log=False, lifespan="off",
            proxy_headers=False,
        )
        self.uvicorn = uvicorn.Server(config)
        self.server_thread = threading.Thread(
            target=lambda: self.uvicorn.run(sockets=[listener]), daemon=True,
        )
        self.server_thread.start()
        deadline = time.monotonic() + 10
        while not self.uvicorn.started and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.uvicorn.started:
            listener.close()
            raise RuntimeError("local synthetic TLS test server unavailable")

        self.proxy = _LoopbackOnlyProxy(port)
        self.proxy_thread = threading.Thread(
            target=self.proxy.serve_forever, daemon=True,
        )
        self.proxy_thread.start()
        self.proxy_address = "http://127.0.0.1:" + str(
            self.proxy.server_address[1]
        )

    def _exchange(self, code, verifier, redirect, endpoint):
        return self.site.token_for(code, verifier, redirect, endpoint)

    def tearDown(self):
        if hasattr(self, "proxy"):
            self.proxy.shutdown()
            self.proxy.server_close()
            self.proxy_thread.join(timeout=5)
        if hasattr(self, "uvicorn"):
            self.uvicorn.should_exit = True
            self.server_thread.join(timeout=8)
        super().tearDown()

    async def test_chromium_or_webkit_real_tls_cookie_through_http_upstream(self):
        authorization_url = await self._start()
        self.assertTrue(self.edge.seen)
        self.assertTrue(all(scheme == "https" for scheme, _ in self.edge.seen))
        await self._complete(authorization_url)
        self.assertEqual(len(self.site.code_exchanges), 1)
        self.assertEqual(self.site.code_exchanges[0][1], APP_ORIGIN + CALLBACK)
        self.assertIn(( "https", CALLBACK), self.edge.seen)
        self.assertIn(APP_HOST + ":443", self.proxy.approved)
        self.assertEqual(self.proxy.rejected, 0)
        self.assertFalse(hasattr(self.site.app, "issue_browser_session"))
        self.assertFalse(hasattr(self.site.app, "authorize_memory_write"))

    async def test_proof_missing_blocks_real_browser_cookie(self):
        self.proof[0] = False
        await self.page.goto(APP_ORIGIN + "/", wait_until="load")
        await self.page.locator("#start").click()
        await self.page.wait_for_function(
            "() => window.__started === true || Boolean(window.__startError)",
            timeout=10000,
        )
        self.assertIsNone(self.site.authorization_url)
        self.assertEqual(
            [c for c in await self.context.cookies([APP_ORIGIN])
             if c["name"] == COOKIE_NAME], [],
        )
        self.assertEqual(self.site.code_exchanges, [])

    async def test_revoked_proof_blocks_callback_code_exchange(self):
        url = await self._start()
        await self.page.goto(url, wait_until="load")
        self.assertIsNotNone(self.site.callback_url)
        self.proof[0] = False
        result = await self.page.goto(
            self.site.callback_url, wait_until="load",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.status, 400)
        self.assertEqual(len(self.site.code_exchanges), 0)

    async def test_wrong_issuer_clears_browser_cookie_without_exchange(self):
        self.site.override_issuer = "https://evil.identity.test"
        url = await self._start()
        await self._complete(url)
        self.assertEqual(len(self.site.code_exchanges), 0)

    async def test_second_browser_cannot_replay_prelogin_state(self):
        url = await self._start()
        other = await self.browser.new_context(
            ignore_https_errors=True, service_workers="block",
        )
        try:
            page2 = await other.new_page()
            await page2.goto(url, wait_until="load")
            await page2.locator("#callback").click()
            await page2.wait_for_url(APP_ORIGIN + DONE, timeout=15000)
            self.assertEqual(self.site.code_exchanges, [])
        finally:
            await other.close()
        await self._complete(url)
        self.assertEqual(len(self.site.code_exchanges), 1)
        await self.page.goto(self.site.callback_url)
        await self.page.wait_for_url(APP_ORIGIN + DONE)
        self.assertEqual(len(self.site.code_exchanges), 1)

    async def test_expired_server_cookie_refuses_browser_callback(self):
        url = await self._start()
        self.clock[0] += 181
        await self._complete(url)
        self.assertEqual(len(self.site.code_exchanges), 0)

    async def test_review_flag_revocation_never_issues_identity(self):
        url = await self._start()
        self.assertTrue(url.startswith(IDP_ORIGIN + "/authorize?"))
        os.environ[FLAG] = "false"
        await self.page.goto(url, wait_until="load")
        result = await self.page.goto(self.site.callback_url, wait_until="load")
        self.assertIsNotNone(result)
        self.assertEqual(result.status, 404)
        self.assertEqual(self.site.code_exchanges, [])
        self.assertFalse(hasattr(self.site.app, "remember"))


if __name__ == "__main__":
    unittest.main()
