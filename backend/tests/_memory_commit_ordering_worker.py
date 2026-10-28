"""OFFLINE concurrency proof of auth-revocation ordering with SQLite.

IMPORTANT: This uses a synthetic marker table, NOT the production memory
service/UOW/ledger and NOT human-confirmed authorization. The proof does not
close the existing production integration gap. No memory text is ever saved.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
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
    SharedConfirmationTickets, TrustedTicketContext,
)
from backend.tests._memory_session_confirm_worker import valid_request
from backend.tests._support import NoNetworkMixin

_MARKER_DDL = """CREATE TABLE IF NOT EXISTS memory_commit_ordering_probe_v1 (
    request_id TEXT PRIMARY KEY, ticket_sha256 TEXT NOT NULL UNIQUE
)"""


class CommitOrderingProofTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "proof.sqlite"
        patch = mock.patch.dict(os.environ, {
            "MEMORY_TRUSTED_AUTH_EPOCH_ENABLED": "true",
            "MEMORY_SHARED_CONFIRMATION_TICKETS_ENABLED": "true",
            "MEMORY_COMMIT_ORDERING_PROBE_ENABLED": "true",
            "MEMORY_SESSION_BOUND_WRITE_ENABLED": "false",
            "MEMORY_SESSION_CONFIRMATION_ENABLED": "false",
            "OPENAI_API_KEY": "",
        })
        patch.start()
        self.addCleanup(patch.stop)
        self.principal = "synthetic-a"
        self.login = "L" * 32
        self.request = valid_request()
        self.registry = AuthEpochRegistry(
            path=self.path,
            identity_provider=lambda: AuthenticatedLogin(self.principal, self.login),
        )
        self.epoch = self.registry.issue_after_authenticated_login()
        self.tickets = SharedConfirmationTickets(
            path=self.path,
            context_provider=lambda: TrustedTicketContext(
                self.principal, self.login, self.epoch.value,
                "s1", ({"id": "s1"},),
            ),
        )
        self.offer = self.tickets.prepare(self.request)
        self.tickets.claim(
            ticket=self.offer.ticket, request=self.request,
            content_sha256=self.offer.content_sha256,
            confirmation_text=self.offer.confirmation_text,
        )

    def _synthetic_commit(self, *, under_lock=None, path=None):
        """Only synthetic marker; final checks occur inside SQLite write txn.

        This is test-side SQL, not an integration into the real memory writer.
        """
        if os.environ.get("MEMORY_COMMIT_ORDERING_PROBE_ENABLED") != "true":
            raise RuntimeError("probe_disabled")
        where = path or self.path
        with closing(sqlite3.connect(where, timeout=5, isolation_level=None)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if os.environ.get("MEMORY_COMMIT_ORDERING_PROBE_ENABLED") != "true":
                    raise RuntimeError("probe_disabled")
                if os.environ.get("MEMORY_TRUSTED_AUTH_EPOCH_ENABLED") != "true":
                    raise RuntimeError("epoch_disabled")
                if os.environ.get("MEMORY_SHARED_CONFIRMATION_TICKETS_ENABLED") != "true":
                    raise RuntimeError("ticket_disabled")
                identity = (self.principal, self.login)
                epoch_digest = hashlib.sha256(self.epoch.value.encode("ascii")).hexdigest()
                ticket_digest = hashlib.sha256(self.offer.ticket.encode("ascii")).hexdigest()
                row = conn.execute(
                    """SELECT e.epoch_sha256,e.revoked,
                              t.principal_id,t.login_id,t.epoch_sha256,
                              t.session_id,t.request_id,t.content_sha256,
                              t.expires_at,t.status
                       FROM memory_auth_epoch_v1 AS e
                       JOIN memory_shared_ticket_v1 AS t
                         ON e.principal_id=t.principal_id
                        AND e.login_id=t.login_id
                       WHERE t.ticket_sha256=? AND e.principal_id=?
                         AND e.login_id=?""",
                    (ticket_digest, *identity),
                ).fetchone()
                if not (
                    row is not None and row[1] == 0
                    and type(row[0]) is str
                    and hmac.compare_digest(row[0], epoch_digest)
                    and row[2] == self.principal and row[3] == self.login
                    and hmac.compare_digest(row[4], epoch_digest)
                    and row[5] == "s1"
                    and row[6] == self.request.request_id
                    and hmac.compare_digest(row[7], self.offer.content_sha256)
                    and row[8] > time.time() and row[9] == "consumed"
                ):
                    raise RuntimeError("epoch_or_ticket_no_longer_valid")
                conn.execute(_MARKER_DDL)
                if under_lock is not None:
                    under_lock()
                # Markers contain only synthetic IDs; no memory text or ledger write.
                conn.execute(
                    """INSERT INTO memory_commit_ordering_probe_v1
                       (request_id,ticket_sha256) VALUES (?,?)""",
                    (self.request.request_id, ticket_digest),
                )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def _marker_count(self):
        with closing(sqlite3.connect(self.path)) as conn:
            present = conn.execute(
                """SELECT COUNT(*) FROM sqlite_master
                   WHERE type='table' AND name='memory_commit_ordering_probe_v1'"""
            ).fetchone()[0]
            if not present:
                return 0
            return conn.execute(
                "SELECT COUNT(*) FROM memory_commit_ordering_probe_v1"
            ).fetchone()[0]

    def test_synthetic_commit_marker_only(self):
        self._synthetic_commit()
        self.assertEqual(self._marker_count(), 1)
        with closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute("SELECT * FROM memory_commit_ordering_probe_v1").fetchone()
        self.assertNotIn(self.request.content, str(row))
        self.assertNotIn(self.offer.ticket, str(row))

    def test_logout_before_commit_rejected(self):
        self.assertTrue(self.registry.revoke_before_logout())
        with self.assertRaises(RuntimeError):
            self._synthetic_commit()
        self.assertEqual(self._marker_count(), 0)

    def test_relogin_before_commit_rejected(self):
        self.epoch = self.registry.issue_after_authenticated_login()
        with self.assertRaises(RuntimeError):
            self._synthetic_commit()
        self.assertEqual(self._marker_count(), 0)

    def test_account_switch_cannot_claim_original_approval(self):
        self.principal = "synthetic-b"
        self.registry.issue_after_authenticated_login()
        with self.assertRaises(RuntimeError):
            self._synthetic_commit()
        self.assertEqual(self._marker_count(), 0)

    def test_commit_lock_precedes_logout_revocation(self):
        acquired = threading.Event()
        release = threading.Event()
        revoked = threading.Event()
        errors = []

        def pause():
            acquired.set()
            if not release.wait(timeout=4):
                raise RuntimeError("test_lock_timeout")

        def writer():
            try:
                self._synthetic_commit(under_lock=pause)
            except Exception as exc:
                errors.append(type(exc).__name__)

        def logout():
            try:
                self.registry.revoke_before_logout()
                revoked.set()
            except Exception as exc:
                errors.append(type(exc).__name__)

        w = threading.Thread(target=writer)
        r = threading.Thread(target=logout)
        w.start()
        self.assertTrue(acquired.wait(timeout=4))
        r.start()
        # Logout cannot commit while the writer holds the SAME SQLite lock.
        time.sleep(0.15)
        self.assertFalse(revoked.is_set())
        release.set()
        w.join(timeout=6)
        r.join(timeout=6)
        self.assertTrue(all(not t.is_alive() for t in (w, r)))
        self.assertEqual(errors, [])
        self.assertTrue(revoked.is_set())
        self.assertEqual(self._marker_count(), 1)
        with self.assertRaises(RuntimeError):
            self._synthetic_commit()

    def test_failed_synthetic_transaction_rolls_back(self):
        def fail():
            raise RuntimeError("synthetic_failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic_failure"):
            self._synthetic_commit(under_lock=fail)
        self.assertEqual(self._marker_count(), 0)
        # A recovered explicit test invocation may retry the synthetic proof;
        # this does NOT establish production one-use crash consistency.
        self._synthetic_commit()
        self.assertEqual(self._marker_count(), 1)

    def test_same_synthetic_request_cannot_commit_twice(self):
        self._synthetic_commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self._synthetic_commit()
        self.assertEqual(self._marker_count(), 1)

    def test_probe_flag_off_fails_closed(self):
        os.environ["MEMORY_COMMIT_ORDERING_PROBE_ENABLED"] = "false"
        with self.assertRaises(RuntimeError):
            self._synthetic_commit()
        self.assertEqual(self._marker_count(), 0)

    def test_epoch_flag_off_fails_closed(self):
        os.environ["MEMORY_TRUSTED_AUTH_EPOCH_ENABLED"] = "false"
        with self.assertRaises(RuntimeError):
            self._synthetic_commit()
        self.assertEqual(self._marker_count(), 0)

    def test_separate_db_cannot_validate_epoch_or_ticket(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(sqlite3.OperationalError):
                self._synthetic_commit(path=Path(temp) / "another.sqlite")
        self.assertEqual(self._marker_count(), 0)


if __name__ == "__main__":
    unittest.main()
