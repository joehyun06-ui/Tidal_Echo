"""Synthetic real-RSA OIDC code/PKCE + durable one-use state (no network)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from backend.memory_oidc_pkce_flow_review import (
    OidcCodePkceOfflineReview, OidcPkceReviewError,
)
from backend.tests._support import NoNetworkMixin

_TEST_KEY = b"Synthetic-only-AES-256-key-000000"
_ISSUER = "https://login.example.test"
_REDIRECT = "https://app.example.test/callback/oidc"
_AUTH = _ISSUER + "/authorize"
_TOKEN = _ISSUER + "/token"
_CLIENT = "synthetic-oidc-client"


class OidcPkceFlowTests(NoNetworkMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pub = cls.key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        cls.other = rsa.generate_private_key(
            public_exponent=65537, key_size=2048,
        )

    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = str(Path(temp.name) / "oidc-pkce.sqlite")
        flags = mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "",
            "MEMORY_OIDC_CODE_PKCE_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_ID_TOKEN_VERIFIER_REVIEW_ENABLED": "true",
        })
        flags.start()
        self.addCleanup(flags.stop)
        self.now = [100000.0]
        self.binding = ["B" * 43]
        self.nonce = ""
        self.exchange_log = []
        self.exchange_behavior = "valid"
        self.store = self.new_store()

    @staticmethod
    def b64(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    def sign(self, *, nonce=None, key=None, overrides=None):
        claims = dict(
            iss=_ISSUER, aud=_CLIENT, sub="test-opaque-user",
            nonce=self.nonce if nonce is None else nonce,
            iat=99980, exp=100180, auth_time=99950,
        )
        claims.update(overrides or {})
        header = {"alg": "RS256", "kid": "test-key-1", "typ": "JWT"}
        sections = ".".join(
            self.b64(json.dumps(item, separators=(",", ":")).encode("ascii"))
            for item in (header, claims)
        )
        signature = (key or self.key).sign(
            sections.encode("ascii"), padding.PKCS1v15(), hashes.SHA256(),
        )
        return sections + "." + self.b64(signature)

    def exchange(self, code, verifier, redirect_uri, token_endpoint):
        self.exchange_log.append((code, verifier, redirect_uri, token_endpoint))
        if self.exchange_behavior == "exception":
            raise RuntimeError("synthetic_exchange_failed")
        if self.exchange_behavior == "invalid_signature":
            return self.sign(key=self.other)
        if self.exchange_behavior == "wrong_nonce":
            return self.sign(nonce="X" * 43)
        if self.exchange_behavior == "missing_id_token":
            return None
        if self.exchange_behavior == "disable":
            os.environ["MEMORY_OIDC_CODE_PKCE_REVIEW_ENABLED"] = "false"
        return self.sign()

    def new_store(self, *, key=_TEST_KEY, bind=True, exchange=True,
                  auth=_AUTH, token=_TOKEN, redirect=_REDIRECT, issuer=_ISSUER):
        return OidcCodePkceOfflineReview(
            path=self.db, issuer=issuer, client_id=_CLIENT,
            authorization_endpoint=auth, token_endpoint=token,
            redirect_uri=redirect,
            browser_binding_provider=(lambda: self.binding[0]) if bind else None,
            token_exchanger=self.exchange if exchange else None,
            pinned_public_keys={"test-key-1": self.pub},
            at_rest_key=key,
            clock=lambda: self.now[0],
        )

    def prepare(self, store=None):
        plan = (store or self.store).prepare()
        params = parse_qs(urlsplit(plan.authorize_url).query)
        self.nonce = params["nonce"][0]
        return plan, params

    def finish(self, params, *, store=None, state=None, code="CODE-synthetic-123"):
        return (store or self.store).complete(
            state=params["state"][0] if state is None else state, code=code,
        )

    def rows(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(
                "SELECT state_sha256,binding_sha256,nonce_sha256,sealed_secret,"
                "created_at,expires_at,consumed_at "
                "FROM memory_oidc_pkce_attempt_review_v1"
            ).fetchall()

    def test_prepares_strict_s256_authorization_request(self):
        plan, p = self.prepare()
        self.assertEqual(urlsplit(plan.authorize_url).scheme, "https")
        for name, expected in (
            ("response_type", "code"), ("client_id", _CLIENT),
            ("redirect_uri", _REDIRECT), ("scope", "openid"),
            ("code_challenge_method", "S256"),
        ):
            self.assertEqual(p[name], [expected])
        self.assertEqual(len(p["code_challenge"][0]), 43)
        self.assertEqual(len(p["state"][0]), 43)
        self.assertEqual(len(p["nonce"][0]), 43)
        self.assertNotIn("code_verifier", p)
        self.assertEqual(plan.expires_in_seconds, 180)
        self.assertFalse(plan.may_issue_browser_session)
        self.assertFalse(plan.may_write_memory)

    def test_success_verifies_real_rsa_signature_but_issues_no_login(self):
        plan, p = self.prepare()
        result = self.finish(p)
        self.assertEqual(result.subject, "test-opaque-user")
        self.assertFalse(result.may_issue_browser_session)
        self.assertFalse(result.may_write_memory)
        self.assertEqual(len(self.exchange_log), 1)
        code, verifier, redirect, token = self.exchange_log[0]
        self.assertEqual(code, "CODE-synthetic-123")
        self.assertEqual(token, _TOKEN)
        self.assertEqual(redirect, _REDIRECT)
        self.assertEqual(
            self.b64(hashlib.sha256(verifier.encode()).digest()),
            p["code_challenge"][0],
        )
        self.assertIsNone(self.rows()[0][3])
        self.assertIsNotNone(self.rows()[0][6])
        self.assertFalse(hasattr(self.store, "remember"))
        self.assertFalse(hasattr(self.store, "issue_browser_session"))

    def test_secrets_absent_in_plaintext_database_fields(self):
        _, p = self.prepare()
        row = str(self.rows())
        for secret in (
            p["state"][0], p["nonce"][0], self.binding[0],
            _TEST_KEY.decode("ascii"),
        ):
            self.assertNotIn(secret, row)
        self.assertEqual(len(self.rows()[0][3]) > 100, True)

    def test_replay_same_state_never_reexchanges_code(self):
        _, p = self.prepare()
        self.finish(p)
        with self.assertRaises(OidcPkceReviewError):
            self.finish(p)
        self.assertEqual(len(self.exchange_log), 1)

    def test_new_instance_recovers_challenge_from_sqlite(self):
        _, p = self.prepare()
        verified = self.finish(p, store=self.new_store())
        self.assertEqual(verified.subject, "test-opaque-user")
        with self.assertRaises(OidcPkceReviewError):
            self.finish(p, store=self.new_store())

    def test_wrong_binding_denied_without_consuming(self):
        _, p = self.prepare()
        self.binding[0] = "D" * 43
        with self.assertRaises(OidcPkceReviewError):
            self.finish(p, store=self.new_store())
        self.assertIsNone(self.rows()[0][6])
        self.binding[0] = "B" * 43
        self.assertEqual(self.finish(p).subject, "test-opaque-user")

    def test_malformed_state_or_code_never_calls_exchange(self):
        _, p = self.prepare()
        for state in ("", "x", "X" * 43, "a?b", None, True):
            with self.subTest(state=state):
                with self.assertRaises(OidcPkceReviewError):
                    self.finish(p, state=state)
        for code in ("", "x", "bad code", "x"*513, None, 3):
            with self.subTest(code=code):
                with self.assertRaises(OidcPkceReviewError):
                    self.finish(p, code=code)
        self.assertEqual(self.exchange_log, [])
        self.assertIsNone(self.rows()[0][6])

    def test_missing_browser_binding_provider_fails_closed(self):
        store = self.new_store(bind=False)
        with self.assertRaises(OidcPkceReviewError):
            store.prepare()
        _, p = self.prepare()
        with self.assertRaises(OidcPkceReviewError):
            self.finish(p, store=store)
        self.assertEqual(self.exchange_log, [])

    def test_missing_exchange_adapter_cannot_claim(self):
        _, p = self.prepare()
        with self.assertRaises(OidcPkceReviewError):
            self.finish(p, store=self.new_store(exchange=False))
        self.assertIsNone(self.rows()[0][6])

    def test_default_off_creates_no_new_table(self):
        os.environ["MEMORY_OIDC_CODE_PKCE_REVIEW_ENABLED"] = "false"
        with self.assertRaises(OidcPkceReviewError):
            self.store.prepare()
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE "
                "name='memory_oidc_pkce_attempt_review_v1'"
            ).fetchone())

    def test_verifier_flag_disabled_fails_closed(self):
        os.environ["MEMORY_OIDC_ID_TOKEN_VERIFIER_REVIEW_ENABLED"] = "false"
        with self.assertRaises(OidcPkceReviewError):
            self.store.prepare()

    def test_ttl_expired_rejects_before_exchange(self):
        _, p = self.prepare()
        self.now[0] += 181
        with self.assertRaises(OidcPkceReviewError):
            self.finish(p)
        self.assertEqual(self.exchange_log, [])
        self.assertIsNone(self.rows()[0][6])

    def test_same_browser_concurrent_race_only_one_exchanges(self):
        _, p = self.prepare()
        barrier = threading.Barrier(3)
        yes, no = [], []
        def run():
            barrier.wait(timeout=6)
            try:
                yes.append(self.finish(p, store=self.new_store()))
            except OidcPkceReviewError:
                no.append("rejected")
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads: thread.start()
        barrier.wait(timeout=6)
        for thread in threads:
            thread.join(timeout=7)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(yes), 1)
        self.assertEqual(no, ["rejected"])
        self.assertEqual(len(self.exchange_log), 1)

    def test_failed_exchange_burns_state(self):
        _, p = self.prepare()
        self.exchange_behavior = "exception"
        with self.assertRaises(OidcPkceReviewError):
            self.finish(p)
        self.assertIsNotNone(self.rows()[0][6])
        self.assertIsNone(self.rows()[0][3])
        self.exchange_behavior = "valid"
        with self.assertRaises(OidcPkceReviewError):
            self.finish(p)
        self.assertEqual(len(self.exchange_log), 1)

    def test_bad_signer_burns_state_without_verified_identity(self):
        _, p = self.prepare()
        self.exchange_behavior = "invalid_signature"
        with self.assertRaises(OidcPkceReviewError):
            self.finish(p)
        self.assertIsNotNone(self.rows()[0][6])
        self.assertIsNone(self.rows()[0][3])
        with self.assertRaises(OidcPkceReviewError):
            self.finish(p)

    def test_wrong_signed_nonce_burns_state(self):
        _, p = self.prepare()
        self.exchange_behavior = "wrong_nonce"
        with self.assertRaises(OidcPkceReviewError):
            self.finish(p)
        self.assertIsNotNone(self.rows()[0][6])

    def test_missing_id_token_burns_state(self):
        _, p = self.prepare()
        self.exchange_behavior = "missing_id_token"
        with self.assertRaises(OidcPkceReviewError):
            self.finish(p)
        self.assertIsNotNone(self.rows()[0][6])

    def test_feature_disabled_after_exchange_does_not_grant_identity(self):
        _, p = self.prepare()
        self.exchange_behavior = "disable"
        with self.assertRaises(OidcPkceReviewError):
            self.finish(p)
        self.assertIsNotNone(self.rows()[0][6])

    def test_tampered_ciphertext_cannot_be_consumed(self):
        _, p = self.prepare()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "UPDATE memory_oidc_pkce_attempt_review_v1 "
                "SET sealed_secret=x'00010203040506070809101112' "
            )
            conn.commit()
        with self.assertRaises(OidcPkceReviewError):
            self.finish(p)
        self.assertIsNone(self.rows()[0][6])
        self.assertEqual(self.exchange_log, [])

    def test_wrong_runtime_encryption_key_refuses_decrypt(self):
        _, p = self.prepare()
        with self.assertRaises(OidcPkceReviewError):
            self.finish(p, store=self.new_store(key=b"X" * 32))
        self.assertIsNone(self.rows()[0][6])
        self.assertEqual(self.finish(p).subject, "test-opaque-user")

    def test_independently_pinned_bad_endpoints_refused(self):
        for changes in (
            {"auth":"http://login.example.test/authorize"},
            {"auth":"https://evil.test/authorize"},
            {"token":"https://evil.test/token"},
            {"token":"https://login.example.test/token?redirect=x"},
            {"redirect":"https://app.example.test@evil.test/callback"},
            {"redirect":"https://app.example.test/callback#fragment"},
            {"issuer":"https://evil.test"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(OidcPkceReviewError):
                    self.new_store(**changes)

    def test_wrong_crypto_key_configuration_refused(self):
        for val in (b"short", b"X" * 16, b"Y" * 64, "X" * 32):
            with self.assertRaises(OidcPkceReviewError):
                self.new_store(key=val)

    def test_disabled_during_browser_binding_callback_rolls_back(self):
        def disable():
            os.environ["MEMORY_OIDC_CODE_PKCE_REVIEW_ENABLED"] = "false"
            return "B" * 43
        store = OidcCodePkceOfflineReview(
            path=self.db, issuer=_ISSUER, client_id=_CLIENT,
            authorization_endpoint=_AUTH, token_endpoint=_TOKEN,
            redirect_uri=_REDIRECT,
            browser_binding_provider=disable,
            token_exchanger=self.exchange,
            pinned_public_keys={"test-key-1": self.pub},
            at_rest_key=_TEST_KEY, clock=lambda: self.now[0],
        )
        with self.assertRaises(OidcPkceReviewError):
            store.prepare()
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE name='memory_oidc_pkce_attempt_review_v1'"
            ).fetchone())

    def test_two_unique_authorization_attempts_are_separate(self):
        first, p1 = self.prepare()
        second, p2 = self.prepare()
        self.assertNotEqual(p1["state"], p2["state"])
        self.assertNotEqual(p1["nonce"], p2["nonce"])
        self.assertNotEqual(p1["code_challenge"], p2["code_challenge"])
        self.assertEqual(len(self.rows()), 2)
        # Finish the second. First token flow must use FIRST nonce, not the
        # latest value from another request in the same test object.
        self.finish(p2)
        with self.assertRaises(OidcPkceReviewError):
            self.finish(p1)
        self.assertEqual(len(self.rows()), 2)

    def test_untrusted_browser_binding_is_never_relay_token(self):
        _, p = self.prepare()
        self.binding[0] = "legacy-shared-relay-secret"
        with self.assertRaises(OidcPkceReviewError):
            self.finish(p)
        self.assertEqual(self.exchange_log, [])

    def test_failed_previous_state_can_start_fresh_attempt(self):
        _, p = self.prepare()
        self.exchange_behavior = "exception"
        with self.assertRaises(OidcPkceReviewError):
            self.finish(p)
        self.exchange_behavior = "valid"
        _, p2 = self.prepare()
        self.assertEqual(self.finish(p2).subject, "test-opaque-user")
        self.assertEqual(len(self.rows()), 2)


if __name__ == "__main__":
    unittest.main()
