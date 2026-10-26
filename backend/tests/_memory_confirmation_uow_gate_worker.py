"""Offline review of UOW confirmation gate: no memory commits or HTTP."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from backend import channel_store
from backend.memory_auth_epoch_store import AuthEpochRegistry, AuthenticatedLogin
from backend.memory_shared_confirmation_ticket import (
    SharedConfirmationTickets, TrustedTicketContext,
)
from backend.memory_confirmation_uow_gate import (
    MemoryUowConfirmationGate, UowConfirmationGateError,
)
from backend.tests._memory_session_confirm_worker import valid_request
from backend.tests._support import NoNetworkMixin
from backend.tests.test_memory_service import bootstrap_runtime, memory_config


class UowGateTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / "uow-gate.sqlite")
        patch = mock.patch.dict(os.environ, {
            "MEMORY_TRUSTED_AUTH_EPOCH_ENABLED": "true",
            "MEMORY_SHARED_CONFIRMATION_TICKETS_ENABLED": "true",
            "MEMORY_CONFIRMED_UOW_GATE_ENABLED": "true",
            "MEMORY_SESSION_CONFIRMATION_ENABLED": "false",
            "MEMORY_SESSION_BOUND_WRITE_ENABLED": "false",
            "OPENAI_API_KEY": "",
        })
        patch.start()
        self.addCleanup(patch.stop)
        with closing(channel_store.connect(self.path)) as db:
            db.execute(channel_store.RELAY_TABLE_DDL["messages"])
        channel_store.run_migrations(self.path)
        runtime = bootstrap_runtime(self.path, memory_config())
        self.store = runtime.privileged_actions._store
        self.context = {
            "principal": "synthetic-a", "login": "L" * 32,
            "epoch": None, "session": "s1",
        }
        self.registry = AuthEpochRegistry(
            path=self.path,
            identity_provider=lambda: AuthenticatedLogin(
                self.context["principal"], self.context["login"]
            ),
        )
        self.context["epoch"] = self.registry.issue_after_authenticated_login()
        self.provider = lambda: TrustedTicketContext(
            self.context["principal"], self.context["login"],
            self.context["epoch"].value, self.context["session"],
            ({"id": "s1"}, {"id": "s2"}),
        )
        self.tickets = SharedConfirmationTickets(
            path=self.path, context_provider=self.provider,
        )
        self.gate = MemoryUowConfirmationGate(
            path=self.path, context_provider=self.provider,
        )
        self.request = valid_request()
        self.offer = self.tickets.prepare(self.request)

    def run_gate(self, uow, **changes):
        payload = {
            "uow": uow, "request": self.request,
            "ticket": self.offer.ticket,
            "content_sha256": self.offer.content_sha256,
            "confirmation_text": self.offer.confirmation_text,
        }
        payload.update(changes)
        return self.gate.consume_in_existing_uow(**payload)

    def _status(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(
                "SELECT status FROM memory_shared_ticket_v1"
            ).fetchone()[0]

    def _count(self, table):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(
                "SELECT COUNT(*) FROM " + table
            ).fetchone()[0]

    def test_consumed_inside_real_memory_uow_but_rolled_back(self):
        with self.store._action_unit_of_work() as uow:
            receipt = self.run_gate(uow)
            self.assertEqual(receipt.request_id, self.request.request_id)
            self.assertNotIn(receipt.request_id, repr(receipt))
            # Using this exact connection sees the provisional consumption.
            value = uow._execute(
                "SELECT status FROM memory_shared_ticket_v1"
            ).fetchone()[0]
            self.assertEqual(value, "consumed")
            # Do NOT commit; real memory action ledger terminal is absent.
        self.assertEqual(self._status(), "pending")
        self.assertEqual(self._count("memory_action_requests"), 0)
        self.assertEqual(self._count("messages"), 0)

    def test_cannot_consume_same_ticket_twice_inside_one_uow(self):
        with self.store._action_unit_of_work() as uow:
            self.run_gate(uow)
            with self.assertRaises(UowConfirmationGateError):
                self.run_gate(uow)
        self.assertEqual(self._status(), "pending")

    def test_outside_a_real_uow_fails(self):
        with self.assertRaises(UowConfirmationGateError):
            self.run_gate(object())
        self.assertEqual(self._status(), "pending")

    def test_disabled_by_default_gate_is_fail_closed(self):
        os.environ.pop("MEMORY_CONFIRMED_UOW_GATE_ENABLED")
        with self.store._action_unit_of_work() as uow:
            with self.assertRaises(UowConfirmationGateError):
                self.run_gate(uow)
        self.assertEqual(self._status(), "pending")

    def test_logout_revocation_before_transaction_fails(self):
        self.registry.revoke_before_logout()
        with self.store._action_unit_of_work() as uow:
            with self.assertRaises(UowConfirmationGateError):
                self.run_gate(uow)
        self.assertEqual(self._status(), "pending")

    def test_relogin_rotates_epoch_and_old_ticket_rejected(self):
        self.context["epoch"] = self.registry.issue_after_authenticated_login()
        with self.store._action_unit_of_work() as uow:
            with self.assertRaises(UowConfirmationGateError):
                self.run_gate(uow)
        self.assertEqual(self._status(), "pending")

    def test_changed_principal_fails_closed(self):
        self.context["principal"] = "synthetic-b"
        self.context["epoch"] = self.registry.issue_after_authenticated_login()
        with self.store._action_unit_of_work() as uow:
            with self.assertRaises(UowConfirmationGateError):
                self.run_gate(uow)
        self.assertEqual(self._status(), "pending")

    def test_changed_session_fails_closed(self):
        self.context["session"] = "s2"
        with self.store._action_unit_of_work() as uow:
            with self.assertRaises(UowConfirmationGateError):
                self.run_gate(uow)
        self.assertEqual(self._status(), "pending")

    def test_wrong_phrase_or_digest_rejected_without_write(self):
        for bad in (
            {"confirmation_text": "okay"},
            {"content_sha256": "0" * 64},
            {"request": valid_request(content="Wrong synthetic preview")},
        ):
            with self.subTest(bad=list(bad)):
                with self.store._action_unit_of_work() as uow:
                    with self.assertRaises(UowConfirmationGateError):
                        self.run_gate(uow, **bad)
                self.assertEqual(self._status(), "pending")

    def test_foreign_db_uow_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = str(Path(tmp) / "other.sqlite")
            with closing(channel_store.connect(other)) as db:
                db.execute(channel_store.RELAY_TABLE_DDL["messages"])
            channel_store.run_migrations(other)
            other_runtime = bootstrap_runtime(other, memory_config())
            with other_runtime.privileged_actions._store._action_unit_of_work() as uow:
                with self.assertRaises(UowConfirmationGateError):
                    self.run_gate(uow)
        self.assertEqual(self._status(), "pending")

    def test_gate_refuses_already_claimed_ticket(self):
        self.tickets.claim(
            ticket=self.offer.ticket, request=self.request,
            content_sha256=self.offer.content_sha256,
            confirmation_text=self.offer.confirmation_text,
        )
        with self.store._action_unit_of_work() as uow:
            with self.assertRaises(UowConfirmationGateError):
                self.run_gate(uow)
        self.assertEqual(self._status(), "consumed")

    def test_real_uow_lock_defers_logout_until_rollback(self):
        # This is the production transaction OBJECT with synthetic evidence;
        # no canonical record/ledger write occurs.
        started = threading.Event()
        finished = threading.Event()
        outcomes = []

        def revoke():
            started.set()
            try:
                outcomes.append(self.registry.revoke_before_logout())
            except Exception as exc:
                outcomes.append(type(exc).__name__)
            finally:
                finished.set()

        with self.store._action_unit_of_work() as uow:
            self.run_gate(uow)
            thread = threading.Thread(target=revoke)
            thread.start()
            self.assertTrue(started.wait(timeout=2))
            time.sleep(0.15)
            self.assertFalse(finished.is_set(), "revocation bypassed UOW lock")
            self.assertEqual(
                uow._execute(
                    "SELECT status FROM memory_shared_ticket_v1"
                ).fetchone()[0], "consumed",
            )
            # No ledger terminal -> implicit ROLLBACK on exit.

        thread.join(timeout=7)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, [True])
        self.assertEqual(self._status(), "pending")
        with self.store._action_unit_of_work() as uow:
            with self.assertRaises(UowConfirmationGateError):
                self.run_gate(uow)
        self.assertEqual(self._count("messages"), 0)
        self.assertEqual(self._count("memory_action_requests"), 0)

    def test_expiry_checked_at_real_uow_verification(self):
        # The clock is sampled while already inside the real write lock.
        self.gate = MemoryUowConfirmationGate(
            path=self.path,
            context_provider=self.provider,
            clock=lambda: time.time() + 121,
        )
        with self.store._action_unit_of_work() as uow:
            with self.assertRaises(UowConfirmationGateError):
                self.run_gate(uow)
        self.assertEqual(self._status(), "pending")
        self.assertEqual(self._count("messages"), 0)

    def test_gate_kill_switch_after_trusted_context_fails_closed(self):
        def provider_disables_gate():
            snapshot = self.provider()
            os.environ["MEMORY_CONFIRMED_UOW_GATE_ENABLED"] = "false"
            return snapshot

        self.gate = MemoryUowConfirmationGate(
            path=self.path, context_provider=provider_disables_gate,
        )
        with self.store._action_unit_of_work() as uow:
            with self.assertRaises(UowConfirmationGateError):
                self.run_gate(uow)
        self.assertEqual(self._status(), "pending")
        self.assertEqual(self._count("messages"), 0)

    def test_ticket_still_pending_after_failed_uow(self):
        try:
            with self.store._action_unit_of_work() as uow:
                self.run_gate(uow)
                raise RuntimeError("simulated_writer_exception")
        except RuntimeError as exc:
            self.assertEqual(str(exc), "simulated_writer_exception")
        self.assertEqual(self._status(), "pending")
        self.assertEqual(self._count("memory_action_requests"), 0)


if __name__ == "__main__":
    unittest.main()
