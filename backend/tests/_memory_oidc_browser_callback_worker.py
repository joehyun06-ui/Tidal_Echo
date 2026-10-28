"""Synthetic ASGI OIDC login start/callback; no server, network or writes."""
from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from starlette.requests import Request

from backend.memory_oidc_browser_callback_review import (
    OidcBrowserCallbackOfflineReview, OidcBrowserCallbackReviewError,
)
from backend.tests._support import NoNetworkMixin

ISSUER = "https://login.example.test"
AUTH = ISSUER + "/authorize"
TOKEN = ISSUER + "/token"
REDIRECT = "https://app.example.test/callback/oidc"
CLIENT = "synthetic-oidc-client"
COOKIE_KEY = b"C" * 48
STATE_KEY = b"A" * 32
CODE = "CODE-synthetic-123"


class BrowserOidcCallbackTests(NoNetworkMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.private = rsa.generate_private_key(
            public_exponent=65537, key_size=2048,
        )
        cls.public = cls.private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = str(Path(temp.name) / "browser-review.sqlite")
        flags = mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "",
            "MEMORY_OIDC_BROWSER_CALLBACK_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_CODE_PKCE_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_ID_TOKEN_VERIFIER_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_HTTPS_SENDER_REVIEW_ENABLED": "false",
            "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED": "false",
        })
        flags.start()
        self.addCleanup(flags.stop)
        self.now = [100000.0]
        self.current_nonce = ""
        self.exchange_log = []
        self.exchange_mode = "valid"
        self.review = self.new_review()

    @staticmethod
    def b64(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    def sign(self, nonce=None):
        claims = {
            "iss": ISSUER, "aud": CLIENT,
            "sub": "opaque-synthetic-person",
            "nonce": self.current_nonce if nonce is None else nonce,
            "iat": 99980, "exp": 100180, "auth_time": 99950,
        }
        header = {"alg": "RS256", "kid": "test", "typ": "JWT"}
        parts = ".".join(self.b64(json.dumps(x, separators=(",", ":")).encode())
                         for x in (header, claims))
        signature = self.private.sign(
            parts.encode(), padding.PKCS1v15(), hashes.SHA256(),
        )
        return parts + "." + self.b64(signature)

    def exchanger(self, code, verifier, redirect, endpoint):
        self.exchange_log.append((code, verifier, redirect, endpoint))
        if self.exchange_mode == "failed":
            raise RuntimeError("synthetic token error")
        if self.exchange_mode == "wrong_nonce":
            return self.sign("Z" * 43)
        return self.sign()

    def new_review(self, *, cookie_key=COOKIE_KEY, exchanger=True,
                   issuer=ISSUER, redirect=REDIRECT):
        return OidcBrowserCallbackOfflineReview(
            path=self.db, issuer=issuer, client_id=CLIENT,
            authorization_endpoint=AUTH, token_endpoint=TOKEN,
            redirect_uri=redirect,
            cookie_hmac_key=cookie_key, state_encryption_key=STATE_KEY,
            pinned_public_keys={"test": self.public},
            token_exchanger=self.exchanger if exchanger else None,
            clock=lambda: self.now[0],
        )

    def request(self, *, method="GET", path="/callback/oidc", query="",
                scheme="https", headers=None, host="app.example.test"):
        if headers is None:
            headers = [("host", host)]
        return Request({
            "type": "http", "scheme": scheme,
            "method": method, "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query.encode("ascii"),
            "headers": [
                (key.encode("ascii"), value.encode("ascii"))
                for key, value in headers
            ],
            "server": (host, 443),
            "client": ("127.0.0.1", 1001),
        })

    def start_request(self, *, headers=None, method="POST", path=None,
                      scheme="https", query=""):
        if headers is None:
            headers = [
                ("host", "app.example.test"),
                ("origin", "https://app.example.test"),
                ("sec-fetch-site", "same-origin"),
                ("content-type", "application/json"),
            ]
        return self.request(
            method=method, path=path or "/__offline_review__/oidc/start",
            scheme=scheme, headers=headers, query=query,
        )

    def begin(self, *, review=None, request=None):
        result = (review or self.review).begin(request or self.start_request())
        params = parse_qs(urlsplit(result.authorization_url).query)
        self.current_nonce = params["nonce"][0]
        return result, params

    def cookie_pair(self, start):
        return start.set_cookie.split(";", 1)[0]

    def callback_request(self, start, params, *, headers=None, query=None,
                         path="/callback/oidc", scheme="https"):
        if headers is None:
            headers = [
                ("host", "app.example.test"),
                ("cookie", self.cookie_pair(start)),
                ("sec-fetch-site", "cross-site"),
                ("sec-fetch-mode", "navigate"),
                ("sec-fetch-dest", "document"),
            ]
        if query is None:
            query = urlencode({
                "code": CODE, "state": params["state"][0], "iss": ISSUER,
            })
        return self.request(
            method="GET", path=path, scheme=scheme,
            headers=headers, query=query,
        )

    def complete(self, start, params, *, review=None, request=None):
        return (review or self.review).complete(
            request or self.callback_request(start, params),
        )

    def denied(self, request, *, review=None):
        with self.assertRaises(OidcBrowserCallbackReviewError):
            (review or self.review).complete(request)

    def test_valid_cross_site_callback_with_real_rsa_and_oneuse_state(self):
        start, params = self.begin()
        result = self.complete(start, params)
        self.assertEqual(result.verified_subject.subject, "opaque-synthetic-person")
        self.assertFalse(result.may_issue_browser_session)
        self.assertFalse(result.may_write_memory)
        self.assertEqual(len(self.exchange_log), 1)
        self.assertNotIn("opaque-synthetic-person", repr(result))
        with self.assertRaises(OidcBrowserCallbackReviewError):
            self.complete(start, params)
        self.assertEqual(len(self.exchange_log), 1)

    def test_cookie_attributes_are_host_only_secure_httponly_lax(self):
        start, params = self.begin()
        self.assertTrue(start.set_cookie.startswith("__Host-gt-oidc-review="))
        for fragment in (
            "Path=/", "Max-Age=180", "Secure", "HttpOnly", "SameSite=Lax",
        ):
            self.assertIn(fragment, start.set_cookie)
        self.assertNotIn("Domain=", start.set_cookie)
        self.assertNotIn(params["nonce"][0], repr(start))
        self.assertNotIn(params["state"][0], repr(start))
        self.assertFalse(start.may_issue_browser_session)
        self.assertFalse(start.may_write_memory)

    def test_missing_start_flag_denies_before_cookie_or_sqlite(self):
        os.environ["MEMORY_OIDC_BROWSER_CALLBACK_REVIEW_ENABLED"]="false"
        with self.assertRaises(OidcBrowserCallbackReviewError):
            self.review.begin(self.start_request())
        self.assertFalse(Path(self.db).exists())

    def test_start_must_be_same_origin_post_json_without_query(self):
        cases=[
            {"method":"GET"},
            {"method":"PUT"},
            {"scheme":"http"},
            {"query":"state=forged"},
            {"path":"/app/real/login"},
        ]
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(OidcBrowserCallbackReviewError):
                    self.review.begin(self.start_request(**values))
        self.assertFalse(Path(self.db).exists())

    def test_cross_site_login_initiation_denied(self):
        for origin, fetch in (
            ("https://evil.test", "same-origin"),
            ("https://app.example.test", "cross-site"),
            ("null", "same-origin"),
            ("https://app.example.test:443", "same-origin"),
        ):
            with self.subTest(origin=origin, fetch=fetch):
                headers=[
                    ("host","app.example.test"),
                    ("origin",origin),
                    ("sec-fetch-site",fetch),
                    ("content-type","application/json"),
                ]
                with self.assertRaises(OidcBrowserCallbackReviewError):
                    self.review.begin(self.start_request(headers=headers))

    def test_identity_authorization_and_forwarded_headers_denied(self):
        for name in (
            "authorization", "x-user-id", "x-principal-id", "x-auth-epoch",
            "forwarded", "x-forwarded-host", "x-forwarded-proto",
            "x-original-url",
        ):
            with self.subTest(name=name):
                headers=[
                    ("host","app.example.test"),
                    ("origin","https://app.example.test"),
                    ("sec-fetch-site","same-origin"),
                    ("content-type","application/json"),
                    (name,"forged"),
                ]
                with self.assertRaises(OidcBrowserCallbackReviewError):
                    self.review.begin(self.start_request(headers=headers))

    def test_duplicate_headers_and_preset_cookie_denied_at_start(self):
        for name in ("host", "origin", "sec-fetch-site", "content-type"):
            headers=[
                ("host","app.example.test"),
                ("origin","https://app.example.test"),
                ("sec-fetch-site","same-origin"),
                ("content-type","application/json"),
            ]
            headers += [next(x for x in headers if x[0]==name)]
            with self.subTest(name=name):
                with self.assertRaises(OidcBrowserCallbackReviewError):
                    self.review.begin(self.start_request(headers=headers))
        headers=[
            ("host","app.example.test"),
            ("origin","https://app.example.test"),
            ("sec-fetch-site","same-origin"),
            ("content-type","application/json"),
            ("cookie","session=fake"),
        ]
        with self.assertRaises(OidcBrowserCallbackReviewError):
            self.review.begin(self.start_request(headers=headers))

    def test_mismatched_issuer_blocks_before_token_exchange(self):
        start, params=self.begin()
        for value in (
            "https://evil.test", "https://login.example.test/",
            "http://login.example.test", "https://login.example.test.evil",
            "",
        ):
            query=urlencode({"code":CODE,"state":params["state"][0],"iss":value})
            with self.subTest(issuer=value):
                self.denied(self.callback_request(start, params, query=query))
        self.assertEqual(self.exchange_log, [])
        self.assertEqual(self.complete(start, params).verified_subject.subject,
                         "opaque-synthetic-person")

    def test_missing_issuer_duplicate_state_and_unknown_query_key_denied(self):
        start, params=self.begin()
        base=urlencode({"code":CODE,"state":params["state"][0],"iss":ISSUER})
        for query in (
            urlencode({"code":CODE,"state":params["state"][0]}),
            base+"&state="+params["state"][0],
            base+"&code="+CODE,
            base+"&iss="+ISSUER,
            base+"&principal=attacker",
            base+"&error=access_denied",
            base+"&nonce=forged",
        ):
            with self.subTest(query=query[-36:]):
                self.denied(self.callback_request(start,params,query=query))
        self.assertEqual(self.exchange_log, [])

    def test_missing_cookie_or_duplicate_cookie_denied(self):
        start, params=self.begin()
        common=[("host","app.example.test")]
        self.denied(self.callback_request(start,params,headers=common))
        cookie=self.cookie_pair(start)
        self.denied(self.callback_request(start,params,headers=common+[
            ("cookie", cookie+"; "+cookie),
        ]))
        self.denied(self.callback_request(start,params,headers=common+[
            ("cookie",cookie),("cookie",cookie),
        ]))
        self.assertEqual(self.exchange_log, [])

    def test_cookie_signature_tamper_denied(self):
        start, params=self.begin()
        cookie=self.cookie_pair(start)
        changed=cookie[:-1]+("A" if cookie[-1]!="A" else "B")
        self.denied(self.callback_request(start,params,headers=[
            ("host","app.example.test"),("cookie",changed),
        ]))
        self.assertEqual(self.exchange_log, [])

    def test_cookie_for_other_hmac_key_is_not_accepted(self):
        start, params=self.begin()
        wrong=self.new_review(cookie_key=b"X"*48)
        self.denied(self.callback_request(start,params),review=wrong)
        self.assertEqual(self.exchange_log, [])

    def test_cookie_expiry_and_future_timestamp_denied(self):
        start, params=self.begin()
        self.now[0]+=181
        self.denied(self.callback_request(start,params))
        self.assertEqual(self.exchange_log, [])
        self.now[0]=99990
        self.denied(self.callback_request(start,params))
        self.assertEqual(self.exchange_log, [])

    def test_valid_samesite_lax_cross_site_navigation(self):
        start, params=self.begin()
        self.assertEqual(
            self.complete(start,params).verified_subject.subject,
            "opaque-synthetic-person",
        )

    def test_wrong_https_host_scheme_path_method_denied(self):
        start, params=self.begin()
        for kwargs in (
            {"scheme":"http"},{"path":"/callback/evil"},
        ):
            self.denied(self.callback_request(start,params,**kwargs))
        query=urlencode({"code":CODE,"state":params["state"][0],"iss":ISSUER})
        for host in ("evil.test","app.example.test:443"):
            headers=[
                ("host",host),("cookie",self.cookie_pair(start)),
            ]
            self.denied(self.callback_request(start,params,headers=headers,query=query))
        self.denied(self.request(
            method="POST",path="/callback/oidc",query=query,
            headers=[("host","app.example.test"),
                     ("cookie",self.cookie_pair(start))],
        ))
        self.assertEqual(self.exchange_log, [])

    def test_callback_origin_not_required_cross_site_but_forged_denied(self):
        start, params=self.begin()
        headers=[
            ("host","app.example.test"),
            ("cookie",self.cookie_pair(start)),
            ("origin","https://evil.test"),
        ]
        self.denied(self.callback_request(start,params,headers=headers))
        self.assertEqual(self.complete(start,params).verified_subject.subject,
                         "opaque-synthetic-person")

    def test_callback_query_encoding_duplicate_and_malformed_denied(self):
        start, params=self.begin()
        base=urlencode({"code":CODE,"state":params["state"][0],"iss":ISSUER})
        for query in (
            base+"%",base+"&foo",base+"%XX",
            base.replace("iss=", "i%73s=")+"&iss="+ISSUER,
            "code=CODE+bad&state="+params["state"][0]+"&iss=bad",
            "error=access_denied&state="+params["state"][0]+"&iss="+ISSUER,
        ):
            with self.subTest(query=query[:25]):
                self.denied(self.callback_request(start,params,query=query))
        self.assertEqual(self.exchange_log, [])

    def test_invalid_code_does_not_exchange_or_consume_state(self):
        start, params=self.begin()
        query=urlencode({
            "code":"x","state":params["state"][0],"iss":ISSUER,
        })
        self.denied(self.callback_request(start,params,query=query))
        self.assertEqual(self.exchange_log, [])
        self.assertEqual(self.complete(start,params).verified_subject.subject,
                         "opaque-synthetic-person")

    def test_wrong_browser_cookie_state_binding_refused(self):
        first, p1=self.begin()
        second, p2=self.begin()
        self.denied(self.callback_request(first,p2))
        self.assertEqual(self.exchange_log, [])
        self.current_nonce=p2["nonce"][0]
        self.assertEqual(self.complete(second,p2).verified_subject.subject,
                         "opaque-synthetic-person")

    def test_cross_instance_sqlite_state_oneuse(self):
        start, params=self.begin()
        self.assertEqual(
            self.complete(start,params,review=self.new_review()).verified_subject.subject,
            "opaque-synthetic-person",
        )
        with self.assertRaises(OidcBrowserCallbackReviewError):
            self.complete(start,params)

    def test_wrong_signed_nonce_burns_state_without_local_login(self):
        start, params=self.begin()
        self.exchange_mode="wrong_nonce"
        with self.assertRaises(OidcBrowserCallbackReviewError):
            self.complete(start,params)
        self.exchange_mode="valid"
        with self.assertRaises(OidcBrowserCallbackReviewError):
            self.complete(start,params)
        self.assertEqual(len(self.exchange_log),1)

    def test_failed_token_exchange_burns_state(self):
        start, params=self.begin()
        self.exchange_mode="failed"
        with self.assertRaises(OidcBrowserCallbackReviewError):
            self.complete(start,params)
        with self.assertRaises(OidcBrowserCallbackReviewError):
            self.complete(start,params)
        self.assertEqual(len(self.exchange_log),1)

    def test_two_independent_worker_objects_race_same_state(self):
        start, params=self.begin()
        barrier=threading.Barrier(3)
        accepted, rejected=[], []
        def run():
            barrier.wait(timeout=5)
            try:
                accepted.append(self.complete(
                    start,params,review=self.new_review(),
                ))
            except OidcBrowserCallbackReviewError:
                rejected.append(True)
        workers=[threading.Thread(target=run) for _ in range(2)]
        for thread in workers: thread.start()
        barrier.wait(timeout=5)
        for thread in workers:
            thread.join(timeout=7)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(accepted),1)
        self.assertEqual(len(rejected),1)
        self.assertEqual(len(self.exchange_log),1)

    def test_no_route_real_login_or_memory_authority(self):
        start,params=self.begin()
        self.assertFalse(hasattr(self.review,"app"))
        self.assertFalse(hasattr(self.review,"remember"))
        self.assertFalse(hasattr(self.review,"issue_browser_session"))
        self.assertFalse(hasattr(self.review,"commit"))
        self.assertFalse(start.may_issue_browser_session)
        self.assertFalse(start.may_write_memory)
        self.assertNotIn(COOKIE_KEY.decode(),repr(start))
        self.assertNotIn(params["state"][0],repr(start))


if __name__ == "__main__":
    unittest.main()
