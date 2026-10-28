"""Real ASGI request/response, synthetic OIDC tokens, NO network sockets."""
from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest import mock

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from backend.memory_oidc_browser_callback_review import OidcBrowserCallbackOfflineReview
from backend.memory_oidc_isolated_asgi_review import (
    create_isolated_oidc_asgi_review,
)
from backend.tests._support import NoNetworkMixin

ISSUER = "https://login.example.test"
REDIRECT = "https://app.example.test/callback/oidc"
START = "/__offline_review__/oidc/start"
DONE = "/__offline_review__/oidc/done"
CNAME = "__Host-gt-oidc-review"


class IsolatedOidcAsgiHttpTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
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
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / "asgi-state.sqlite")
        flags = mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "",
            "MEMORY_OIDC_ISOLATED_ASGI_REVIEW_ENABLED": "true",
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
        self.exchange_calls = []
        self.exchange_mode = "valid"
        self.review = OidcBrowserCallbackOfflineReview(
            path=self.path,
            issuer=ISSUER,
            client_id="synthetic-oidc-client",
            authorization_endpoint=ISSUER+"/authorize",
            token_endpoint=ISSUER+"/token",
            redirect_uri=REDIRECT,
            cookie_hmac_key=b"H"*48,
            state_encryption_key=b"A"*32,
            pinned_public_keys={"kid-1":self.public},
            token_exchanger=self.exchange,
            clock=lambda:self.now[0],
        )
        self.app = create_isolated_oidc_asgi_review(self.review)

    async def asyncSetUp(self):
        self.client = self.make_client()
        await self.client.__aenter__()

    async def asyncTearDown(self):
        await self.client.__aexit__(None, None, None)

    def make_client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="https://app.example.test",
            follow_redirects=False,
        )

    @staticmethod
    def b64(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    def sign(self, nonce):
        claims = {
            "iss":ISSUER, "aud":"synthetic-oidc-client",
            "sub":"synthetic-private-identity",
            "nonce":nonce, "iat":99980,
            "exp":100180, "auth_time":99950,
        }
        head = {"alg":"RS256","kid":"kid-1","typ":"JWT"}
        signing = ".".join(self.b64(json.dumps(x,separators=(",",":")).encode())
                           for x in (head,claims))
        signature = self.key.sign(
            signing.encode(),padding.PKCS1v15(),hashes.SHA256(),
        )
        return signing+"."+self.b64(signature)

    def exchange(self, code, verifier, redirect, endpoint):
        self.exchange_calls.append((code, verifier, redirect, endpoint))
        if self.exchange_mode == "failure":
            raise RuntimeError("token=SECRET_FAKE")
        if self.exchange_mode == "wrong_nonce":
            return self.sign("X"*43)
        return self.sign(self.current_nonce)

    async def begin(self, *, client=None, headers=None, content=b"{}"):
        hdr = {
            "origin":"https://app.example.test",
            "sec-fetch-site":"same-origin",
            "content-type":"application/json",
        }
        if headers is not None:
            hdr = headers
        response = await (client or self.client).post(
            START,headers=hdr,content=content,
        )
        if response.status_code==303:
            params = parse_qs(urlsplit(response.headers["location"]).query)
            self.current_nonce = params["nonce"][0]
            return response, params
        return response, None

    async def callback(self, params, *, client=None, query=None, headers=None):
        if query is None:
            query=urlencode({
                "state":params["state"][0],
                "code":"CODE-synthetic-123",
                "iss":ISSUER,
            })
        return await (client or self.client).get(
            "/callback/oidc?"+query,
            headers=headers if headers is not None else {
                "sec-fetch-site":"cross-site",
                "sec-fetch-mode":"navigate",
                "sec-fetch-dest":"document",
            },
        )

    @staticmethod
    def assert_safe(response):
        for name in ("cache-control","pragma","referrer-policy",
                     "x-content-type-options","content-security-policy"):
            assert name in response.headers, name
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "no-store" in response.headers["cache-control"]

    async def test_real_asgi_cookie_redirect_verified_then_cleared(self):
        started, fields = await self.begin()
        self.assertEqual(started.status_code,303)
        self.assertEqual(urlsplit(started.headers["location"]).netloc,
                         "login.example.test")
        self.assertNotIn("synthetic-private-identity",str(started.headers))
        cookie=started.headers["set-cookie"]
        for word in ("Secure","HttpOnly","SameSite=Lax","Path=/","Max-Age=180"):
            self.assertIn(word,cookie)
        self.assertNotIn("Domain=",cookie)
        self.assertIn(CNAME,self.client.cookies)
        self.assert_safe(started)
        result=await self.callback(fields)
        self.assertEqual(result.status_code,303)
        self.assertEqual(result.headers["location"],DONE)
        self.assertIn("Max-Age=0",result.headers["set-cookie"])
        self.assertIn("HttpOnly",result.headers["set-cookie"])
        self.assertIn("Secure",result.headers["set-cookie"])
        self.assertNotIn(CNAME,self.client.cookies)
        self.assertEqual(len(self.exchange_calls),1)
        self.assert_safe(result)
        terminal=await self.client.get(DONE)
        self.assertEqual(terminal.status_code,200)
        self.assertIn("No user login or memory write",terminal.text)
        self.assertNotIn("synthetic-private-identity",terminal.text)
        self.assertNotIn(fields["state"][0],terminal.text)
        self.assert_safe(terminal)

    async def test_replay_after_cookie_cleared_cannot_exchange_again(self):
        _, fields=await self.begin()
        await self.callback(fields)
        replay=await self.callback(fields)
        self.assertEqual(replay.status_code,303)
        self.assertEqual(len(self.exchange_calls),1)
        self.assertEqual(replay.headers["location"],DONE)

    async def test_invalid_issuer_clears_cookie_before_any_exchange(self):
        _,fields=await self.begin()
        result=await self.callback(
            fields,query=urlencode({
                "state":fields["state"][0],
                "code":"CODE-synthetic-123",
                "iss":"https://evil.test",
            }),
        )
        self.assertEqual(result.status_code,303)
        self.assertEqual(result.headers["location"],DONE)
        self.assertNotIn(CNAME,self.client.cookies)
        self.assertEqual(self.exchange_calls,[])
        self.assert_safe(result)

    async def test_failed_token_exchange_burns_state_and_clears_cookie(self):
        _,fields=await self.begin()
        self.exchange_mode="failure"
        res=await self.callback(fields)
        self.assertEqual(res.status_code,303)
        self.assertEqual(len(self.exchange_calls),1)
        self.assertNotIn("SECRET",str(res.headers))
        self.exchange_mode="valid"
        await self.callback(fields)
        self.assertEqual(len(self.exchange_calls),1)

    async def test_wrong_nonce_signed_token_never_issues_cookie(self):
        _,fields=await self.begin()
        self.exchange_mode="wrong_nonce"
        res=await self.callback(fields)
        self.assertEqual(res.status_code,303)
        self.assertEqual(len(self.exchange_calls),1)
        self.assertNotIn("synthetic-private-identity",res.text)
        self.assertNotIn(CNAME,self.client.cookies)

    async def test_wrong_browser_cannot_complete_other_browser_state(self):
        _,fields=await self.begin()
        async with self.make_client() as second:
            res=await self.callback(fields,client=second)
            self.assertEqual(res.status_code,303)
            self.assertEqual(self.exchange_calls,[])
            # Second browser's failure doesn't consume the first browser state.
        self.assertEqual((await self.callback(fields)).status_code,303)
        self.assertEqual(len(self.exchange_calls),1)

    async def test_tampered_cookie_cannot_complete(self):
        started,fields=await self.begin()
        pair=started.headers["set-cookie"].split(";",1)[0]
        self.client.cookies.clear()
        bad=pair[:-2]+"XX"
        res=await self.callback(fields,headers={
            "cookie":bad,
            "sec-fetch-site":"cross-site",
            "sec-fetch-mode":"navigate",
            "sec-fetch-dest":"document",
        })
        self.assertEqual(res.status_code,303)
        self.assertEqual(self.exchange_calls,[])
        self.assertEqual(res.headers["location"],DONE)

    async def test_expired_browser_cookie_cannot_exchange(self):
        _,fields=await self.begin()
        self.now[0]+=181
        res=await self.callback(fields)
        self.assertEqual(res.status_code,303)
        self.assertEqual(self.exchange_calls,[])

    async def test_start_requires_exact_same_origin_and_no_proxy_headers(self):
        for name,value in (
            ("origin","https://evil.test"),
            ("sec-fetch-site","cross-site"),
            ("authorization","Bearer relay-shared-token"),
            ("x-principal-id","forged-user"),
            ("forwarded","host=evil.test"),
        ):
            with self.subTest(name=name):
                h={
                    "origin":"https://app.example.test",
                    "sec-fetch-site":"same-origin",
                    "content-type":"application/json",
                    name:value,
                }
                res,_=await self.begin(headers=h)
                self.assertEqual(res.status_code,400)
                self.assertNotIn("set-cookie",res.headers)
        self.assertEqual(self.exchange_calls,[])

    async def test_invalid_json_body_denied_before_prelogin_cookie(self):
        for payload in (b"",b"[]",b"{\"approved\":true}",b"x"*17,
                        b'{"principal":"a"}',b"{ }"):
            with self.subTest(payload=payload[:12]):
                resp,_=await self.begin(content=payload)
                self.assertEqual(resp.status_code,400)
                self.assertNotIn("set-cookie",resp.headers)
        self.assertFalse(Path(self.path).exists())

    async def test_duplicate_host_origin_and_cookie_deny(self):
        pairs=[
            ("origin","https://app.example.test"),
            ("sec-fetch-site","same-origin"),
            ("content-type","application/json"),
        ]
        res=await self.client.post(
            START,content=b"{}",headers=pairs+[
                ("origin","https://app.example.test"),
            ],
        )
        self.assertEqual(res.status_code,400)
        self.assertNotIn("set-cookie",res.headers)
        started,fields=await self.begin()
        cookie=started.headers["set-cookie"].split(";",1)[0]
        res=await self.callback(fields,headers=[
            ("cookie",cookie),("cookie",cookie),
            ("sec-fetch-site","cross-site"),
        ])
        self.assertEqual(res.status_code,303)
        self.assertEqual(len(self.exchange_calls),0)

    async def test_duplicate_state_and_invalid_query_clear_cookie(self):
        _,fields=await self.begin()
        qs=urlencode({"state":fields["state"][0],
                      "code":"CODE-synthetic-123","iss":ISSUER})
        resp=await self.callback(fields,query=qs+"&state="+fields["state"][0])
        self.assertEqual(resp.status_code,303)
        self.assertEqual(self.exchange_calls,[])
        self.assertNotIn(CNAME,self.client.cookies)

    async def test_callbacks_cannot_reflect_code_state_or_issuer(self):
        _,fields=await self.begin()
        code="CODE-private-secret-123456789"
        query=urlencode({
            "state":fields["state"][0],"code":code,"iss":ISSUER,
        })
        res=await self.callback(fields,query=query)
        self.assertEqual(res.status_code,303)
        self.assertNotIn(code,str(res.headers))
        self.assertNotIn(fields["state"][0],str(res.headers))
        self.assertNotIn(ISSUER,str(res.headers))
        self.assertNotIn("opaque",res.text)
        self.assertEqual(res.headers["location"],DONE)

    async def test_start_twice_with_existing_cookie_rejected(self):
        first,_=await self.begin()
        self.assertEqual(first.status_code,303)
        second,_=await self.begin()
        self.assertEqual(second.status_code,400)
        self.assertNotIn("set-cookie",second.headers)

    async def test_disabled_flag_blocks_all_routes_and_no_secret_state(self):
        os.environ["MEMORY_OIDC_ISOLATED_ASGI_REVIEW_ENABLED"]="false"
        res,_=await self.begin()
        self.assertEqual(res.status_code,404)
        self.assertNotIn("set-cookie",res.headers)
        self.assertEqual((await self.client.get("/callback/oidc")).status_code,404)
        self.assertEqual((await self.client.get(DONE)).status_code,404)
        self.assertFalse(Path(self.path).exists())

    async def test_real_network_sender_approval_flag_denies_isolated_app(self):
        os.environ["MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED"]="true"
        res,_=await self.begin()
        self.assertEqual(res.status_code,404)
        self.assertEqual(self.exchange_calls,[])

    async def test_https_sender_flag_denies_isolated_app(self):
        os.environ["MEMORY_OIDC_HTTPS_SENDER_REVIEW_ENABLED"]="true"
        res,_=await self.begin()
        self.assertEqual(res.status_code,404)
        self.assertEqual(self.exchange_calls,[])

    async def test_done_query_rejected_and_no_sensitive_information(self):
        x=await self.client.get(DONE+"?state=leak")
        self.assertEqual(x.status_code,400)
        self.assertNotIn("leak",x.text)
        self.assertNotIn("set-cookie",x.headers)
        self.assert_safe(x)

    async def test_wrong_method_path_never_uses_oauth_review(self):
        self.assertEqual((await self.client.get(START)).status_code,405)
        self.assertEqual((await self.client.post("/callback/oidc")).status_code,405)
        self.assertEqual((await self.client.get("/app/memory/write")).status_code,404)
        self.assertEqual((await self.client.get("/app/memory/session-write")).status_code,404)
        self.assertEqual(self.exchange_calls,[])

    async def test_rejected_host_header_start_does_not_issue_cookie(self):
        result,_=await self.begin(headers={
            "host":"evil.test",
            "origin":"https://app.example.test",
            "sec-fetch-site":"same-origin",
            "content-type":"application/json",
        })
        self.assertEqual(result.status_code,400)
        self.assertNotIn("set-cookie",result.headers)

    async def test_session_cookie_is_not_a_logged_in_identity(self):
        _,fields=await self.begin()
        self.assertTrue(self.client.cookies.get(CNAME))
        self.assertNotIn("gtmem",str(self.client.cookies))
        self.assertFalse(hasattr(self.app,"remember"))
        self.assertFalse(hasattr(self.app,"issue_browser_session"))
        self.assertEqual(len(self.app.routes),3)
        # A prelogin cookie can only bind the one-use state.
        self.assertEqual(self.exchange_calls,[])


if __name__=="__main__":
    unittest.main()
