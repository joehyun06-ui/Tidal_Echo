"""Real Chromium/WebKit cookie navigation over a strict loopback-only TLS proxy.

ONLY synthetic credentials; no external network. Browser executable downloads
are CI test dependency setup, never runtime OAuth/API traffic. The local test
TLS certificate is self-signed and browsers ignore its chain intentionally.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import html
import json
import os
import select
import socket
import socketserver
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlencode

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from playwright.async_api import async_playwright
from starlette.datastructures import Headers
from starlette.responses import HTMLResponse, PlainTextResponse, Response

from backend.memory_oidc_browser_callback_review import OidcBrowserCallbackOfflineReview
from backend.memory_oidc_isolated_asgi_review import create_isolated_oidc_asgi_review

APP_HOST = "app.example.test"
IDP_HOST = "login.identity.test"  # DIFFERENT SITE from app.example.test
ISSUER = "https://" + IDP_HOST
APP_ORIGIN = "https://" + APP_HOST
IDP_ORIGIN = "https://" + IDP_HOST
START = "/__offline_review__/oidc/start"
DONE = "/__offline_review__/oidc/done"
COOKIE_NAME = "__Host-gt-oidc-review"
_ALLOWED = frozenset({APP_HOST + ":443", IDP_HOST + ":443"})


class _LoopbackConnectHandler(socketserver.StreamRequestHandler):
    def handle(self):
        remote = None
        try:
            self.connection.settimeout(12)
            first = self.rfile.readline(4096)
            parts = first.decode("ascii").strip().split()
            if len(parts) != 3 or parts[0] != "CONNECT" or parts[1] not in _ALLOWED:
                self.server.rejected += 1
                self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                self.wfile.flush()
                return
            for _ in range(40):
                line = self.rfile.readline(4096)
                if not line:
                    return
                if line in (b"\r\n", b"\n"):
                    break
            else:
                return
            self.server.approved.append(parts[1])
            # Never resolves browser-supplied hostnames or reaches the Internet.
            remote = socket.create_connection(
                ("127.0.0.1", self.server.local_tls_port), timeout=5,
            )
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()
            self.connection.settimeout(20)
            remote.settimeout(20)
            endpoints = (self.connection, remote)
            until = time.monotonic() + 30
            while time.monotonic() < until:
                readable, _, _ = select.select(endpoints, [], [], 0.3)
                for sock in readable:
                    other = remote if sock is self.connection else self.connection
                    data = sock.recv(16384)
                    if not data:
                        return
                    other.sendall(data)
        except (OSError, ValueError):
            pass
        finally:
            if remote is not None:
                remote.close()


class _LoopbackOnlyProxy(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, local_tls_port):
        super().__init__(("127.0.0.1", 0), _LoopbackConnectHandler)
        self.local_tls_port = local_tls_port
        self.approved = []
        self.rejected = 0


class _SyntheticTwoSiteApp:
    """Exact two hostnames, one isolated ASGI app, synthetic IdP HTML only."""

    def __init__(self, browser_review, rsa_key):
        self.app = create_isolated_oidc_asgi_review(browser_review)
        self.private = rsa_key
        self.authorization_url = None
        self.callback_url = None
        self.nonces_by_code = {}
        self.code_exchanges = []
        self.override_issuer = None

    @staticmethod
    def _b64(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    def token_for(self, code, verifier, redirect, endpoint):
        self.code_exchanges.append((code, redirect, endpoint))
        nonce = self.nonces_by_code.get(code)
        if not nonce:
            raise ValueError("synthetic token exchange denied")
        claims = {
            "iss": ISSUER,
            "aud": "synthetic-browser-client",
            "sub": "browser-fixture-opaque-person",
            "nonce": nonce,
            "iat": 99980,
            "exp": 100180,
            "auth_time": 99950,
        }
        header = {"alg": "RS256", "kid": "fixture-kid", "typ": "JWT"}
        signing = ".".join(
            self._b64(json.dumps(data, separators=(",", ":")).encode("ascii"))
            for data in (header, claims)
        )
        signature = self.private.sign(
            signing.encode("ascii"), padding.PKCS1v15(), hashes.SHA256(),
        )
        return signing + "." + self._b64(signature)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host = Headers(scope=scope).get("host")
        path = scope.get("path")
        if host == APP_HOST and path == "/":
            body = (
                "<!doctype html><meta charset='utf-8'>"
                "<button id='start'>Synthetic sign-in</button>"
                "<script>"
                "document.getElementById('start').onclick=async()=>{"
                "try{await fetch('" + START + "',{method:'POST',"
                "headers:{'Content-Type':'application/json'},"
                "body:'{}',redirect:'manual'});window.__started=true}"
                "catch(e){window.__startError=String(e)}};"
                "</script>"
            )
            await HTMLResponse(body, headers={
                "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            })(scope, receive, send)
            return
        if host == IDP_HOST and path == "/authorize":
            params = parse_qs(scope.get("query_string", b"").decode("ascii"))
            try:
                if params["response_type"] != ["code"] or params["scope"] != ["openid"]:
                    raise ValueError("bad synthetic authorization")
                state = params["state"][0]
                nonce = params["nonce"][0]
                assert len(state) == 43 and len(nonce) == 43
                code = "CODE-" + state[:24]
                self.nonces_by_code[code] = nonce
                self.callback_url = (
                    APP_ORIGIN + "/callback/oidc?" + urlencode({
                        "state": state, "code": code,
                        "iss": self.override_issuer or ISSUER,
                    })
                )
                body = (
                    "<!doctype html><meta charset='utf-8'>"
                    "<p>Synthetic IdP: no actual credentials used</p>"
                    "<a id='callback' href='" +
                    html.escape(self.callback_url, quote=True) +
                    "'>Return to application</a>"
                )
                await HTMLResponse(body, headers={
                    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                })(scope, receive, send)
            except Exception:
                await Response(status_code=400)(scope, receive, send)
            return
        if host == APP_HOST:
            async def record_send(message):
                if (
                    path == START and message.get("type") == "http.response.start"
                    and message.get("status") == 303
                ):
                    hs = dict(message.get("headers", []))
                    location = hs.get(b"location")
                    if location is not None:
                        self.authorization_url = location.decode("ascii")
                await send(message)
            await self.app(scope, receive, record_send)
            return
        await Response(status_code=421)(scope, receive, send)


def _make_ephemeral_certificate(folder: Path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    distinguished = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, APP_HOST),
    ])
    current = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(distinguished).issuer_name(distinguished)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(current - datetime.timedelta(days=1))
        .not_valid_after(current + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName(APP_HOST), x509.DNSName(IDP_HOST),
        ]), critical=False)
        .sign(key, hashes.SHA256())
    )
    certfile = folder / "ephemeral-test-cert.pem"
    keyfile = folder / "ephemeral-test-key.pem"
    certfile.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    os.chmod(keyfile, 0o600)
    return certfile, keyfile


class RealBrowserCrossSiteCookieTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        patch = unittest.mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "",
            "MEMORY_OIDC_ISOLATED_ASGI_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_BROWSER_CALLBACK_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_CODE_PKCE_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_ID_TOKEN_VERIFIER_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_HTTPS_SENDER_REVIEW_ENABLED": "false",
            "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED": "false",
        })
        patch.start()
        self.addCleanup(patch.stop)
        self.clock = [100000.0]
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.review = OidcBrowserCallbackOfflineReview(
            path=str(folder / "cross-site-state.sqlite"),
            issuer=ISSUER, client_id="synthetic-browser-client",
            authorization_endpoint=IDP_ORIGIN + "/authorize",
            token_endpoint=IDP_ORIGIN + "/token",
            redirect_uri=APP_ORIGIN + "/callback/oidc",
            cookie_hmac_key=b"H" * 48, state_encryption_key=b"A" * 32,
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
        config = uvicorn.Config(
            self.site, host="127.0.0.1", port=port,
            ssl_certfile=str(certfile), ssl_keyfile=str(keyfile),
            log_level="critical", access_log=False, lifespan="off",
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
            raise RuntimeError("loopback TLS fixture did not start")
        self.proxy = _LoopbackOnlyProxy(port)
        self.proxy_thread = threading.Thread(
            target=self.proxy.serve_forever, daemon=True,
        )
        self.proxy_thread.start()
        self.proxy_address = "http://127.0.0.1:" + str(self.proxy.server_address[1])

    def _exchange(self, code, verifier, redirect, endpoint):
        return self.site.token_for(code, verifier, redirect, endpoint)

    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        engine = os.environ.get("OIDC_BROWSER_ENGINE", "chromium")
        if engine not in ("chromium", "webkit"):
            raise RuntimeError("test requires fixed browser engine")
        self.browser = await getattr(self.playwright, engine).launch(
            headless=True,
            proxy={"server": self.proxy_address, "bypass": ""},
        )
        self.context = await self.browser.new_context(
            ignore_https_errors=True, service_workers="block",
        )
        self.page = await self.context.new_page()

    async def asyncTearDown(self):
        if hasattr(self, "context"):
            await self.context.close()
        if hasattr(self, "browser"):
            await self.browser.close()
        if hasattr(self, "playwright"):
            await self.playwright.stop()

    def tearDown(self):
        if hasattr(self, "proxy"):
            self.proxy.shutdown()
            self.proxy.server_close()
            self.proxy_thread.join(timeout=5)
        if hasattr(self, "uvicorn"):
            self.uvicorn.should_exit = True
            self.server_thread.join(timeout=8)
        super().tearDown()

    async def _start(self):
        await self.page.goto(APP_ORIGIN + "/", wait_until="load")
        await self.page.locator("#start").click()
        await self.page.wait_for_function(
            "() => window.__started === true || Boolean(window.__startError)",
            timeout=10000,
        )
        error = await self.page.evaluate("window.__startError")
        self.assertIsNone(error)
        url = self.site.authorization_url
        self.assertIsNotNone(url, "server must actually send 303")
        self.assertTrue(url.startswith(IDP_ORIGIN + "/authorize?"))
        cookies = await self.context.cookies([APP_ORIGIN])
        prelogin = [c for c in cookies if c["name"] == COOKIE_NAME]
        self.assertEqual(len(prelogin), 1, cookies)
        item = prelogin[0]
        self.assertEqual(item["domain"], APP_HOST)
        self.assertTrue(item["secure"])
        self.assertTrue(item["httpOnly"])
        self.assertEqual(item["sameSite"], "Lax")
        self.assertEqual(await self.page.evaluate("document.cookie"), "")
        return url

    async def _complete(self, authorization_url):
        await self.page.goto(authorization_url, wait_until="load")
        self.assertEqual(urlsplit(self.page.url).hostname, IDP_HOST)
        await self.page.locator("#callback").click()
        await self.page.wait_for_url(APP_ORIGIN + DONE, timeout=15000)
        self.assertIn("No user login or memory write", await self.page.locator("body").inner_text())
        self.assertEqual(
            [c for c in await self.context.cookies([APP_ORIGIN])
             if c["name"] == COOKIE_NAME], [],
        )
        self.assertNotIn("state=", self.page.url)
        self.assertNotIn("code=", self.page.url)

    async def test_cross_site_lax_cookie_reaches_real_browser_callback(self):
        url = await self._start()
        await self._complete(url)
        self.assertEqual(len(self.site.code_exchanges), 1)
        self.assertIn(APP_HOST + ":443", self.proxy.approved)
        self.assertIn(IDP_HOST + ":443", self.proxy.approved)
        self.assertEqual(self.proxy.rejected, 0)

    async def test_wrong_issuer_clears_cookie_without_exchanging_code(self):
        self.site.override_issuer = "https://evil.identity.test"
        url = await self._start()
        await self._complete(url)
        self.assertEqual(len(self.site.code_exchanges), 0)

    async def test_another_browser_context_cannot_reuse_original_state(self):
        url = await self._start()
        other = await self.browser.new_context(
            ignore_https_errors=True, service_workers="block",
        )
        try:
            page2 = await other.new_page()
            await page2.goto(url)
            await page2.locator("#callback").click()
            await page2.wait_for_url(APP_ORIGIN + DONE)
            self.assertEqual(len(self.site.code_exchanges), 0)
        finally:
            await other.close()
        await self._complete(url)
        self.assertEqual(len(self.site.code_exchanges), 1)
        await self.page.goto(self.site.callback_url)
        await self.page.wait_for_url(APP_ORIGIN + DONE)
        self.assertEqual(len(self.site.code_exchanges), 1)

    async def test_expired_server_cookie_denies_even_while_browser_keeps_it(self):
        url = await self._start()
        self.clock[0] += 181
        await self._complete(url)
        self.assertEqual(len(self.site.code_exchanges), 0)


if __name__ == "__main__":
    unittest.main()
