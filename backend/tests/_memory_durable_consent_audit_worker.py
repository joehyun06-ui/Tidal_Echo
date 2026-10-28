"""Synthetic durable observed-event review, never a browser/consent authority."""
from __future__ import annotations

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
from unittest import mock

from backend.memory_auth_epoch_store import AuthEpochRegistry, AuthenticatedLogin
from backend.memory_browser_consent_review import TrustedBrowserReviewContext
from backend.memory_durable_consent_audit import (
    DurableObservedConsentAudit, DurableConsentAuditError,
    TrustedObservedInteraction,
)
from backend.memory_shared_confirmation_ticket import TrustedTicketContext
from backend.tests._memory_session_confirm_worker import valid_request
from backend.tests._support import NoNetworkMixin

TEST_SECRET = b"Synthetic-only-durable-audit-HMAC-2026-test-key-abcdefgh"


class DurableObservedAuditTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / "synthetic-audit.sqlite")
        patch = mock.patch.dict(os.environ, {
            "MEMORY_DURABLE_CONSENT_AUDIT_REVIEW_ENABLED": "true",
            "MEMORY_TRUSTED_AUTH_EPOCH_ENABLED": "true",
            "MEMORY_BROWSER_CONSENT_REVIEW_ENABLED": "true",
            "OPENAI_API_KEY": "",
        })
        patch.start()
        self.addCleanup(patch.stop)
        self.state = dict(
            principal="synthetic-user", login="L" * 32,
            session="s1", epoch=None, origin="https://example.test",
            csrf="C" * 43, action="approve", nonce="", event_ref="E" * 43,
        )
        self.now = [1000.0]
        self.registry = AuthEpochRegistry(
            path=self.path,
            identity_provider=lambda: AuthenticatedLogin(
                self.state["principal"], self.state["login"],
            ),
        )
        self.state["epoch"] = self.registry.issue_after_authenticated_login()
        self.request = valid_request()
        self.context = lambda: TrustedBrowserReviewContext(
            identity=TrustedTicketContext(
                principal_id=self.state["principal"],
                login_id=self.state["login"],
                auth_epoch=self.state["epoch"].value,
                session_id=self.state["session"],
                canonical_sessions=({"id": "s1"}, {"id": "s2"}),
            ),
            origin=self.state["origin"], csrf_token=self.state["csrf"],
        )
        self.observer = lambda: TrustedObservedInteraction(
            nonce=self.state["nonce"],
            action=self.state["action"],
            event_ref=self.state["event_ref"],
        )
        self.store = self.new_store()

    def new_store(self, key=TEST_SECRET):
        return DurableObservedConsentAudit(
            path=self.path, context_provider=self.context,
            interaction_provider=self.observer,
            hmac_secret=key, clock=lambda: self.now[0],
        )

    def prepare(self, request=None, **changes):
        return self.store.prepare(
            request or self.request,
            origin=changes.get("origin", self.state["origin"]),
            csrf_token=changes.get("csrf_token", self.state["csrf"]),
        )

    def observe(self, offer, request=None, store=None, **changes):
        args = {
            "nonce": offer.nonce,
            "content_sha256": offer.content_sha256,
            "confirmation_text": offer.confirmation_text,
            "origin": self.state["origin"],
            "csrf_token": self.state["csrf"],
        }
        args.update(changes)
        return (store or self.store).observe(request or self.request, **args)

    def set_event(self, offer, action="approve", ref="E" * 43):
        self.state["nonce"] = offer.nonce
        self.state["action"] = action
        self.state["event_ref"] = ref

    def count(self, table):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def test_default_off_prepares_nothing(self):
        os.environ.pop("MEMORY_DURABLE_CONSENT_AUDIT_REVIEW_ENABLED")
        with self.assertRaises(DurableConsentAuditError):
            self.prepare()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE "
                             "name='memory_consent_offer_audit_v1'").fetchone()[0],
                0,
            )

    def test_approved_event_is_signed_and_readable_from_new_instance(self):
        offer = self.prepare()
        self.set_event(offer)
        result = self.observe(offer)
        self.assertEqual(result.action, "approve")
        self.assertEqual(self.count("memory_consent_offer_audit_v1"), 1)
        self.assertEqual(self.count("memory_consent_event_audit_v1"), 1)
        verifier = self.new_store()
        self.assertEqual(
            verifier.verify_historical(self.request, nonce=offer.nonce).action,
            "approve",
        )
        self.assertFalse(hasattr(self.store, "commit"))
        self.assertFalse(hasattr(self.store, "remember"))

    def test_decline_is_separate_signed_terminal_not_authorization(self):
        offer = self.prepare()
        self.set_event(offer, action="decline")
        self.assertEqual(self.observe(offer).action, "decline")
        self.assertEqual(
            self.new_store().verify_historical(self.request, nonce=offer.nonce).action,
            "decline",
        )

    def test_pending_stores_no_content_token_csrf_or_request_id(self):
        offer = self.prepare()
        with closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute(
                "SELECT * FROM memory_consent_offer_audit_v1"
            ).fetchone()
            snapshot = str(row)
        for secret in (
            self.request.content, self.request.request_id, offer.nonce,
            self.state["csrf"], self.state["origin"], TEST_SECRET.decode(),
        ):
            self.assertNotIn(secret, snapshot)
        self.assertNotIn(self.request.content, repr(offer))
        self.assertEqual(self.count("memory_consent_event_audit_v1"), 0)

    def test_replayed_event_rejected(self):
        offer = self.prepare()
        self.set_event(offer)
        self.observe(offer)
        with self.assertRaises(DurableConsentAuditError):
            self.observe(offer)
        self.assertEqual(self.count("memory_consent_event_audit_v1"), 1)

    def test_two_independent_instances_race_only_one_wins(self):
        offer = self.prepare()
        self.set_event(offer)
        barrier = threading.Barrier(3)
        accepted, declined = [], []
        stores = [self.new_store(), self.new_store()]

        def run(store):
            barrier.wait(timeout=4)
            try:
                accepted.append(self.observe(offer, store=store).action)
            except DurableConsentAuditError:
                declined.append("rejected")

        workers = [threading.Thread(target=run, args=(store,)) for store in stores]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=4)
        for worker in workers:
            worker.join(timeout=6)
            self.assertFalse(worker.is_alive())
        self.assertEqual(accepted, ["approve"])
        self.assertEqual(declined, ["rejected"])

    def test_wrong_origin_or_csrf_prepare_rejected(self):
        with self.assertRaises(DurableConsentAuditError):
            self.prepare(origin="https://evil.example.test")
        with self.assertRaises(DurableConsentAuditError):
            self.prepare(csrf_token="X" * 43)
        self.assertEqual(self.count("memory_consent_offer_audit_v1"), 0)

    def test_wrong_origin_or_csrf_does_not_record(self):
        offer = self.prepare()
        self.set_event(offer)
        for kwargs in (
            {"origin": "https://example.test.evil"},
            {"csrf_token": "X" * 43},
        ):
            with self.assertRaises(DurableConsentAuditError):
                self.observe(offer, **kwargs)
        self.assertEqual(self.count("memory_consent_event_audit_v1"), 0)

    def test_cross_account_session_and_login_epoch_rejected(self):
        cases = ["principal", "session", "epoch"]
        for name in cases:
            with self.subTest(name=name):
                offer = self.prepare()
                self.set_event(offer, ref="R" * 43)
                previous = dict(self.state)
                if name == "principal":
                    self.state["principal"] = "synthetic-other"
                    self.state["epoch"] = self.registry.issue_after_authenticated_login()
                elif name == "session":
                    self.state["session"] = "s2"
                else:
                    self.state["epoch"] = self.registry.issue_after_authenticated_login()
                with self.assertRaises(DurableConsentAuditError):
                    self.observe(offer)
                self.state.update(previous)
                if name in ("principal", "epoch"):
                    self.state["epoch"] = self.registry.issue_after_authenticated_login()
        self.assertEqual(self.count("memory_consent_event_audit_v1"), 0)

    def test_revoked_epoch_blocks_future_event(self):
        offer = self.prepare()
        self.set_event(offer)
        self.assertTrue(self.registry.revoke_before_logout())
        with self.assertRaises(DurableConsentAuditError):
            self.observe(offer)
        self.assertEqual(self.count("memory_consent_event_audit_v1"), 0)

    def test_ttl_checked_while_holding_sqlite_lock(self):
        offer = self.prepare()
        self.set_event(offer)
        self.now[0] += 121
        with self.assertRaises(DurableConsentAuditError):
            self.observe(offer)
        self.assertEqual(self.count("memory_consent_event_audit_v1"), 0)

    def test_changed_request_or_digest_refused(self):
        offer = self.prepare()
        self.set_event(offer)
        with self.assertRaises(DurableConsentAuditError):
            self.observe(offer, request=valid_request(content="Changed content"))
        with self.assertRaises(DurableConsentAuditError):
            self.observe(offer, content_sha256="0" * 64)
        with self.assertRaises(DurableConsentAuditError):
            self.observe(offer, confirmation_text="CONFIRM")
        self.assertEqual(self.count("memory_consent_event_audit_v1"), 0)

    def test_observer_must_be_server_event_not_boolean_or_mapping(self):
        offer = self.prepare()
        for output in (
            True, {"approved": True},
            TrustedObservedInteraction(offer.nonce, "approve", "bad"),
            TrustedObservedInteraction("X" * 43, "approve", "R" * 43),
        ):
            with self.subTest(output=type(output).__name__):
                bad = DurableObservedConsentAudit(
                    path=self.path, context_provider=self.context,
                    interaction_provider=lambda value=output: value,
                    hmac_secret=TEST_SECRET, clock=lambda: self.now[0],
                )
                with self.assertRaises(DurableConsentAuditError):
                    self.observe(offer, store=bad)
        self.assertEqual(self.count("memory_consent_event_audit_v1"), 0)

    def test_event_ref_cannot_be_reused_for_another_nonce(self):
        first = self.prepare()
        self.set_event(first)
        self.observe(first)
        second = self.prepare()
        self.set_event(second)
        with self.assertRaises(DurableConsentAuditError):
            self.observe(second)
        self.assertEqual(self.count("memory_consent_event_audit_v1"), 1)

    def test_sqlite_triggers_reject_update_and_delete(self):
        offer = self.prepare()
        self.set_event(offer)
        self.observe(offer)
        for table in ("memory_consent_offer_audit_v1", "memory_consent_event_audit_v1"):
            with self.subTest(table=table):
                with closing(sqlite3.connect(self.path)) as conn:
                    with self.assertRaisesRegex(
                        sqlite3.IntegrityError, "memory_consent_audit_immutable",
                    ):
                        conn.execute("UPDATE " + table + " SET nonce_sha256='bad'")
                    conn.rollback()
                    with self.assertRaisesRegex(
                        sqlite3.IntegrityError, "memory_consent_audit_immutable",
                    ):
                        conn.execute("DELETE FROM " + table)
                    conn.rollback()

    def test_wrong_runtime_hmac_key_cannot_verify(self):
        offer = self.prepare()
        self.set_event(offer)
        self.observe(offer)
        with self.assertRaises(DurableConsentAuditError):
            self.new_store(key=b"D" * 48).verify_historical(
                self.request, nonce=offer.nonce,
            )

    def test_historical_audit_remains_readable_after_logout(self):
        offer = self.prepare()
        self.set_event(offer)
        self.observe(offer)
        self.assertTrue(self.registry.revoke_before_logout())
        self.assertEqual(
            self.store.verify_historical(self.request, nonce=offer.nonce).action,
            "approve",
        )

    def test_flag_disabled_after_offer_denies_event(self):
        offer = self.prepare()
        self.set_event(offer)
        os.environ["MEMORY_DURABLE_CONSENT_AUDIT_REVIEW_ENABLED"] = "false"
        with self.assertRaises(DurableConsentAuditError):
            self.observe(offer)
        self.assertEqual(self.count("memory_consent_event_audit_v1"), 0)

    def test_separate_python_process_verifies_persisted_event(self):
        offer = self.prepare()
        self.set_event(offer)
        self.observe(offer)
        env = dict(os.environ)
        env["OPENAI_API_KEY"] = ""
        result = subprocess.run(
            [sys.executable, "-m", "backend.tests._memory_durable_consent_child"],
            input=json.dumps({
                "db": self.path, "nonce": offer.nonce,
                "secret_hex": TEST_SECRET.hex(),
            }), text=True, capture_output=True,
            env=env, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        self.assertEqual(result.stdout.strip(), "verified:approve")

    def test_wrong_request_after_signed_event_fails_closed(self):
        offer = self.prepare()
        self.set_event(offer)
        self.observe(offer)
        with self.assertRaises(DurableConsentAuditError):
            self.store.verify_historical(
                valid_request(content="Different"), nonce=offer.nonce,
            )


if __name__ == "__main__":
    unittest.main()
