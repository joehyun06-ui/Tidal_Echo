"""Synthetic ASGI raw proxy header trust boundary; no external sockets."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from starlette.requests import Request

from backend.memory_oidc_trusted_proxy_review import (
    TrustedOidcProxyProvenanceReview, TrustedProxyReviewError,
)
from backend.tests._support import NoNetworkMixin

INTERNAL = "relay.internal.test"
PUBLIC = "app.example.test"
PATH = "/__offline_review__/proxy/inspect"
PEER = "127.0.0.1"
IP = "8.8.8.8"
BASE = [
    ("host", INTERNAL),
    ("x-forwarded-for", IP),
    ("x-forwarded-proto", "https"),
    ("x-forwarded-host", PUBLIC),
]


class TrustedProxyProvenanceTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "",
            "MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED": "false",
        })
        patch.start()
        self.addCleanup(patch.stop)
        self.raw_peer = [PEER]
        self.proof = [True]
        self.evaluations = [0]
        self.review = self.make_review()

    def make_review(self, *, peers=(PEER,), public=PUBLIC, internal=INTERNAL,
                    peer_provider=True, proof_provider=True):
        return TrustedOidcProxyProvenanceReview(
            approved_peer_ips=peers,
            public_host=public, internal_host=internal,
            review_path=PATH,
            raw_socket_peer_provider=(
                lambda: self.raw_peer[0]
            ) if peer_provider else None,
            ingress_header_sanitization_proof=(
                lambda: self.proof[0]
            ) if proof_provider else None,
        )

    def request(self, headers=None, *, scheme="http", path=PATH,
                server=("127.0.0.1", 8081),
                client=("10.9.9.9", 12345), query=b"",
                root="", method="GET"):
        if headers is None:
            headers = list(BASE)
        return Request({
            "type":"http", "scheme":scheme, "method":method, "path":path,
            "root_path":root, "server":server, "client":client,
            "query_string":query,
            "headers":[
                (name.encode("ascii") if isinstance(name,str) else name,
                 value.encode("ascii") if isinstance(value,str) else value)
                for name, value in headers
            ],
        })

    def deny(self, request=None, *, reviewer=None):
        with self.assertRaisesRegex(
            TrustedProxyReviewError,
            "memory_oidc_trusted_proxy_not_ready",
        ):
            (reviewer or self.review).evaluate(
                self.request() if request is None else request
            )

    def test_exact_trusted_peer_onehop_xforwarded_success(self):
        result=self.review.evaluate(self.request())
        self.assertEqual(result.external_origin,"https://"+PUBLIC)
        self.assertEqual(result.family,"x-forwarded")
        self.assertEqual(result.client_ip, IP)
        self.assertEqual(result.proxy_peer,PEER)
        self.assertFalse(result.may_issue_browser_session)
        self.assertFalse(result.may_write_memory)
        self.assertNotIn(PEER,repr(result))
        self.assertNotIn(IP,repr(result))

    def test_rfc_forwarded_strict_onehop_ipv4_success(self):
        heads=[
            ("host",INTERNAL),
            ("forwarded","for="+IP+";proto=https;host="+PUBLIC),
        ]
        obj=self.review.evaluate(self.request(heads))
        self.assertEqual(obj.family,"forwarded")
        self.assertEqual(obj.client_ip,IP)

    def test_direct_untrusted_raw_socket_peer_denied(self):
        for v in ("8.8.4.4","::1","10.2.3.4","192.168.0.1"):
            with self.subTest(peer=v):
                self.raw_peer[0]=v
                self.deny()
        self.raw_peer[0]=PEER

    def test_rewritten_scope_client_cannot_impersonate_socket(self):
        self.raw_peer[0]="8.8.4.4"
        # Even if an ASGI middleware already forged the scope client to
        # look identical to an approved proxy, the raw transport callback wins.
        self.deny(self.request(client=(PEER, 3400)))

    def test_any_asgi_client_value_does_not_change_external_result(self):
        for client in (("1.1.1.1",9000),("127.0.0.1",1),
                       ("user-supplied",999),None):
            with self.subTest(client=client):
                result=self.review.evaluate(self.request(client=client))
                self.assertEqual(result.client_ip,IP)

    def test_off_prevents_any_provider_execution(self):
        count=[0]
        def call():
            count[0]+=1
            return PEER
        reviewer=TrustedOidcProxyProvenanceReview(
            approved_peer_ips=(PEER,), public_host=PUBLIC,
            internal_host=INTERNAL, review_path=PATH,
            raw_socket_peer_provider=call,
            ingress_header_sanitization_proof=lambda: True,
        )
        os.environ["MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED"]="false"
        self.deny(reviewer=reviewer)
        self.assertEqual(count[0],0)

    def test_explicit_proxy_sanitization_proof_required(self):
        self.proof[0]=False
        self.deny()
        self.proof[0]=1
        self.deny()
        self.proof[0]=None
        self.deny()

    def test_missing_server_only_callbacks_refused(self):
        for kw in ({"peer_provider":False},{"proof_provider":False}):
            with self.subTest(kw=kw):
                with self.assertRaises(TrustedProxyReviewError):
                    self.make_review(**kw)

    def test_invalid_peer_configuration_refused(self):
        for peers in (
            (),"*",("*",),("0.0.0.0/0",),
            ("127.0.0.1","127.0.0.1"),
            tuple("10.0.0."+str(i) for i in range(1,6)),
            ("::ffff:127.0.0.1",),
            ("127.000.000.001",),("bad-proxy",),
        ):
            with self.subTest(peers=peers):
                with self.assertRaises(TrustedProxyReviewError):
                    self.make_review(peers=peers)

    def test_tampered_proto_http_denied(self):
        self.deny(self.request([*BASE[:2],("x-forwarded-proto","http"),BASE[-1]]))

    def test_public_host_and_trailing_port_must_match(self):
        for h in ("evil.test",PUBLIC+":443","evil."+PUBLIC,
                  "APP.EXAMPLE.TEST","app.example.test,evil.test"):
            with self.subTest(host=h):
                self.deny(self.request(BASE[:-1]+[("x-forwarded-host",h)]))

    def test_inbound_host_spoofing_fails_even_with_valid_forwarded_host(self):
        for host in ("evil.test",PUBLIC,INTERNAL+":8081",INTERNAL.upper()):
            with self.subTest(host=host):
                self.deny(self.request([("host",host)]+BASE[1:]))

    def test_x_forwarded_for_multi_hop_and_duplicates_denied(self):
        for ip in ("1.1.1.1,8.8.8.8","1.1.1.1, 8.8.8.8",
                   "8.8.8.8:443","unknown","_hidden","1.1.1.1 ",
                   " 1.1.1.1"):
            self.deny(self.request([BASE[0],("x-forwarded-for",ip),*BASE[2:]]))
        self.deny(self.request(BASE+[BASE[1]]))

    def test_external_ip_private_loopback_reserved_and_mapped_denied(self):
        for ip in (
            "127.0.0.1","10.1.2.3","172.16.0.1","192.168.1.1",
            "169.254.169.254","0.0.0.0","100.64.0.1",
            "::1","fc00::1","fe80::1","::ffff:8.8.8.8",
        ):
            with self.subTest(ip=ip):
                self.deny(self.request([BASE[0],("x-forwarded-for",ip),*BASE[2:]]))

    def test_public_ipv6_single_xforwarded_client_valid(self):
        x=self.review.evaluate(self.request([
            BASE[0],("x-forwarded-for","2606:4700:4700::1111"),*BASE[2:]
        ]))
        self.assertEqual(x.client_ip,"2606:4700:4700::1111")

    def test_forwarded_mixed_with_xforwarded_family_denied(self):
        self.deny(self.request(BASE+[(
            "forwarded","for="+IP+";proto=https;host="+PUBLIC,
        )]))

    def test_forwarded_duplicate_and_multiple_proxy_entries_denied(self):
        vals=[
            "for="+IP+";proto=https;host="+PUBLIC+", for=1.1.1.1",
            "for="+IP+";proto=https;host="+PUBLIC+";host="+PUBLIC,
            "for="+IP+";proto=https;host="+PUBLIC+";proto=https",
            "for="+IP+";proto=https",
            "for="+IP+";proto=https;host="+PUBLIC+";by=proxy",
            "for=\""+IP+"\";proto=https;host="+PUBLIC,
            "for=[2606:4700:4700::1111];proto=https;host="+PUBLIC,
        ]
        for val in vals:
            with self.subTest(val=val[:35]):
                self.deny(self.request([("host",INTERNAL),("forwarded",val)]))

    def test_forwarded_wrong_issuer_proto_or_host_denied(self):
        for val in (
            "for="+IP+";proto=http;host="+PUBLIC,
            "for="+IP+";proto=https;host=evil.test",
            "for=127.0.0.1;proto=https;host="+PUBLIC,
        ):
            self.deny(self.request([("host",INTERNAL),("forwarded",val)]))

    def test_case_insensitive_duplicate_header_names_rejected(self):
        self.deny(self.request(BASE+[("X-FORWARDED-PROTO","https")]))
        self.deny(self.request(BASE+[("HOST",INTERNAL)]))

    def test_client_identity_and_legacy_bearer_headers_rejected(self):
        for name in (
            "authorization","x-user-id","x-principal-id","x-login-id",
            "x-session-id","x-memory-identity","x-auth-epoch",
            "x-real-ip","proxy-authorization","x-original-url","x-rewrite-url",
            "x-forwarded-prefix","x-forwarded-port",
        ):
            with self.subTest(header=name):
                self.deny(self.request(BASE+[(name,"forged")]))

    def test_missing_part_of_single_header_family_denied(self):
        for name in ("x-forwarded-for","x-forwarded-proto","x-forwarded-host","host"):
            self.deny(self.request([x for x in BASE if x[0]!=name]))

    def test_normalized_scope_scheme_is_not_trusted_original(self):
        self.deny(self.request(scheme="https"))
        self.deny(self.request(scheme="ws"))

    def test_wrong_internal_transport_target_and_path_denied(self):
        for obj in (
            self.request(server=("0.0.0.0",8081)),
            self.request(server=("127.0.0.1",80)),
            self.request(path="/app/login"),
            self.request(root="/proxy"),
            self.request(query=b"principal=spoof"),
        ):
            self.deny(obj)

    def test_header_injection_controls_and_malformed_bytes_denied(self):
        for vals in (
            BASE+[("x-custom", "attack\r\nHost:evil.test")],
            BASE+[("x-custom", b"\xff")],
            BASE+[("x-custom", "X"*1025)],
            BASE+[("x-custom", "")],
        ):
            self.deny(self.request(vals))

    def test_too_many_headers_and_wrong_header_shapes_denied(self):
        too_many=BASE+[("x-test"+str(i),"v") for i in range(47)]
        self.deny(self.request(too_many))
        scope=self.request().scope
        scope["headers"]=[(b"host",b"relay.internal.test"),"bad"]
        self.deny(Request(scope))

    def test_attestation_revocation_during_review_fails_closed(self):
        calls=[0]
        def proof():
            calls[0]+=1
            return calls[0] == 1
        reviewer=TrustedOidcProxyProvenanceReview(
            approved_peer_ips=(PEER,), public_host=PUBLIC,
            internal_host=INTERNAL, review_path=PATH,
            raw_socket_peer_provider=lambda:PEER,
            ingress_header_sanitization_proof=proof,
        )
        self.deny(reviewer=reviewer)

    def test_peer_change_during_review_fails_closed(self):
        seq=iter([PEER,"1.1.1.1"])
        reviewer=TrustedOidcProxyProvenanceReview(
            approved_peer_ips=(PEER,),public_host=PUBLIC,
            internal_host=INTERNAL,review_path=PATH,
            raw_socket_peer_provider=lambda:next(seq),
            ingress_header_sanitization_proof=lambda:True,
        )
        self.deny(reviewer=reviewer)

    def test_review_is_not_login_and_cannot_write_memory(self):
        result=self.review.evaluate(self.request())
        self.assertFalse(result.may_issue_browser_session)
        self.assertFalse(result.may_write_memory)
        self.assertFalse(hasattr(self.review,"issue_browser_session"))
        self.assertFalse(hasattr(self.review,"remember"))
        self.assertFalse(hasattr(self.review,"app"))
        self.assertFalse(hasattr(self.review,"commit"))

    def test_invalid_hosts_cannot_configure_reviewer(self):
        for name in ("localhost","app.local","http://app.example.test",
                     PUBLIC+":443","app.example.test/path","app.EXAMPLE.test"):
            with self.subTest(name=name):
                with self.assertRaises(TrustedProxyReviewError):
                    self.make_review(public=name)
        with self.assertRaises(TrustedProxyReviewError):
            self.make_review(internal=PUBLIC)


if __name__ == "__main__":
    unittest.main()
