"""Offline HTTPS sender hardening. Mocks prevent ANY DNS/socket/TLS traffic."""
from __future__ import annotations

import io
import json
import os
import socket
import ssl
import unittest
from dataclasses import replace
from unittest import mock

from backend import memory_oidc_https_sender_review as sender_module
from backend.memory_oidc_https_sender_review import (
    PinnedOidcHttpsSenderReview, OidcHttpsSenderReviewError,
    _PinnedHttpsConnection,
)
from backend.memory_oidc_token_exchange_review import (
    OfflineTokenRequest, PinnedOidcTokenExchangeReview,
    OidcTokenExchangeReviewError,
)
from backend.tests._support import NoNetworkMixin

URL = "https://login.example.test/token"
HOST = "login.example.test"
PUBLIC = "8.8.8.8"
BODY = b"grant_type=authorization_code&code=TEST-code-123&client_id=synthetic"
ACCESS = "Synthetic-Opaque-Access-Token-1234567890"


class FakeSocket:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


class FakeResponse:
    def __init__(self, body, *, status=200, headers=None, fail=False):
        self.status = status
        self._buf = io.BytesIO(body)
        self._fail = fail
        self._headers = headers if headers is not None else [
            ("Content-Type", "application/json"),
            ("Cache-Control", "no-store"),
            ("Pragma", "no-cache"),
            ("Content-Length", str(len(body))),
        ]

    def getheaders(self):
        return self._headers

    def read(self, amount):
        if self._fail:
            raise RuntimeError("SENSITIVE_READ_FAILURE")
        return self._buf.read(amount)


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.sock = FakeSocket()
        self.sent = []
        self.closed = False
        self.raise_on_request = False

    def request(self, *args, **kwargs):
        if self.raise_on_request:
            raise RuntimeError("SENSITIVE_AUTHORIZATION_CODE")
        self.sent.append((args, kwargs))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class PinnedHttpsSenderTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        flag = mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "",
            "MEMORY_OIDC_HTTPS_SENDER_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED": "true",
            "MEMORY_OIDC_TOKEN_EXCHANGE_REVIEW_ENABLED": "true",
        })
        flag.start()
        self.addCleanup(flag.stop)
        self.approved = [True]
        self.request = OfflineTokenRequest(
            method="POST", url=URL,
            headers=(
                ("accept", "application/json"),
                ("content-type", "application/x-www-form-urlencoded"),
                ("cache-control", "no-store"),
            ), body=BODY,
        )
        self.response = FakeResponse(self.json_body())
        self.connection = FakeConnection(self.response)
        self.created = []
        self.dns_results = [self.answer(PUBLIC)]
        self.dns_calls = []
        def resolver(*args, **kwargs):
            self.dns_calls.append((args, kwargs))
            return self.dns_results
        def make_conn(host, ip, *, context):
            self.created.append((host, ip, context))
            return self.connection
        patch_dns = mock.patch.object(sender_module.socket, "getaddrinfo", resolver)
        patch_conn = mock.patch.object(
            sender_module, "_PinnedHttpsConnection", side_effect=make_conn,
        )
        patch_dns.start(); patch_conn.start()
        self.addCleanup(patch_dns.stop)
        self.addCleanup(patch_conn.stop)
        self.sender = self.make_sender()

    @staticmethod
    def answer(ip, *, family=socket.AF_INET, port=443):
        return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                (ip, port))

    @staticmethod
    def json_body():
        return json.dumps({
            "access_token": ACCESS, "token_type": "Bearer",
            "expires_in": 300, "id_token": "eyJhbGciOiJSUzI1NiJ9.e30.c2ln",
        }).encode()

    def make_sender(self, *, approved=True, endpoint=URL, hostname=HOST):
        return PinnedOidcHttpsSenderReview(
            approved_token_endpoint=endpoint, approved_dns_hostname=hostname,
            network_approval_provider=(
                (lambda: self.approved[0]) if approved else None
            ),
        )

    def denied(self, *, sender=None, request=None):
        with self.assertRaisesRegex(
            OidcHttpsSenderReviewError, "memory_oidc_https_sender_unavailable",
        ):
            (sender or self.sender)(self.request if request is None else request)

    def test_valid_mock_transport_pins_ip_and_tls_hostname(self):
        outcome = self.sender(self.request)
        self.assertEqual(outcome.status_code, 200)
        self.assertEqual(outcome.final_url, URL)
        self.assertEqual(outcome.body, self.json_body())
        self.assertEqual(self.created[0][:2], (HOST, PUBLIC))
        ctx = self.created[0][2]
        self.assertTrue(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreaterEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(len(self.connection.sent), 1)
        args, kwargs = self.connection.sent[0]
        self.assertEqual(args, ("POST", "/token"))
        self.assertEqual(kwargs["body"], BODY)
        self.assertEqual(kwargs["headers"]["Accept-Encoding"], "identity")
        self.assertEqual(kwargs["headers"]["Connection"], "close")
        self.assertTrue(self.connection.closed)
        self.assertTrue(all(0 < n <= 3 for n in self.connection.sock.timeouts))

    def test_default_off_blocks_even_dns(self):
        os.environ["MEMORY_OIDC_HTTPS_SENDER_REVIEW_ENABLED"] = "false"
        self.denied()
        self.assertEqual(self.dns_calls, [])
        self.assertEqual(self.created, [])

    def test_explicit_network_approval_flag_required(self):
        os.environ["MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED"] = "false"
        self.denied()
        self.assertEqual(self.dns_calls, [])

    def test_missing_runtime_approval_provider_blocks(self):
        self.denied(sender=self.make_sender(approved=False))
        self.assertEqual(self.dns_calls, [])

    def test_runtime_approval_must_be_exact_true(self):
        for value in (False, None, 1, "true", {}, 0):
            with self.subTest(value=value):
                self.approved[0] = value
                self.denied()
        self.assertEqual(self.dns_calls, [])

    def test_target_url_and_method_must_match_pinned_request(self):
        for changes in (
            {"url":"https://evil.test/token"},
            {"url":URL+"?code=leak"},
            {"url":URL+"/"},
            {"url":"http://login.example.test/token"},
            {"method":"GET"},{"method":"PUT"},
            {"headers":(("authorization","Bearer secret"),)},
            {"body":b""},{"body":b"x"*4097},
            {"body":b"x\nSECRET"},
            {"body":b"x\rSECRET"},
        ):
            with self.subTest(change=tuple(changes.keys())):
                self.denied(request=replace(self.request, **changes))
        self.assertEqual(self.dns_calls, [])

    def test_invalid_constructor_target_cannot_connect(self):
        for endpoint, hostname in (
            ("http://login.example.test/token", HOST),
            ("https://127.0.0.1/token", "127.0.0.1"),
            ("https://localhost/token", "localhost"),
            ("https://login.example.test/token?x=1", HOST),
            ("https://login.example.test@evil.test/token", "evil.test"),
            (URL, "other.example.test"),
            ("https://something.internal/token", "something.internal"),
            ("https://something.local/token", "something.local"),
            ("https://LOGIN.EXAMPLE.TEST/token", "LOGIN.EXAMPLE.TEST"),
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(OidcHttpsSenderReviewError):
                    self.make_sender(endpoint=endpoint, hostname=hostname)
        self.assertEqual(self.dns_calls, [])

    def test_private_loopback_metadata_ipv6_addresses_denied(self):
        for ip in (
            "127.0.0.1","10.0.0.1","172.16.0.2","192.168.1.1",
            "169.254.169.254","0.0.0.0","100.64.0.1",
            "::1","fc00::1","fe80::1","::ffff:127.0.0.1",
            "fe80::1%lo0",
        ):
            with self.subTest(ip=ip):
                family = socket.AF_INET6 if ":" in ip else socket.AF_INET
                self.dns_results = [self.answer(ip,family=family)]
                self.denied()
        self.assertEqual(self.created, [])

    def test_mixed_public_and_private_dns_is_denied(self):
        self.dns_results = [
            self.answer(PUBLIC), self.answer("10.0.0.1"),
        ]
        self.denied()
        self.assertEqual(self.created, [])

    def test_public_ipv6_can_be_pinned(self):
        ip="2606:4700:4700::1111"
        self.dns_results=[self.answer(ip,family=socket.AF_INET6)]
        self.assertEqual(self.sender(self.request).status_code,200)
        self.assertEqual(self.created[0][1],ip)

    def test_empty_dns_and_excessive_results_denied(self):
        for answers in ([], [self.answer(PUBLIC)] * 17):
            self.dns_results=answers
            self.denied()
        self.assertEqual(self.created, [])

    def test_wrong_dns_family_port_or_shape_denied(self):
        bads = [
            [self.answer(PUBLIC, port=80)],
            [self.answer(PUBLIC, family=socket.AF_UNIX)],
            [("bad",)],
            ["8.8.8.8"],
        ]
        for batch in bads:
            with self.subTest(result=str(batch)[:50]):
                self.dns_results=batch
                self.denied()
        self.assertEqual(self.created, [])

    def test_dns_resolution_exception_is_generic_no_leak(self):
        with mock.patch.object(
            sender_module.socket, "getaddrinfo",
            side_effect=RuntimeError("SECRET_HOST_RESOLVER_PAYLOAD"),
        ):
            try:
                self.sender(self.request)
            except OidcHttpsSenderReviewError as err:
                self.assertNotIn("SECRET", str(err))
            else:
                self.fail("DNS exception must fail closed")

    def test_https_numeric_ip_dial_uses_original_sni_host(self):
        # Test real connection class numeric IP argument; socket is mocked.
        with mock.patch.object(sender_module, "_PinnedHttpsConnection",
                               _PinnedHttpsConnection):
            with mock.patch.object(
                sender_module.socket, "create_connection",
                return_value=object(),
            ) as dial:
                conn = _PinnedHttpsConnection(
                    HOST, PUBLIC, context=ssl.create_default_context(),
                )
                sock = conn._dial_numeric((HOST, 443), 3, None)
                self.assertIsNotNone(sock)
                dial.assert_called_once_with((PUBLIC,443), 3, None)
                self.assertEqual(conn.host, HOST)
                with self.assertRaises(OidcHttpsSenderReviewError):
                    conn._dial_numeric(("evil.test",443),3,None)
                conn.sock = None
                conn.close()

    def test_false_approval_after_dns_never_creates_connection(self):
        count=[0]
        def revoke():
            count[0]+=1
            return count[0] == 1
        s=PinnedOidcHttpsSenderReview(
            approved_token_endpoint=URL,approved_dns_hostname=HOST,
            network_approval_provider=revoke,
        )
        self.denied(sender=s)
        self.assertEqual(self.created, [])

    def test_no_proxy_or_environment_redirect_following(self):
        with mock.patch.dict(os.environ, {
            "HTTPS_PROXY":"http://127.0.0.1:3128",
            "HTTP_PROXY":"http://127.0.0.1:3128",
            "ALL_PROXY":"socks5://localhost:8888",
        }):
            self.sender(self.request)
        self.assertEqual(self.created[0][:2],(HOST,PUBLIC))
        self.assertEqual(len(self.connection.sent),1)

    def test_http_redirect_response_is_not_followed(self):
        self.response=FakeResponse(b"",status=302,headers=[
            ("Location","https://evil.test/collect"),
        ])
        self.connection.response=self.response
        out=self.sender(self.request)
        self.assertEqual(out.status_code,302)
        self.assertEqual(len(self.created),1)
        self.assertEqual(out.final_url,URL)
        # The parser from #150 separately rejects this response.
        parser=PinnedOidcTokenExchangeReview(
            token_endpoint=URL,redirect_uri="https://app.example.test/callback",
            client_id="synthetic-client",
            offline_transport=lambda _req:out,
        )
        with self.assertRaises(OidcTokenExchangeReviewError):
            parser.exchange(
                "CODE-synthetic-123","V"*64,
                "https://app.example.test/callback",URL,
            )

    def test_body_limit_rejected_without_unbounded_buffering(self):
        self.connection.response=FakeResponse(
            b"A"*(_MAX_BODY_PLUS_ONE:=12289),headers=[
                ("Content-Type","application/json"),
            ],
        )
        self.denied()
        self.assertTrue(self.connection.closed)

    def test_response_content_length_over_limit_denied(self):
        self.connection.response=FakeResponse(b"x",headers=[
            ("Content-Length","999999"),
        ])
        self.denied()
        self.assertTrue(self.connection.closed)

    def test_duplicate_content_length_denied(self):
        self.connection.response=FakeResponse(b"abc",headers=[
            ("Content-Length","3"),("Content-Length","3"),
        ])
        self.denied()

    def test_excessive_and_invalid_response_headers_rejected(self):
        for headers in (
            [("X-X", "yes")]*33,
            [("X-Bad", "value\nINJECTION")],
            [("X-Bad","Z"*513)],
        ):
            self.connection.closed=False
            self.connection.response=FakeResponse(b"{}",headers=headers)
            self.denied()
            self.assertTrue(self.connection.closed)

    def test_network_failure_redacts_exception_and_closes(self):
        self.connection.raise_on_request=True
        with self.assertRaises(OidcHttpsSenderReviewError) as ctx:
            self.sender(self.request)
        self.assertEqual(str(ctx.exception),"memory_oidc_https_sender_unavailable")
        self.assertTrue(self.connection.closed)

    def test_read_failure_redacts_and_closes(self):
        self.connection.response=FakeResponse(b"secret",fail=True)
        self.denied()
        self.assertTrue(self.connection.closed)

    def test_timeout_deadline_causes_fail_closed_cleanup(self):
        with mock.patch.object(sender_module.time, "monotonic",
                               side_effect=[0.0,10.0]):
            self.denied()
        self.assertTrue(self.connection.closed)

    def test_revoked_approval_after_read_denies_response(self):
        calls=[0]
        def approve():
            calls[0]+=1
            return calls[0] <= 3
        s=PinnedOidcHttpsSenderReview(
            approved_token_endpoint=URL,approved_dns_hostname=HOST,
            network_approval_provider=approve,
        )
        self.denied(sender=s)
        self.assertTrue(self.connection.closed)

    def test_no_real_sender_installed_in_oauth_or_memory_route(self):
        self.assertFalse(hasattr(self.sender,"remember"))
        self.assertFalse(hasattr(self.sender,"issue_browser_session"))
        self.assertNotIn("SECRET",repr(self.request))
        self.assertNotIn(ACCESS,repr(self.connection.response))


if __name__=="__main__":
    unittest.main()
