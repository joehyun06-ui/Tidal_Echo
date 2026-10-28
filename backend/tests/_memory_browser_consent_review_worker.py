"""Offline browser consent/CSRF review: no HTTP, no actual memory write."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from backend.memory_auth_epoch_store import (
    AuthEpochRegistry, AuthenticatedLogin, ServerIssuedEpoch,
)
from backend.memory_browser_consent_review import (
    BrowserConsentReview, BrowserConsentReviewError,
    TrustedBrowserReviewContext,
)
from backend.memory_shared_confirmation_ticket import TrustedTicketContext
from backend.tests._memory_session_confirm_worker import valid_request
from backend.tests._support import NoNetworkMixin


class BrowserConsentSecurityTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = str(Path(temp.name) / "review.sqlite")
        patch = mock.patch.dict(os.environ, {
            "MEMORY_BROWSER_CONSENT_REVIEW_ENABLED": "true",
            "MEMORY_TRUSTED_AUTH_EPOCH_ENABLED": "true",
            "MEMORY_SHARED_CONFIRMATION_TICKETS_ENABLED": "true",
            "OPENAI_API_KEY": "",
        })
        patch.start()
        self.addCleanup(patch.stop)
        self.session = {
            "principal": "synthetic-user", "login": "L" * 32,
            "epoch": None, "id": "s1", "origin": "https://example.test",
            "csrf": "C" * 43,
        }
        self.registry = AuthEpochRegistry(
            path=self.db,
            identity_provider=lambda: AuthenticatedLogin(
                self.session["principal"], self.session["login"],
            ),
        )
        self.session["epoch"] = self.registry.issue_after_authenticated_login()
        self.request = valid_request()
        self.context = lambda: TrustedBrowserReviewContext(
            identity=TrustedTicketContext(
                principal_id=self.session["principal"],
                login_id=self.session["login"],
                auth_epoch=self.session["epoch"].value,
                session_id=self.session["id"],
                canonical_sessions=({"id": "s1"}, {"id": "s2"}),
            ),
            origin=self.session["origin"],
            csrf_token=self.session["csrf"],
        )
        self.review = BrowserConsentReview(
            context_provider=self.context,
            epoch_checker=lambda identity: self.registry.assert_current(
                ServerIssuedEpoch(identity.auth_epoch)
            ),
        )

    def prepare(self, **changes):
        return self.review.prepare(
            changes.pop("request", self.request),
            origin=changes.pop("origin", self.session["origin"]),
            csrf_token=changes.pop("csrf_token", self.session["csrf"]),
            **changes,
        )

    def confirm(self, preview, **changes):
        payload = {
            "nonce": preview.nonce,
            "content_sha256": preview.content_sha256,
            "confirmation_text": preview.confirmation_text,
            "origin": self.session["origin"],
            "csrf_token": self.session["csrf"],
            "confirmed": True,
        }
        payload.update(changes)
        return self.review.confirm(
            payload.pop("request", self.request), **payload,
        )

    def test_opt_in_disabled_by_default(self):
        del os.environ["MEMORY_BROWSER_CONSENT_REVIEW_ENABLED"]
        with self.assertRaises(BrowserConsentReviewError):
            self.prepare()

    def test_review_prepares_and_confirms_without_writer(self):
        preview = self.prepare()
        self.assertEqual(preview.preview_content, self.request.content)
        result = self.confirm(preview)
        self.assertEqual(result.request_id, self.request.request_id)
        self.assertNotIn(self.request.content, repr(preview))
        self.assertNotIn(self.request.request_id, repr(result))
        self.assertFalse(hasattr(self.review, "remember"))
        self.assertFalse(hasattr(self.review, "commit"))
        with self.assertRaises(BrowserConsentReviewError):
            self.confirm(preview)

    def test_wrong_csrf_never_prepares(self):
        with self.assertRaises(BrowserConsentReviewError):
            self.prepare(csrf_token="X"*43)

    def test_origin_must_match_exact_authenticated_server_origin(self):
        for origin in (
            "https://evil.example.test", "https://example.test.evil",
            "http://example.test", "https://example.test:443",
            "null", "https://example.test/path",
            "https://example.test@evil.test",
        ):
            with self.subTest(origin=origin):
                with self.assertRaises(BrowserConsentReviewError):
                    self.prepare(origin=origin)

    def test_no_http_except_explicit_loopback(self):
        self.session["origin"] = "http://example.test"
        with self.assertRaises(BrowserConsentReviewError):
            self.prepare()
        self.session["origin"] = "http://127.0.0.1:8765"
        self.assertIsNotNone(self.prepare())

    def test_reject_cookie_only_or_omitted_origin_or_token(self):
        for origin, csrf in (("", self.session["csrf"]), ("null", self.session["csrf"]),
                             (self.session["origin"], ""), (self.session["origin"], "wrong")):
            with self.subTest(origin=origin, csrf=bool(csrf)):
                with self.assertRaises(BrowserConsentReviewError):
                    self.prepare(origin=origin, csrf_token=csrf)

    def test_cross_account_switch_rejected(self):
        preview = self.prepare()
        self.session["principal"] = "synthetic-other"
        self.session["epoch"] = self.registry.issue_after_authenticated_login()
        with self.assertRaises(BrowserConsentReviewError):
            self.confirm(preview)

    def test_session_change_rejected(self):
        preview = self.prepare()
        self.session["id"] = "s2"
        with self.assertRaises(BrowserConsentReviewError):
            self.confirm(preview)

    def test_login_epoch_rotation_denies_old_consent(self):
        preview = self.prepare()
        self.session["epoch"] = self.registry.issue_after_authenticated_login()
        with self.assertRaises(BrowserConsentReviewError):
            self.confirm(preview)

    def test_logout_revocation_denies(self):
        preview = self.prepare()
        self.assertTrue(self.registry.revoke_before_logout())
        with self.assertRaises(BrowserConsentReviewError):
            self.confirm(preview)

    def test_csrf_rotation_denies_old_preview(self):
        preview = self.prepare()
        self.session["csrf"] = "D" * 43
        with self.assertRaises(BrowserConsentReviewError):
            self.confirm(preview)

    def test_wrong_csrf_on_confirm_consumes_one_use_nonce(self):
        preview = self.prepare()
        with self.assertRaises(BrowserConsentReviewError):
            self.confirm(preview, csrf_token="X" * 43)
        with self.assertRaises(BrowserConsentReviewError):
            self.confirm(preview)

    def test_confirmation_requires_explicit_boolean_and_phrase(self):
        for payload in (
            {"confirmed": False}, {"confirmed": "true"},
            {"confirmed": 1}, {"confirmation_text": "CONFIRM"},
            {"content_sha256": "0"*64},
        ):
            with self.subTest(payload=payload):
                preview = self.prepare()
                with self.assertRaises(BrowserConsentReviewError):
                    self.confirm(preview, **payload)

    def test_modified_content_does_not_match_preview(self):
        preview = self.prepare()
        with self.assertRaises(BrowserConsentReviewError):
            self.confirm(preview, request=valid_request(content="Altered"))
        with self.assertRaises(BrowserConsentReviewError):
            self.confirm(preview)

    def test_ttl_checked_at_confirm(self):
        now = [100.0]
        review = BrowserConsentReview(
            context_provider=self.context,
            epoch_checker=lambda identity: self.registry.assert_current(
                ServerIssuedEpoch(identity.auth_epoch),
            ),
            clock=lambda: now[0],
        )
        preview = review.prepare(
            self.request, origin=self.session["origin"], csrf_token=self.session["csrf"],
        )
        now[0] += 121
        with self.assertRaises(BrowserConsentReviewError):
            review.confirm(
                self.request, nonce=preview.nonce,
                content_sha256=preview.content_sha256,
                confirmation_text=preview.confirmation_text,
                origin=self.session["origin"], csrf_token=self.session["csrf"],
                confirmed=True,
            )

    def test_same_nonce_concurrent_confirm_only_one_wins(self):
        preview = self.prepare()
        success, rejected = [], []
        barrier = threading.Barrier(4)

        def attempt():
            barrier.wait(timeout=5)
            try:
                success.append(self.confirm(preview))
            except BrowserConsentReviewError:
                rejected.append(True)

        threads = [threading.Thread(target=attempt) for _ in range(3)]
        for t in threads:
            t.start()
        barrier.wait(timeout=5)
        for t in threads:
            t.join(timeout=5)
            self.assertFalse(t.is_alive())
        self.assertEqual(len(success), 1)
        self.assertEqual(len(rejected), 2)

    def test_capacity_is_fail_closed_without_content_storage(self):
        for i in range(8):
            self.prepare(request=valid_request(marker=chr(97+i)))
        with self.assertRaises(BrowserConsentReviewError):
            self.prepare(request=valid_request(marker="z"))
        # No memory SQLite writer was initialized and no memory tables exist.
        self.assertEqual(self.review._pending.__len__(), 8)
        self.assertFalse(any(
            hasattr(p, "content") or hasattr(p, "preview_content")
            for p in self.review._pending.values()
        ))

    def test_clear_pending_on_logout(self):
        preview = self.prepare()
        self.review.clear_for_logout()
        with self.assertRaises(BrowserConsentReviewError):
            self.confirm(preview)

    def test_fail_closed_on_untrusted_context_type(self):
        review = BrowserConsentReview(
            context_provider=lambda: {"principal": self.session["principal"]},
            epoch_checker=lambda _identity: None,
        )
        with self.assertRaises(BrowserConsentReviewError):
            review.prepare(
                self.request, origin=self.session["origin"],
                csrf_token=self.session["csrf"],
            )

    def test_denied_after_epoch_checker_changes_flag(self):
        def checker(identity):
            self.registry.assert_current(ServerIssuedEpoch(identity.auth_epoch))
            os.environ["MEMORY_BROWSER_CONSENT_REVIEW_ENABLED"] = "false"
        review = BrowserConsentReview(
            context_provider=self.context, epoch_checker=checker,
        )
        with self.assertRaises(BrowserConsentReviewError):
            review.prepare(
                self.request, origin=self.session["origin"],
                csrf_token=self.session["csrf"],
            )


if __name__ == "__main__":
    unittest.main()
