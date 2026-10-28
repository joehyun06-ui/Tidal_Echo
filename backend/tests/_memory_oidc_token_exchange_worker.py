"""Strict offline OAuth token endpoint response and #149 integration tests.

No network sockets, no real provider, no token forwarding; RSA keys ephemeral.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from backend.memory_oidc_token_exchange_review import (
    PinnedOidcTokenExchangeReview, OidcTokenExchangeReviewError,
    OfflineTokenRequest, OfflineTokenResponse,
)
from backend.memory_oidc_pkce_flow_review import (
    OidcCodePkceOfflineReview, OidcPkceReviewError,
)
from backend.tests._support import NoNetworkMixin

ISSUER = "https://login.example.test"
ENDPOINT = ISSUER + "/token"
AUTHORIZE = ISSUER + "/authorize"
REDIRECT = "https://app.example.test/callback/oidc"
CLIENT = "synthetic-oidc-client"
ACCESS = "synthetic-unusable-access-token-ABCdef0987654321"


class OidcTokenEndpointWireTests(NoNetworkMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.rsa_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048,
        )
        cls.rsa_pem = cls.rsa_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def setUp(self):
        super().setUp()
        flags = mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "",
            "MEMORY_OIDC_TOKEN_EXCHANGE_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_CODE_PKCE_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_ID_TOKEN_VERIFIER_REVIEW_ENABLED": "true",
        })
        flags.start()
        self.addCleanup(flags.stop)
        self.requests = []
        self.token = "eyJhbGciOiJSUzI1NiJ9.e30.c2lnbmF0dXJl"
        self.response_override = None
        self.adapter = self.make_adapter()

    @staticmethod
    def b64(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    def make_adapter(self, *, transport=True, url=ENDPOINT, redirect=REDIRECT,
                     client=CLIENT):
        return PinnedOidcTokenExchangeReview(
            token_endpoint=url, redirect_uri=redirect, client_id=client,
            offline_transport=self.transport if transport else None,
        )

    def good_response(self, *, id_token=None, **changes):
        data = {
            "access_token": ACCESS,
            "token_type": "Bearer",
            "expires_in": 300,
            "id_token": id_token if id_token is not None else self.token,
        }
        data.update(changes)
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return self.wrap(raw)

    def wrap(self, body, *, status=200, url=ENDPOINT, headers=None):
        if headers is None:
            headers=(
                ("content-type", "application/json"),
                ("cache-control", "no-store"),
                ("pragma", "no-cache"),
                ("content-length", str(len(body))),
            )
        return OfflineTokenResponse(
            status_code=status, final_url=url,
            headers=headers, body=body,
        )

    def transport(self, request):
        self.requests.append(request)
        return (
            self.response_override
            if self.response_override is not None
            else self.good_response()
        )

    def exchange(self, *, adapter=None, code="CODE-synthetic-123",
                 verifier="V"*64, redirect=REDIRECT, endpoint=ENDPOINT):
        return (adapter or self.adapter).exchange(
            code, verifier, redirect, endpoint,
        )

    def reject(self, **changes):
        with self.assertRaises(OidcTokenExchangeReviewError):
            self.exchange(**changes)

    def test_generates_exact_form_encoded_authorization_code_request(self):
        self.assertEqual(self.exchange(), self.token)
        self.assertEqual(len(self.requests), 1)
        req=self.requests[0]
        self.assertEqual(type(req), OfflineTokenRequest)
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.url, ENDPOINT)
        self.assertEqual(req.headers, (
            ("accept", "application/json"),
            ("content-type", "application/x-www-form-urlencoded"),
            ("cache-control", "no-store"),
        ))
        fields=parse_qs(req.body.decode("ascii"), keep_blank_values=True)
        self.assertEqual(fields, {
            "grant_type": ["authorization_code"],
            "code": ["CODE-synthetic-123"],
            "redirect_uri": [REDIRECT],
            "client_id": [CLIENT],
            "code_verifier": ["V"*64],
        })
        self.assertNotIn("CODE-synthetic", repr(req))
        self.assertNotIn("V"*64, repr(req))

    def test_legacy_shared_relay_token_is_not_user_identity_or_credential(self):
        self.exchange()
        body=self.requests[0].body.decode("ascii")
        self.assertNotIn("authorization", str(self.requests[0].headers))
        self.assertNotIn("relay", body.lower())
        self.assertNotIn("client_secret", body)

    def test_no_transport_is_always_fail_closed(self):
        self.reject(adapter=self.make_adapter(transport=False))
        self.assertEqual(len(self.requests), 0)

    def test_default_off_prevents_transport(self):
        os.environ["MEMORY_OIDC_TOKEN_EXCHANGE_REVIEW_ENABLED"]="false"
        self.reject()
        self.assertEqual(len(self.requests), 0)

    def test_wrong_endpoint_and_redirect_rejected_before_transport(self):
        for field, value in (
            ("endpoint", "https://evil.test/token"),
            ("endpoint", ENDPOINT+"?redirect=x"),
            ("endpoint", "http://login.example.test/token"),
            ("redirect", "https://evil.test/callback"),
            ("redirect", REDIRECT+"#fragment"),
        ):
            with self.subTest(field=field, value=value):
                self.reject(**{field: value})
        self.assertEqual(len(self.requests), 0)

    def test_bad_code_and_pkce_verifier_never_transport(self):
        for code in ("", "short", "bad code", "a"*513, True, None):
            with self.subTest(code=code):
                self.reject(code=code)
        for verifier in ("", "x"*42, "x"*129, "x x"*20, True, None):
            with self.subTest(verifier=verifier):
                self.reject(verifier=verifier)
        self.assertEqual(len(self.requests), 0)

    def test_invalid_pinned_config_refused(self):
        for url in (
            "http://login.example.test/token",
            "https://login.example.test@evil.test/token",
            "https://login.example.test/token?x=1",
        ):
            with self.assertRaises(OidcTokenExchangeReviewError):
                self.make_adapter(url=url)
        with self.assertRaises(OidcTokenExchangeReviewError):
            self.make_adapter(client="")

    def test_strict_valid_token_response_extracts_only_id_token(self):
        self.assertEqual(self.exchange(), self.token)
        self.assertNotIn(ACCESS, repr(self.response_override))
        self.assertNotIn(ACCESS, repr(self.requests[0]))
        self.assertNotIn(ACCESS, self.exchange())
        self.assertFalse(hasattr(self.adapter, "issue_browser_session"))
        self.assertFalse(hasattr(self.adapter, "remember"))

    def test_redirect_status_or_changed_final_url_rejected(self):
        for result in (
            self.wrap(b"{}", status=302),
            self.wrap(b"{}", status=307),
            self.good_response(),
        ):
            if result.status_code == 200:
                result=self.wrap(result.body,url="https://evil.test/token")
            self.response_override=result
            with self.subTest(status=result.status_code,final=result.final_url):
                self.reject()

    def test_http_errors_do_not_become_identity(self):
        for status in (400, 401, 403, 429, 500, 503):
            with self.subTest(status=status):
                self.response_override=self.wrap(
                    b'{"error":"invalid_grant"}',status=status,
                )
                self.reject()

    def test_wrong_or_missing_json_mime_rejected(self):
        for mime in (
            "text/html", "text/plain", "application/x-www-form-urlencoded",
            "application/json; charset=latin1", "application/json; charset=UTF-8",
            "", None,
        ):
            with self.subTest(mime=mime):
                headers=(
                    (("content-type",mime),) if mime is not None else ()
                )+(("cache-control","no-store"),("pragma","no-cache"))
                self.response_override=self.wrap(
                    self.good_response().body, headers=headers,
                )
                self.reject()

    def test_no_store_and_no_cache_required(self):
        for missing in ("cache-control", "pragma"):
            with self.subTest(missing=missing):
                hs=tuple(x for x in self.good_response().headers
                         if x[0]!=missing)
                self.response_override=self.wrap(
                    self.good_response().body,headers=hs,
                )
                self.reject()

    def test_duplicate_and_untrusted_headers_rejected(self):
        for name,value in (
            ("content-type","text/html"),
            ("cache-control","public"),
            ("pragma","cache"),
        ):
            hs=self.good_response().headers+((name,value),)
            self.response_override=self.wrap(self.good_response().body,headers=hs)
            self.reject()
        for name in ("location","set-cookie","content-encoding",
                     "transfer-encoding","www-authenticate"):
            hs=self.good_response().headers+((name,"bad"),)
            self.response_override=self.wrap(self.good_response().body,headers=hs)
            self.reject()

    def test_invalid_or_mismatched_content_length_rejected(self):
        for val in ("0","NaN","-1","999999999999999999"):
            hs=tuple(
                (name,val if name=="content-length" else value)
                for name,value in self.good_response().headers
            )
            self.response_override=self.wrap(
                self.good_response().body,headers=hs,
            )
            self.reject()

    def test_oversize_or_empty_body_denied(self):
        for raw in (b"",b"A"*12289):
            self.response_override=self.wrap(raw)
            self.reject()

    def test_json_duplicate_key_rejected(self):
        obj=self.good_response().body.decode("ascii")
        bad=obj.replace('"token_type":"Bearer"',
                        '"token_type":"Bearer","token_type":"Basic"')
        self.response_override=self.wrap(bad.encode("ascii"))
        self.reject()

    def test_invalid_json_unicode_and_scalar_rejected(self):
        for raw in (b"null",b"[]",b"true",b'{"x":NaN}',
                    b'{"\xff":1}',b'{"unterminated":'):
            self.response_override=self.wrap(raw)
            self.reject()

    def test_strict_required_fields_and_no_refresh_token(self):
        good=json.loads(self.good_response().body)
        for key in good:
            bad=dict(good);del bad[key]
            self.response_override=self.wrap(json.dumps(bad).encode())
            self.reject()
        for key in ("refresh_token", "extra", "error", "scope"):
            bad=dict(good);bad[key]="bad"
            self.response_override=self.wrap(json.dumps(bad).encode())
            self.reject()

    def test_access_token_never_exposed_for_invalid_response(self):
        self.response_override=self.wrap(json.dumps({
            "error":"invalid_grant","error_description":"SENSITIVE_CODE",
        }).encode())
        with self.assertRaises(OidcTokenExchangeReviewError) as ctx:
            self.exchange()
        self.assertEqual(str(ctx.exception), "memory_oidc_token_exchange_unavailable")
        self.assertNotIn("SENSITIVE",str(ctx.exception))

    def test_malformed_access_token_and_token_type_rejected(self):
        for change in (
            {"access_token":""},{"access_token":3},
            {"access_token":"a"*4097},{"access_token":"token with spaces"},
            {"token_type":"Basic"},{"token_type":"bearer"},
            {"token_type":True},
        ):
            self.response_override=self.good_response(**change)
            self.reject()

    def test_expiration_not_bool_or_string_and_bounded(self):
        for expires in (True, False, "300", 1.5, 0, -1, 3601, None):
            with self.subTest(expires=expires):
                self.response_override=self.good_response(expires_in=expires)
                self.reject()

    def test_malformed_unsigned_id_token_not_accepted(self):
        for val in ("",None,True,"a.b","a.b.c.d","a.!.c","a"*8193):
            with self.subTest(id_token=type(val).__name__):
                self.response_override=self.good_response(id_token=val)
                self.reject()

    def test_malformed_transport_results_fail_closed(self):
        for result in (None,{},True,"{}",b"{}",object()):
            self.response_override=result
            if result is None:
                # None means the fixture's default response; override
                # transport to explicitly return None instead.
                broken=self.make_adapter()
                broken._transport=lambda _req: None
                self.reject(adapter=broken)
            else:
                self.reject()

    def test_disabled_during_transport_does_not_accept(self):
        def disable(_req):
            os.environ["MEMORY_OIDC_TOKEN_EXCHANGE_REVIEW_ENABLED"]="false"
            return self.good_response()
        adapter=self.make_adapter()
        adapter._transport=disable
        self.reject(adapter=adapter)

    def test_integrated_pkce_and_signed_id_token_both_required(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db=str(Path(temp.name)/"full-offline-code-flow.sqlite")
        now=[100000.0]
        bind=["B"*43]
        signed_nonce=[""]
        def sign_token():
            claims={
                "iss":ISSUER, "aud":CLIENT, "sub":"opaque-test-user",
                "nonce":signed_nonce[0],"iat":99980,
                "exp":100180,"auth_time":99950,
            }
            header={"alg":"RS256","kid":"test-rsa"}
            parts=".".join(self.b64(json.dumps(x,separators=(",",":")).encode())
                           for x in (header,claims))
            sig=self.rsa_key.sign(
                parts.encode(),padding.PKCS1v15(),hashes.SHA256(),
            )
            return parts+"."+self.b64(sig)
        captured=[]
        def transport(req):
            captured.append(req)
            return self.good_response(id_token=sign_token())
        adapter=PinnedOidcTokenExchangeReview(
            token_endpoint=ENDPOINT,redirect_uri=REDIRECT,client_id=CLIENT,
            offline_transport=transport,
        )
        flow=OidcCodePkceOfflineReview(
            path=db,issuer=ISSUER,client_id=CLIENT,
            authorization_endpoint=AUTHORIZE,token_endpoint=ENDPOINT,
            redirect_uri=REDIRECT,
            browser_binding_provider=lambda: bind[0],
            token_exchanger=adapter.exchange,
            pinned_public_keys={"test-rsa":self.rsa_pem},
            at_rest_key=b"K"*32,clock=lambda:now[0],
        )
        plan=flow.prepare()
        fields=parse_qs(urlsplit(plan.authorize_url).query)
        signed_nonce[0]=fields["nonce"][0]
        result=flow.complete(
            state=fields["state"][0],code="CODE-synthetic-123",
        )
        self.assertEqual(result.subject,"opaque-test-user")
        self.assertFalse(result.may_issue_browser_session)
        self.assertFalse(result.may_write_memory)
        self.assertEqual(len(captured),1)
        form=parse_qs(captured[0].body.decode("ascii"))
        self.assertEqual(
            self.b64(hashlib.sha256(form["code_verifier"][0].encode()).digest()),
            fields["code_challenge"][0],
        )
        with self.assertRaises(OidcPkceReviewError):
            flow.complete(state=fields["state"][0],code="CODE-synthetic-123")
        self.assertEqual(len(captured),1)

    def test_integrated_bad_token_response_burns_original_state(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db=str(Path(temp.name)/"invalid-response.sqlite")
        capture=[]
        adapter=PinnedOidcTokenExchangeReview(
            token_endpoint=ENDPOINT,redirect_uri=REDIRECT,client_id=CLIENT,
            offline_transport=lambda req: (
                capture.append(req) or self.wrap(
                    b'{"error":"invalid_grant"}',status=400
                )
            ),
        )
        flow=OidcCodePkceOfflineReview(
            path=db,issuer=ISSUER,client_id=CLIENT,
            authorization_endpoint=AUTHORIZE,token_endpoint=ENDPOINT,
            redirect_uri=REDIRECT,
            browser_binding_provider=lambda:"B"*43,
            token_exchanger=adapter.exchange,
            pinned_public_keys={"test-rsa":self.rsa_pem},
            at_rest_key=b"K"*32,clock=lambda:100000.0,
        )
        plan=flow.prepare()
        state=parse_qs(urlsplit(plan.authorize_url).query)["state"][0]
        with self.assertRaises(OidcPkceReviewError):
            flow.complete(state=state,code="CODE-synthetic-123")
        with self.assertRaises(OidcPkceReviewError):
            flow.complete(state=state,code="CODE-synthetic-123")
        self.assertEqual(len(capture),1)

    def test_transport_errors_do_not_expose_sensitive_payload(self):
        adapter=self.make_adapter()
        adapter._transport=lambda _: (_ for _ in ()).throw(
            RuntimeError("token=SECRET_INTERNAL")
        )
        with self.assertRaises(OidcTokenExchangeReviewError) as cm:
            self.exchange(adapter=adapter)
        self.assertEqual(str(cm.exception),"memory_oidc_token_exchange_unavailable")


if __name__=="__main__":
    unittest.main()
