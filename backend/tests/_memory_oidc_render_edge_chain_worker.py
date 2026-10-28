"""Offline Render/Cloudflare multi-hop source-boundary regression.

Cloudflare and Render edge can form multiple proxy hops. Their actual
forwarded-header rewriting is NOT attested by this test. All peers, IPs and
proof callbacks below are synthetic, and even a matching review has no login
or memory-write authority. No real HTTP, Render, IdP or paid API.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from starlette.requests import Request
from uvicorn import Config

from backend.memory_oidc_trusted_proxy_review import (
    TrustedOidcProxyProvenanceReview, TrustedProxyReviewError,
)
from backend.tests._support import NoNetworkMixin

FLAG = "MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED"
INTERNAL = "relay.internal.test"
PUBLIC = "app.example.test"
PATH = "/__offline_review__/proxy/inspect"
PEER = "127.0.0.1"  # synthetic, NOT a Render/Cloudflare peer claim
CLIENT = "8.8.8.8"  # synthetic global client
HEADERS = [
    (b"host", INTERNAL.encode()),
    (b"x-forwarded-for", CLIENT.encode()),
    (b"x-forwarded-proto", b"https"),
    (b"x-forwarded-host", PUBLIC.encode()),
]


def synthetic_review(peer_provider, proof_provider):
    return TrustedOidcProxyProvenanceReview(
        approved_peer_ips=(PEER,),
        public_host=PUBLIC,
        internal_host=INTERNAL,
        review_path=PATH,
        raw_socket_peer_provider=peer_provider,
        ingress_header_sanitization_proof=proof_provider,
    )


def synthetic_request(headers=None, *, client=("203.0.113.9", 4311),
                      scheme="http"):
    return Request({
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "method": "GET", "path": PATH, "root_path": "",
        "raw_path": PATH.encode(), "query_string": b"",
        "scheme": scheme, "server": ("127.0.0.1", 8081),
        "client": client,
        "headers": list(HEADERS if headers is None else headers),
    })


class RenderEdgeMultiHopSyntheticTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {
            FLAG: "true",
            "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED": "false",
            "OPENAI_API_KEY": "",
        })
        patch.start()
        self.addCleanup(patch.stop)
        self.peer = [PEER]
        self.proof = [True]  # MOCK ONLY: not an actual Render edge attestation
        self.review = synthetic_review(
            lambda: self.peer[0], lambda: self.proof[0],
        )

    def deny(self, headers=None, **opts):
        with self.assertRaisesRegex(
            TrustedProxyReviewError, "memory_oidc_trusted_proxy_not_ready",
        ):
            self.review.evaluate(synthetic_request(headers, **opts))

    def test_synthetic_one_hop_positive_control_has_no_grant(self):
        v = self.review.evaluate(synthetic_request())
        self.assertEqual(v.family, "x-forwarded")
        self.assertEqual(v.external_origin, "https://" + PUBLIC)
        self.assertFalse(v.may_issue_browser_session)
        self.assertFalse(v.may_write_memory)

    def test_cloudflare_then_render_comma_chain_denied(self):
        self.deny([
            HEADERS[0],
            (b"x-forwarded-for", b"8.8.8.8, 1.1.1.1"),
            *HEADERS[2:],
        ])

    def test_three_hop_comma_chain_denied(self):
        self.deny([
            HEADERS[0],
            (b"x-forwarded-for", b"8.8.8.8, 1.1.1.1, 9.9.9.9"),
            *HEADERS[2:],
        ])

    def test_multi_hop_chain_without_spaces_still_denied(self):
        self.deny([
            HEADERS[0],
            (b"x-forwarded-for", b"8.8.8.8,1.1.1.1"),
            *HEADERS[2:],
        ])

    def test_repeated_xff_fields_cannot_substitute_chain(self):
        self.deny([*HEADERS, (b"X-Forwarded-For", b"1.1.1.1")])

    def test_combined_rfc_and_xforwarded_families_denied(self):
        self.deny([
            *HEADERS,
            (b"forwarded", b"for=8.8.8.8;proto=https;host=app.example.test"),
        ])

    def test_rfc_multi_entry_chain_denied(self):
        self.deny([
            HEADERS[0],
            (b"forwarded",
             b"for=8.8.8.8;proto=https;host=app.example.test,"
             b"for=1.1.1.1;proto=https;host=app.example.test"),
        ])

    def test_client_supplied_cf_connecting_ip_never_grants_authority(self):
        v = self.review.evaluate(synthetic_request([
            *HEADERS, (b"cf-connecting-ip", b"8.8.4.4"),
        ]))
        self.assertEqual(v.client_ip, CLIENT)
        self.assertFalse(v.may_issue_browser_session)
        self.assertFalse(v.may_write_memory)

    def test_render_request_id_header_is_not_a_trust_proof(self):
        v = self.review.evaluate(synthetic_request([
            *HEADERS, (b"rndr-id", b"synthetic-request-123"),
            (b"cf-ray", b"synthetic-ray-123"),
        ]))
        self.assertFalse(v.may_issue_browser_session)
        self.assertFalse(v.may_write_memory)

    def test_untrusted_actual_provider_peer_denied_despite_cf_headers(self):
        self.peer[0] = "10.0.0.5"
        self.deny([*HEADERS, (b"cf-connecting-ip", b"8.8.8.8")])

    def test_forged_asgi_scope_client_cannot_replace_raw_peer(self):
        self.peer[0] = "8.8.4.4"
        self.deny(client=(PEER, 443))

    def test_false_or_absent_header_cleaning_proof_denied(self):
        for value in (None, False, 1, "true"):
            with self.subTest(value=value):
                self.proof[0] = value
                self.deny()

    def test_header_family_cannot_be_authorized_from_source_ip_alone(self):
        self.deny([HEADERS[0], (b"x-forwarded-for", CLIENT.encode())])

    def test_https_scope_rewritten_by_middleware_is_rejected(self):
        self.deny(scheme="https")

    def test_public_host_header_cannot_replace_internal_host(self):
        self.deny([(b"host", PUBLIC.encode()), *HEADERS[1:]])

    def test_ambiguous_host_or_proto_with_comma_denied(self):
        for header in (
            (b"x-forwarded-proto", b"https,http"),
            (b"x-forwarded-host", b"app.example.test,evil.example.test"),
        ):
            with self.subTest(header=header[0]):
                modified = [(k, header[1] if k == header[0] else v)
                            for k, v in HEADERS]
                self.deny(modified)

    def test_feature_off_prevents_proof_callback_from_running(self):
        count = [0]

        def provider():
            count[0] += 1
            return True

        review = synthetic_review(lambda: PEER, provider)
        os.environ[FLAG] = "false"
        with self.assertRaises(TrustedProxyReviewError):
            review.evaluate(synthetic_request())
        self.assertEqual(count, [0])


class RenderEdgeUvicornScopeTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def _stack(self, *, enabled, headers=None):
        captured = []

        async def asgi(scope, receive, send):
            captured.append(dict(scope))
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        cfg = Config(
            asgi, interface="asgi3", log_config=None, lifespan="off",
            proxy_headers=enabled, forwarded_allow_ips="*",
        )
        cfg.load()
        events = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(event):
            events.append(event)

        await cfg.loaded_app(
            synthetic_request(headers).scope, receive, send,
        )
        self.assertEqual(events[0]["status"], 204)
        self.assertEqual(len(captured), 1)
        return captured[0]

    async def test_real_uvicorn_disabled_preserves_multihop_as_untrusted(self):
        raw = [
            HEADERS[0],
            (b"x-forwarded-for", b"8.8.8.8, 1.1.1.1"),
            *HEADERS[2:],
        ]
        result = await self._stack(enabled=False, headers=raw)
        self.assertEqual(result["scheme"], "http")
        self.assertEqual(result["client"], ("203.0.113.9", 4311))
        self.assertEqual(result["headers"], raw)
        with mock.patch.dict(os.environ, {FLAG: "true"}):
            reviewer = synthetic_review(lambda: PEER, lambda: True)
            with self.assertRaises(TrustedProxyReviewError):
                reviewer.evaluate(Request(result))

    async def test_real_uvicorn_disabled_keeps_onehop_untrusted_until_proven(self):
        result = await self._stack(enabled=False)
        self.assertEqual(result["scheme"], "http")
        self.assertEqual(result["headers"], HEADERS)
        with mock.patch.dict(os.environ, {FLAG: "true"}):
            reviewer = synthetic_review(lambda: PEER, lambda: False)
            with self.assertRaises(TrustedProxyReviewError):
                reviewer.evaluate(Request(result))

    async def test_real_uvicorn_enabled_positive_control_rewrites_proto(self):
        result = await self._stack(enabled=True)
        self.assertEqual(result["scheme"], "https")
        with mock.patch.dict(os.environ, {FLAG: "true"}):
            reviewer = synthetic_review(lambda: PEER, lambda: True)
            with self.assertRaises(TrustedProxyReviewError):
                reviewer.evaluate(Request(result))


if __name__ == "__main__":
    unittest.main()
