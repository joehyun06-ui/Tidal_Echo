"""Standalone synthetic, network-free tests for default-OFF auth epochs."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from backend.memory_auth_epoch_store import (
    AuthEpochError, AuthEpochRegistry, AuthenticatedLogin, ServerIssuedEpoch,
)
from backend.memory_session_confirmation import SessionWriteConfirmation, SessionConfirmationError
from backend.tests._memory_session_confirm_worker import FakeService, valid_request
from backend.tests._support import NoNetworkMixin


class EpochRegistryTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "epoch.sqlite"
        env = mock.patch.dict(os.environ, {
            "MEMORY_TRUSTED_AUTH_EPOCH_ENABLED": "true",
            "MEMORY_SESSION_BOUND_WRITE_ENABLED": "true",
            "MEMORY_SESSION_CONFIRMATION_ENABLED": "true",
        })
        env.start()
        self.addCleanup(env.stop)
        self.state = {"principal": "synthetic-a", "login": "L" * 32, "epoch": None}
        self.registry = self.new_registry()

    def new_registry(self):
        return AuthEpochRegistry(
            path=self.path,
            identity_provider=lambda: AuthenticatedLogin(
                self.state["principal"], self.state["login"]
            ),
            clock=lambda: 900.0,
        )

    def test_disabled_is_fail_closed_without_sqlite_creation(self):
        os.environ.pop("MEMORY_TRUSTED_AUTH_EPOCH_ENABLED")
        with self.assertRaises(AuthEpochError):
            self.registry.issue_after_authenticated_login()
        with self.assertRaises(AuthEpochError):
            self.registry.assert_current(ServerIssuedEpoch("A" * 43))
        with self.assertRaises(AuthEpochError):
            self.registry.revoke_before_logout()
        self.assertFalse(self.path.exists())

    def test_login_issue_and_cross_registry_validation(self):
        issued = self.registry.issue_after_authenticated_login()
        self.new_registry().assert_current(issued)
        self.assertNotIn(issued.value, repr(issued))
        self.assertNotIn(issued.value, repr(self.registry))
        self.assertNotIn(self.state["principal"], repr(AuthenticatedLogin("synthetic-a", "L" * 32)))
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT epoch_sha256, generation, revoked FROM memory_auth_epoch_v1").fetchone()
        self.assertEqual(row, (hashlib.sha256(issued.value.encode()).hexdigest(), 1, 0))
        self.assertNotEqual(row[0], issued.value)

    def test_relogin_rotates_and_old_epoch_fails(self):
        old = self.registry.issue_after_authenticated_login()
        new = self.new_registry().issue_after_authenticated_login()
        self.assertNotEqual(old.value, new.value)
        with self.assertRaises(AuthEpochError):
            self.registry.assert_current(old)
        self.registry.assert_current(new)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT generation FROM memory_auth_epoch_v1").fetchone()[0], 2)

    def test_logout_revokes_and_relogin_never_restores_old_epoch(self):
        old = self.registry.issue_after_authenticated_login()
        self.assertTrue(self.new_registry().revoke_before_logout())
        self.assertFalse(self.registry.revoke_before_logout())
        with self.assertRaises(AuthEpochError):
            self.registry.assert_current(old)
        new = self.registry.issue_after_authenticated_login()
        self.registry.assert_current(new)
        with self.assertRaises(AuthEpochError):
            self.registry.assert_current(old)

    def test_account_switch_cannot_reuse_epoch(self):
        old = self.registry.issue_after_authenticated_login()
        self.state["principal"] = "synthetic-b"
        with self.assertRaises(AuthEpochError):
            self.registry.assert_current(old)
        other = self.registry.issue_after_authenticated_login()
        self.registry.assert_current(other)
        self.state["principal"] = "synthetic-a"
        with self.assertRaises(AuthEpochError):
            self.registry.assert_current(other)
        self.registry.assert_current(old)

    def test_different_login_of_same_principal_is_not_same_epoch(self):
        old = self.registry.issue_after_authenticated_login()
        self.state["login"] = "M" * 32
        with self.assertRaises(AuthEpochError):
            self.registry.assert_current(old)
        new = self.registry.issue_after_authenticated_login()
        self.registry.assert_current(new)
        self.assertEqual(self.registry.revoke_all_for_authenticated_principal(), 2)
        with self.assertRaises(AuthEpochError):
            self.registry.assert_current(new)
        self.state["login"] = "L" * 32
        with self.assertRaises(AuthEpochError):
            self.registry.assert_current(old)

    def test_bad_provider_context_or_epoch_fail_closed(self):
        for principal, login in [
            ("", "L" * 32), ("x" * 129, "L" * 32),
            ("synthetic-a", "short"), ("synthetic-a", "L" * 31 + "!"),
        ]:
            with self.subTest(principal=principal, login=login):
                self.state.update(principal=principal, login=login)
                with self.assertRaises(AuthEpochError):
                    self.registry.issue_after_authenticated_login()
        self.state.update(principal="synthetic-a", login="L" * 32)
        with self.assertRaises(AuthEpochError):
            self.registry.assert_current(ServerIssuedEpoch("forged"))
        with self.assertRaises(AuthEpochError):
            self.registry.assert_current("A" * 43)
        self.assertFalse(self.path.exists())

    def test_missing_epoch_store_cannot_be_silently_bootstrapped_by_read(self):
        with self.assertRaises(AuthEpochError):
            self.registry.assert_current(ServerIssuedEpoch("A" * 43))
        self.assertFalse(self.path.exists())

    def test_parallel_login_rotation_single_current_generation(self):
        issued = []
        errors = []
        lock = threading.Lock()

        def rotate():
            try:
                epoch = self.new_registry().issue_after_authenticated_login()
                with lock:
                    issued.append(epoch)
            except Exception as exc:
                with lock:
                    errors.append(type(exc).__name__)

        threads = [threading.Thread(target=rotate) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=8)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertFalse(errors, errors)
        valid = 0
        for epoch in issued:
            try:
                self.registry.assert_current(epoch)
                valid += 1
            except AuthEpochError:
                pass
        self.assertEqual(len(issued), 8)
        self.assertEqual(valid, 1)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT generation FROM memory_auth_epoch_v1").fetchone()[0], 8)

    def test_adapter_rejects_old_ticket_on_logout(self):
        self.state["epoch"] = self.registry.issue_after_authenticated_login()
        provider = self.registry.make_confirmation_provider(
            epoch_provider=lambda: self.state["epoch"],
            session_provider=lambda: ("s1", ({"id": "s1"},)),
        )
        service = FakeService()
        confirmation = SessionWriteConfirmation(service=service, authorization_provider=provider)
        offer = confirmation.prepare(valid_request())
        self.registry.revoke_before_logout()
        with self.assertRaises(SessionConfirmationError):
            confirmation.confirm(
                ticket=offer.ticket,
                content_sha256=offer.content_sha256,
                confirmation_text=offer.confirmation_text,
            )
        self.assertEqual(service.writes, [])

    def test_adapter_rejects_old_ticket_on_relogin(self):
        self.state["epoch"] = self.registry.issue_after_authenticated_login()
        provider = self.registry.make_confirmation_provider(
            epoch_provider=lambda: self.state["epoch"],
            session_provider=lambda: ("s1", ({"id": "s1"},)),
        )
        service = FakeService()
        confirmation = SessionWriteConfirmation(service=service, authorization_provider=provider)
        offer = confirmation.prepare(valid_request())
        self.state["epoch"] = self.registry.issue_after_authenticated_login()
        with self.assertRaises(SessionConfirmationError):
            confirmation.confirm(
                ticket=offer.ticket,
                content_sha256=offer.content_sha256,
                confirmation_text=offer.confirmation_text,
            )
        self.assertEqual(service.writes, [])

    def test_adapter_refuses_unverified_session_provider(self):
        self.registry.issue_after_authenticated_login()
        with self.assertRaises(AuthEpochError):
            self.registry.make_confirmation_provider(epoch_provider=object(), session_provider=lambda: ())
        provider = self.registry.make_confirmation_provider(
            epoch_provider=lambda: ServerIssuedEpoch("Z" * 43),
            session_provider=lambda: ("s1", ({"id": "s1"},)),
        )
        with self.assertRaises(AuthEpochError):
            provider()


if __name__ == "__main__":
    unittest.main()
