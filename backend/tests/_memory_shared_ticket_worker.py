"""Isolated synthetic cross-worker SQLite ticket-claim tests (no memory writes)."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from backend.memory_auth_epoch_store import (
    AuthEpochRegistry, AuthenticatedLogin,
)
from backend.memory_shared_confirmation_ticket import (
    SharedConfirmationTickets, SharedTicketError, TrustedTicketContext,
)
from backend.tests._memory_session_confirm_worker import valid_request
from backend.tests._support import NoNetworkMixin


class SharedTicketTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "shared.sqlite"
        patch = mock.patch.dict(os.environ, {
            "MEMORY_TRUSTED_AUTH_EPOCH_ENABLED": "true",
            "MEMORY_SHARED_CONFIRMATION_TICKETS_ENABLED": "true",
            "MEMORY_SESSION_CONFIRMATION_ENABLED": "false",
            "MEMORY_SESSION_BOUND_WRITE_ENABLED": "false",
            "OPENAI_API_KEY": "",
        })
        patch.start()
        self.addCleanup(patch.stop)
        self.state = {
            "principal": "synthetic-a",
            "login": "L" * 32,
            "epoch": None,
            "session": "s1",
        }
        self.registry = AuthEpochRegistry(
            path=self.path,
            identity_provider=lambda: AuthenticatedLogin(
                self.state["principal"], self.state["login"]
            ),
        )
        self.state["epoch"] = self.registry.issue_after_authenticated_login()
        self.storage = self.new_storage()

    def new_storage(self, clock=time.time):
        return SharedConfirmationTickets(
            path=self.path,
            context_provider=lambda: TrustedTicketContext(
                principal_id=self.state["principal"],
                login_id=self.state["login"],
                auth_epoch=self.state["epoch"].value,
                session_id=self.state["session"],
                canonical_sessions=({"id": "s1"}, {"id": "s2"}),
            ),
            clock=clock,
        )

    def claim(self, offer, request=None, storage=None, **override):
        args = {
            "ticket": offer.ticket, "request": valid_request(),
            "content_sha256": offer.content_sha256,
            "confirmation_text": offer.confirmation_text,
        }
        if request is not None:
            args["request"] = request
        args.update(override)
        return (storage or self.storage).claim(**args)

    def test_prepare_zero_memory_mutations_and_no_plaintext_db(self):
        offer = self.storage.prepare(valid_request())
        with closing(sqlite3.connect(self.path)) as db:
            columns = [r[1] for r in db.execute("PRAGMA table_info(memory_shared_ticket_v1)")]
            rows = db.execute("SELECT ticket_sha256,status FROM memory_shared_ticket_v1").fetchall()
        self.assertEqual(rows[0][1], "pending")
        self.assertNotIn("content", columns)
        self.assertNotIn(offer.ticket, repr(offer))
        self.assertNotEqual(rows[0][0], offer.ticket)
        self.assertEqual(offer.expires_in_seconds, 120)
        self.assertEqual(self.claim(offer).request_id, valid_request().request_id)
        with self.assertRaises(SharedTicketError):
            self.claim(offer)

    def test_cross_instance_claim_single_use(self):
        offer = self.storage.prepare(valid_request())
        self.assertEqual(self.claim(offer, storage=self.new_storage()).request_id, "a" * 32)
        with self.assertRaises(SharedTicketError):
            self.claim(offer)

    def test_atomic_simultaneous_claims(self):
        offer = self.storage.prepare(valid_request())
        ok, denied = [], []
        lock = threading.Lock()
        def worker():
            try:
                value = self.claim(offer, storage=self.new_storage())
                with lock: ok.append(value)
            except SharedTicketError:
                with lock: denied.append(True)
        threads = [threading.Thread(target=worker) for _ in range(10)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=8)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(denied), 9)

    def test_wrong_phrase_burns_ticket(self):
        offer = self.storage.prepare(valid_request())
        with self.assertRaises(SharedTicketError):
            self.claim(offer, confirmation_text="okay")
        with self.assertRaises(SharedTicketError):
            self.claim(offer)

    def test_wrong_digest_burns_ticket(self):
        offer = self.storage.prepare(valid_request())
        with self.assertRaises(SharedTicketError):
            self.claim(offer, content_sha256="0" * 64)
        with self.assertRaises(SharedTicketError):
            self.claim(offer)

    def test_wrong_content_burns_ticket(self):
        offer = self.storage.prepare(valid_request())
        with self.assertRaises(SharedTicketError):
            self.claim(offer, request=valid_request(content="Different synthetic content"))
        with self.assertRaises(SharedTicketError):
            self.claim(offer)

    def test_relogin_rotates_old_claim(self):
        offer = self.storage.prepare(valid_request())
        self.state["epoch"] = self.registry.issue_after_authenticated_login()
        with self.assertRaises(SharedTicketError):
            self.claim(offer)

    def test_logout_revokes_old_claim(self):
        offer = self.storage.prepare(valid_request())
        self.registry.revoke_before_logout()
        with self.assertRaises(SharedTicketError):
            self.claim(offer)

    def test_switch_principal_or_session_fails_closed(self):
        for field, value in [("principal", "synthetic-b"), ("session", "s2")]:
            offer = self.storage.prepare(valid_request())
            old = self.state[field]
            self.state[field] = value
            with self.assertRaises(SharedTicketError):
                self.claim(offer)
            self.state[field] = old
            with self.assertRaises(SharedTicketError):
                self.claim(offer)

    def test_expiry_strict_120_seconds(self):
        start = 1000.0
        store = self.new_storage(clock=lambda: start)
        offer = store.prepare(valid_request())
        expired = self.new_storage(clock=lambda: start + 120)
        with self.assertRaises(SharedTicketError):
            self.claim(offer, storage=expired)

    def test_off_is_fail_closed(self):
        offer = self.storage.prepare(valid_request())
        os.environ["MEMORY_SHARED_CONFIRMATION_TICKETS_ENABLED"] = "false"
        with self.assertRaises(SharedTicketError):
            self.claim(offer)
        with self.assertRaises(SharedTicketError):
            self.storage.prepare(valid_request())
        os.environ["MEMORY_SHARED_CONFIRMATION_TICKETS_ENABLED"] = "true"
        self.registry.revoke_before_logout()
        with self.assertRaises(SharedTicketError):
            self.claim(offer)

    def test_missing_epoch_registry_rejects_prepare(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SharedConfirmationTickets(
                path=Path(temp) / "empty.sqlite",
                context_provider=lambda: TrustedTicketContext(
                    "synthetic-a", "L" * 32, "A" * 32, "s1", ({"id": "s1"},)
                ),
            )
            with self.assertRaises(SharedTicketError):
                store.prepare(valid_request())

    def test_global_pending_limit_and_expiry_cleanup(self):
        start = 1500.0
        store = self.new_storage(clock=lambda: start)
        for i in range(8):
            store.prepare(valid_request(marker=chr(97+i)))
        with self.assertRaises(SharedTicketError):
            store.prepare(valid_request(marker="z"))
        self.new_storage(clock=lambda: start + 121).prepare(valid_request(marker="z"))

    def test_lock_wait_does_not_extend_ticket_expiry(self):
        # An expired ticket must not be accepted with a timestamp sampled
        # before SQLite BEGIN IMMEDIATE has finished waiting for the lock.
        moment = [1000.0]
        store = self.new_storage(clock=lambda: moment[0])
        offer = store.prepare(valid_request())
        started = threading.Event()
        results = []
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            def attempt():
                started.set()
                try:
                    self.claim(offer, storage=store)
                    results.append("accepted")
                except SharedTicketError:
                    results.append("rejected")
            thread = threading.Thread(target=attempt)
            thread.start()
            self.assertTrue(started.wait(timeout=2))
            time.sleep(0.12)
            moment[0] = 1120.0
            lock.execute("COMMIT")
        thread.join(timeout=6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, ["rejected"])

    def test_flag_disabled_while_waiting_for_db_lock(self):
        moment = [1000.0]
        store = self.new_storage(clock=lambda: moment[0])
        offer = store.prepare(valid_request())
        started = threading.Event()
        results = []
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            def attempt():
                started.set()
                try:
                    self.claim(offer, storage=store)
                    results.append("accepted")
                except SharedTicketError:
                    results.append("rejected")
            thread = threading.Thread(target=attempt)
            thread.start()
            self.assertTrue(started.wait(timeout=2))
            time.sleep(0.12)
            os.environ["MEMORY_SHARED_CONFIRMATION_TICKETS_ENABLED"] = "false"
            lock.execute("COMMIT")
        thread.join(timeout=6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, ["rejected"])
        os.environ["MEMORY_SHARED_CONFIRMATION_TICKETS_ENABLED"] = "true"
        self.assertEqual(self.claim(offer, storage=store).request_id, valid_request().request_id)

    def test_prepare_ttl_begins_after_db_lock_acquisition(self):
        moment = [1000.0]
        store = self.new_storage(clock=lambda: moment[0])
        started = threading.Event()
        result = []
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            def attempt():
                started.set()
                try:
                    result.append(store.prepare(valid_request()))
                except SharedTicketError:
                    result.append(None)
            thread = threading.Thread(target=attempt)
            thread.start()
            self.assertTrue(started.wait(timeout=2))
            time.sleep(0.12)
            moment[0] = 1120.0
            lock.execute("COMMIT")
        thread.join(timeout=6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result), 1)
        self.assertIsNotNone(result[0])
        self.assertEqual(self.claim(result[0], storage=store).request_id, "a" * 32)

    def _observed_context_storage(self, called):
        # Record WHEN trusted server state is read, rather than relying only
        # on a sleep-based scheduling guess in lock contention tests.
        def provider():
            called.set()
            return TrustedTicketContext(
                principal_id=self.state["principal"],
                login_id=self.state["login"],
                auth_epoch=self.state["epoch"].value,
                session_id=self.state["session"],
                canonical_sessions=({"id": "s1"}, {"id": "s2"}),
            )
        return SharedConfirmationTickets(
            path=self.path, context_provider=provider,
        )

    def test_session_switch_while_waiting_for_claim_lock_fails_closed(self):
        # The pending ticket was made for s1; the trusted server session
        # changes during SQLite contention. A pre-lock snapshot is unsafe.
        offer = self.storage.prepare(valid_request())
        started = threading.Event()
        provider_called = threading.Event()
        observed_store = self._observed_context_storage(provider_called)
        outcomes = []
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            def attempt():
                started.set()
                try:
                    self.claim(offer, storage=observed_store)
                    outcomes.append("accepted")
                except SharedTicketError:
                    outcomes.append("rejected")
            thread = threading.Thread(target=attempt)
            thread.start()
            self.assertTrue(started.wait(timeout=2))
            time.sleep(0.15)
            self.assertFalse(
                provider_called.is_set(),
                "trusted context was sampled before acquiring the DB lock",
            )
            self.state["session"] = "s2"
            lock.execute("COMMIT")
        thread.join(timeout=7)
        self.assertFalse(thread.is_alive())
        self.assertTrue(provider_called.is_set())
        self.assertEqual(outcomes, ["rejected"])
        self.state["session"] = "s1"
        with self.assertRaises(SharedTicketError):
            self.claim(offer)

    def _assert_prepare_rejected_after_session_switch_while_waiting_for_lock(self):
        started = threading.Event()
        provider_called = threading.Event()
        observed_store = self._observed_context_storage(provider_called)
        outcomes = []
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            def attempt():
                started.set()
                try:
                    observed_store.prepare(valid_request())
                    outcomes.append("accepted")
                except SharedTicketError:
                    outcomes.append("rejected")
            thread = threading.Thread(target=attempt)
            thread.start()
            self.assertTrue(started.wait(timeout=2))
            time.sleep(0.15)
            self.assertFalse(
                provider_called.is_set(),
                "trusted context was sampled before acquiring the DB lock",
            )
            self.state["session"] = "s2"
            lock.execute("COMMIT")
        thread.join(timeout=7)
        self.assertFalse(thread.is_alive())
        self.assertTrue(provider_called.is_set())
        self.assertEqual(outcomes, ["rejected"])

    def test_session_switch_while_waiting_for_prepare_lock_fails_closed(self):
        self._assert_prepare_rejected_after_session_switch_while_waiting_for_lock()
        with closing(sqlite3.connect(self.path)) as conn:
            # The first prepare creates its table inside the same transaction;
            # rejecting that prepare must roll back the DDL as well as any row.
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='memory_shared_ticket_v1'"
            ).fetchone())

    def test_session_switch_while_waiting_for_prepare_lock_preserves_existing_ticket(self):
        request = valid_request(marker="b")
        offer = self.storage.prepare(request)
        with closing(sqlite3.connect(self.path)) as conn:
            before = conn.execute("SELECT * FROM memory_shared_ticket_v1").fetchall()
        self._assert_prepare_rejected_after_session_switch_while_waiting_for_lock()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT * FROM memory_shared_ticket_v1").fetchall(),
                before,
            )
        self.state["session"] = "s1"
        self.assertEqual(self.claim(offer, request=request).request_id, request.request_id)

    def test_bad_identity_provider_refused(self):
        offer = self.storage.prepare(valid_request())
        invalid = SharedConfirmationTickets(
            path=self.path, context_provider=lambda: {
                "principal": "synthetic-a", "epoch": self.state["epoch"].value
            }
        )
        with self.assertRaises(SharedTicketError):
            self.claim(offer, storage=invalid)
        with self.assertRaises(SharedTicketError):
            self.claim(offer)

    def test_second_python_process_claim_is_durable(self):
        offer = self.storage.prepare(valid_request())
        payload = {
            "principal": self.state["principal"], "login": self.state["login"],
            "epoch": self.state["epoch"].value, "session": "s1",
            "ticket": offer.ticket, "digest": offer.content_sha256,
        }
        result = subprocess.run(
            [sys.executable, "-m", "backend.tests._memory_shared_ticket_claim_child",
             str(self.path)],
            input=json.dumps(payload), text=True, capture_output=True,
            timeout=12, check=False, env=os.environ.copy(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "CLAIMED_OFFLINE")
        with self.assertRaises(SharedTicketError):
            self.claim(offer)


if __name__ == "__main__":
    unittest.main()
