"""Offline RSA-signed OIDC identity-token validation; no auth provider/network."""
from __future__ import annotations

import base64
import json
import os
import unittest
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from backend.memory_oidc_id_token_review import (
    OidcTokenReviewError, PinnedOidcIdTokenVerifier,
)
from backend.tests._support import NoNetworkMixin


class PinnedIdTokenVerificationTests(NoNetworkMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Private test key generated in memory, NEVER written to source.
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_pem = cls.private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        cls.alternate = rsa.generate_private_key(
            public_exponent=65537, key_size=2048,
        )
        cls.alternate_pem = cls.alternate.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def setUp(self):
        super().setUp()
        flags = mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "",
            "MEMORY_OIDC_ID_TOKEN_VERIFIER_REVIEW_ENABLED": "true",
        })
        flags.start()
        self.addCleanup(flags.stop)
        self.now = [100000.0]
        self.server_nonce = ["N" * 43]
        self.issuer = "https://login.example.test"
        self.client_id = "guitting-test-client"
        self.verifier = self.make_verifier()

    def make_verifier(self, *, keys=None, nonce=True, issuer=None,
                      client_id=None, clock=None):
        return PinnedOidcIdTokenVerifier(
            expected_issuer=issuer or self.issuer,
            client_id=client_id or self.client_id,
            pinned_public_keys=keys if keys is not None else {"key-1": self.public_pem},
            expected_nonce_provider=(
                (lambda: self.server_nonce[0]) if nonce else None
            ),
            clock=clock or (lambda: self.now[0]),
        )

    def claims(self, **changes):
        claims = dict(
            iss=self.issuer, aud=self.client_id,
            sub="opaque-provider-user", nonce="N" * 43,
            iat=99980, exp=100180, auth_time=99950,
        )
        claims.update(changes)
        return claims

    @staticmethod
    def b64(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    def sign(self, *, header=None, claims=None, raw_header=None,
             raw_claims=None, key=None):
        if raw_header is None:
            raw_header = json.dumps(
                header if header is not None else {
                    "alg": "RS256", "kid": "key-1", "typ": "JWT",
                }, separators=(",", ":"),
            )
        if raw_claims is None:
            raw_claims = json.dumps(
                claims if claims is not None else self.claims(),
                separators=(",", ":"),
            )
        signing = (self.b64(raw_header.encode("utf-8")) + "." +
                   self.b64(raw_claims.encode("utf-8")))
        signature = (key or self.private).sign(
            signing.encode("ascii"), padding.PKCS1v15(), hashes.SHA256(),
        )
        return signing + "." + self.b64(signature)

    def rejects(self, token, verifier=None):
        with self.assertRaises(OidcTokenReviewError):
            (verifier or self.verifier).verify(token)

    def test_real_rsa_signature_and_claims_are_verified(self):
        result = self.verifier.verify(self.sign())
        self.assertEqual(result.subject, "opaque-provider-user")
        self.assertEqual(result.issuer, self.issuer)
        self.assertEqual(result.authenticated_at, 99950)
        self.assertFalse(result.may_issue_browser_session)
        self.assertFalse(result.may_write_memory)
        self.assertNotIn(result.subject, repr(result))
        self.assertFalse(hasattr(self.verifier, "issue_browser_session"))
        self.assertFalse(hasattr(self.verifier, "remember"))

    def test_default_off_denies_even_valid_signed_token(self):
        token = self.sign()
        os.environ["MEMORY_OIDC_ID_TOKEN_VERIFIER_REVIEW_ENABLED"] = "false"
        self.rejects(token)

    def test_without_server_owned_nonce_provider_denies(self):
        self.rejects(self.sign(), self.make_verifier(nonce=False))

    def test_modified_payload_signature_is_rejected(self):
        original = self.sign()
        altered = original.split(".")
        altered[1] = self.b64(json.dumps(
            self.claims(sub="attacker"), separators=(",", ":"),
        ).encode())
        self.rejects(".".join(altered))

    def test_modified_header_signature_is_rejected(self):
        parts = self.sign().split(".")
        parts[0] = self.b64(json.dumps({
            "alg": "RS256", "kid": "key-1",
        }, separators=(",", ":")).encode())
        self.rejects(".".join(parts))

    def test_wrong_signer_cannot_use_pinned_kid(self):
        self.rejects(self.sign(key=self.alternate))

    def test_unknown_kid_cannot_trigger_remote_discovery(self):
        self.rejects(self.sign(header={"alg":"RS256", "kid":"unknown"}))

    def test_pinned_rotation_accepts_only_allowed_new_key(self):
        token = self.sign(
            header={"alg": "RS256", "kid": "key-2"},
            key=self.alternate,
        )
        self.rejects(token)
        both = self.make_verifier(keys={
            "key-1": self.public_pem, "key-2": self.alternate_pem,
        })
        self.assertEqual(both.verify(token).subject, "opaque-provider-user")
        only_new = self.make_verifier(keys={"key-2": self.alternate_pem})
        self.rejects(self.sign(), only_new)

    def test_alg_none_and_hmac_confusion_rejected(self):
        for alg in ("none", "HS256", "RS384", "PS256", ""):
            with self.subTest(alg=alg):
                self.rejects(self.sign(header={"alg":alg, "kid":"key-1"}))

    def test_remote_jwk_and_unapproved_jose_headers_rejected(self):
        for key in ("jwk", "jku", "x5u", "crit", "x5c", "zip"):
            with self.subTest(key=key):
                self.rejects(self.sign(
                    header={"alg":"RS256", "kid":"key-1", key:"evil"}
                ))

    def test_duplicate_json_header_or_claims_denied(self):
        self.rejects(self.sign(
            raw_header='{"alg":"RS256","kid":"key-1","alg":"none"}',
        ))
        self.rejects(self.sign(
            raw_claims=(
                '{"iss":"https://login.example.test","iss":"https://evil.test",'
                '"aud":"guitting-test-client","sub":"x","nonce":"' +
                "N" * 43 + '","iat":99980,"exp":100180,"auth_time":99950}'
            ),
        ))

    def test_bad_issuer_and_audience_rejected(self):
        for change in (
            {"iss":"https://evil.test"},
            {"iss":"https://login.example.test/"},
            {"aud":"another-client"},
            {"aud":["guitting-test-client"]},
            {"aud":["guitting-test-client", "evil-client"]},
            {"azp":"guitting-test-client"},
        ):
            with self.subTest(change=change):
                self.rejects(self.sign(claims=self.claims(**change)))

    def test_bad_sub_non_ascii_or_missing_rejected(self):
        for sub in ("", " ", "test user", "中文", None, True, 123):
            with self.subTest(sub=sub):
                self.rejects(self.sign(claims=self.claims(sub=sub)))

    def test_nonce_mismatch_or_missing_is_denied(self):
        token = self.sign()
        self.server_nonce[0] = "D" * 43
        self.rejects(token)
        self.server_nonce[0] = "N" * 43
        for nonce in (None, "", "n", False):
            self.rejects(self.sign(claims=self.claims(nonce=nonce)))

    def test_expired_or_invalid_lifetime_denied(self):
        for changes in (
            {"exp": 100000},
            {"exp": 99999},
            {"exp": 99980},
            {"exp": 101000},
            {"iat": 99800},
            {"iat": 100100},
            {"iat": 99980, "exp": 100500},
            {"iat": 99990, "exp": 99995},
        ):
            with self.subTest(changes=changes):
                self.rejects(self.sign(claims=self.claims(**changes)))

    def test_future_and_stale_authentication_denied(self):
        for changes in (
            {"auth_time": 99600},
            {"auth_time": 100100},
            {"auth_time": -1},
            {"auth_time": 0},
        ):
            with self.subTest(changes=changes):
                self.rejects(self.sign(claims=self.claims(**changes)))

    def test_future_auth_time_skew_cannot_stack_with_future_iat(self):
        # iat may be +30s due to clock skew; auth_time must not compound
        # another +30s on top of that.
        too_future = self.claims(iat=100030, auth_time=100060, exp=100180)
        self.rejects(self.sign(claims=too_future))
        within_skew = self.claims(iat=100030, auth_time=100030, exp=100180)
        self.assertEqual(
            self.verifier.verify(self.sign(claims=within_skew)).subject,
            "opaque-provider-user",
        )

    def test_security_numeric_dates_must_be_integer_not_bool_or_float(self):
        for field in ("iat", "exp", "auth_time"):
            for value in (True, 99980.0, "99980", None):
                with self.subTest(field=field,value=value):
                    self.rejects(self.sign(claims=self.claims(**{field:value})))

    def test_not_before_optional_but_checked_when_present(self):
        valid = self.sign(claims=self.claims(nbf=99980))
        self.assertEqual(self.verifier.verify(valid).subject, "opaque-provider-user")
        for value in (100100, -1, 100000.0, True, None):
            self.rejects(self.sign(claims=self.claims(nbf=value)))

    def test_invalid_clock_fails_closed(self):
        for value in (float("nan"), float("inf"), -1, "now"):
            self.rejects(self.sign(), self.make_verifier(clock=lambda v=value: v))

    def test_wrong_typ_and_missing_key_id_denied(self):
        for header in (
            {"alg": "RS256", "kid":"key-1", "typ":"at+jwt"},
            {"alg": "RS256"},
            {"alg": "RS256", "kid": 4},
            {"alg": "RS256", "kid": ""},
        ):
            self.rejects(self.sign(header=header))

    def test_multiple_or_malformed_segments_rejected(self):
        token = self.sign()
        for corrupted in (
            token + ".extra", "invalid", "", "a.b.c",
            token.split(".")[0] + "." + token.split(".")[1] + ".!",
            token.replace(".", "=", 1), "e30.e30.", " " + token,
            token + "A" * 9000,
        ):
            self.rejects(corrupted)

    def test_invalid_public_key_configuration_denied(self):
        for keys in (
            {}, {"key-1": b"not a key"}, {"": self.public_pem},
            {"key-1": b"private-key"}, {"key-1": self.public_pem} |
            {f"k{i}": self.public_pem for i in range(2, 7)},
        ):
            with self.subTest(keys=len(keys)):
                with self.assertRaises(OidcTokenReviewError):
                    self.make_verifier(keys=keys)

    def test_unexpected_issuer_configuration_denied(self):
        for issuer in (
            "http://login.example.test", "https://x@y", "https://a/b?query=1",
            "https://a/#foo", "https://login.example.test/",
        ):
            with self.assertRaises(OidcTokenReviewError):
                self.make_verifier(issuer=issuer)

    def test_valid_signature_never_claims_one_time_login_or_human_consent(self):
        token = self.sign()
        first = self.verifier.verify(token)
        # A signature checker is NOT a replay cache. The OAuth code+PKCE+
        # server nonce store still needs atomic consume before real login.
        second = self.verifier.verify(token)
        self.assertEqual(first, second)
        self.assertFalse(second.may_issue_browser_session)
        self.assertFalse(second.may_write_memory)

    def test_nonce_provider_not_called_before_bad_signature_rejected(self):
        calls = []
        verifier = PinnedOidcIdTokenVerifier(
            expected_issuer=self.issuer, client_id=self.client_id,
            pinned_public_keys={"key-1": self.public_pem},
            expected_nonce_provider=lambda: calls.append("called") or "N"*43,
            clock=lambda: self.now[0],
        )
        self.rejects(self.sign(key=self.alternate), verifier)
        self.assertEqual(calls, [])

    def test_runtime_disable_during_nonce_lookup_denies(self):
        def revoked():
            os.environ["MEMORY_OIDC_ID_TOKEN_VERIFIER_REVIEW_ENABLED"] = "false"
            return "N" * 43
        verifier = PinnedOidcIdTokenVerifier(
            expected_issuer=self.issuer, client_id=self.client_id,
            pinned_public_keys={"key-1": self.public_pem},
            expected_nonce_provider=revoked,
            clock=lambda: self.now[0],
        )
        self.rejects(self.sign(), verifier)


if __name__ == "__main__":
    unittest.main()
