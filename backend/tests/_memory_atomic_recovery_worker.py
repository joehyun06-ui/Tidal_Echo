"""Hard-crash and uncertain-outcome checks for default-OFF confirmed memory.

Real SQLite and existing HMAC ledger; synthetic data only, no HTTP, no
external network. Separate process is killed at four transaction phases.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from contextlib import closing
from unittest import mock

from backend import memory_action_ledger, memory_explicit_actions
from backend.memory_confirmation_uow_gate import UowConfirmationGateError
from backend.tests._memory_atomic_confirmed_worker import (
    AtomicConfirmedMemoryTests,
)


class CrashAndRecoveryTests(unittest.TestCase):
    def setUp(self):
        # Reuse the thoroughly reviewed fixture and its NoNetworkMixin.
        fixture = AtomicConfirmedMemoryTests(
            "test_no_route_or_public_service_method_added"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fx = fixture

    def _assert_db(self, *, committed):
        self.assertEqual(self.fx.status(), "consumed" if committed else "pending")
        for table in ("messages", "memory_action_requests", "memory_items"):
            self.assertEqual(self.fx.count(table), int(committed), table)

    def _hard_exit(self, phase):
        payload = dict(
            phase=phase, path=self.fx.path,
            epoch=self.fx.state["epoch"].value,
            ticket=self.fx.offer.ticket,
            digest=self.fx.offer.content_sha256,
            phrase=self.fx.offer.confirmation_text,
        )
        env = dict(os.environ)
        env["OPENAI_API_KEY"] = ""
        run = subprocess.run(
            [sys.executable, "-m", "backend.tests._memory_atomic_crash_child"],
            input=json.dumps(payload), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=35, env=env, check=False,
        )
        self.assertEqual(
            run.returncode, 61,
            "crash process did not reach injection: " + run.stderr[-700:],
        )

    def test_exit_after_ticket_rolls_back_all(self):
        self._hard_exit("after_ticket")
        self._assert_db(committed=False)
        self.assertEqual(self.fx.commit().status, "completed")
        self._assert_db(committed=True)

    def test_exit_after_canonical_rolls_back_all(self):
        self._hard_exit("after_canonical")
        self._assert_db(committed=False)
        self.assertEqual(self.fx.commit().status, "completed")
        self._assert_db(committed=True)

    def test_exit_after_ledger_rolls_back_all(self):
        self._hard_exit("after_ledger")
        self._assert_db(committed=False)
        self.assertEqual(self.fx.commit().status, "completed")
        self._assert_db(committed=True)

    def test_exit_after_commit_retains_all_and_blocks_same_ticket(self):
        self._hard_exit("after_commit")
        self._assert_db(committed=True)
        with self.assertRaises(UowConfirmationGateError):
            self.fx.commit()
        self._assert_db(committed=True)

    def _lose_ack(self, on_committed):
        original = memory_action_ledger._MemoryActionUnitOfWork.commit
        called = [False]

        def unreliable_commit(uow):
            if not called[0]:
                called[0] = True
                if on_committed:
                    result = original(uow)
                    on_committed()
                    raise memory_action_ledger.MemoryActionLedgerError(
                        "transaction_outcome_uncertain"
                    )
                uow.rollback()
                raise memory_action_ledger.MemoryActionLedgerError(
                    "transaction_outcome_uncertain"
                )
            return original(uow)

        with mock.patch.object(
            memory_action_ledger._MemoryActionUnitOfWork,
            "commit", new=unreliable_commit,
        ):
            return self.fx.commit()

    def test_lost_ack_after_sql_commit_recovers_valid_signed_terminal(self):
        result = self._lose_ack(lambda: None)
        self.assertTrue(result.replayed)
        self._assert_db(committed=True)

    def test_uncommitted_uncertain_result_denies_without_side_effect(self):
        with self.assertRaisesRegex(
            memory_explicit_actions.ExplicitMemoryActionError,
            "transaction_outcome_uncertain",
        ):
            self._lose_ack(None)
        self._assert_db(committed=False)

    def test_lost_ack_missing_consumed_ticket_cannot_claim_success(self):
        def corrupt_ticket_for_negative_test():
            with closing(sqlite3.connect(self.fx.path)) as conn:
                conn.execute(
                    "UPDATE memory_shared_ticket_v1 SET status='pending', "
                    "consumed_at=NULL WHERE status='consumed'"
                )
                conn.commit()

        with self.assertRaises(UowConfirmationGateError):
            self._lose_ack(corrupt_ticket_for_negative_test)
        self.assertEqual(self.fx.status(), "pending")
        self.assertEqual(self.fx.count("messages"), 1)
        self.assertEqual(self.fx.count("memory_action_requests"), 1)

    def test_lost_ack_after_logout_refuses_live_recovery(self):
        with self.assertRaises(UowConfirmationGateError):
            self._lose_ack(self.fx.registry.revoke_before_logout)
        self._assert_db(committed=True)

    def test_lost_ack_can_recover_after_ttl_elapsed(self):
        def pass_clock_after_commit():
            # Original consumption was inside TTL, so recovery cannot require
            # current wall-clock time to be before the original expiry.
            self.fx.gate._clock = lambda: time.time() + 3600
        result = self._lose_ack(pass_clock_after_commit)
        self.assertTrue(result.replayed)
        self._assert_db(committed=True)

    def test_lost_ack_disabled_flag_refuses_recovery(self):
        def disable_after_commit():
            os.environ["MEMORY_ATOMIC_CONFIRMED_REVIEW_ENABLED"] = "false"
        with self.assertRaises(
            memory_explicit_actions.ExplicitMemoryActionError,
        ):
            self._lose_ack(disable_after_commit)
        self._assert_db(committed=True)

    def test_lost_ack_tampered_hmac_ledger_refuses_recovery(self):
        def tamper_signed_terminal():
            with closing(sqlite3.connect(self.fx.path)) as conn:
                conn.execute(
                    "UPDATE memory_action_requests SET terminal_digest=?",
                    (b"X" * 32,),
                )
                conn.commit()
        with self.assertRaises(memory_action_ledger.MemoryActionLedgerError):
            self._lose_ack(tamper_signed_terminal)
        self._assert_db(committed=True)

    def test_no_unexpected_public_confirmation_write_entry(self):
        service = memory_explicit_actions.bind_operator_cli(self.fx.backend)
        self.assertFalse(hasattr(service, "remember_confirmed"))
        self.assertFalse(hasattr(service, "_remember_confirmed_offline_review"))


if __name__ == "__main__":
    unittest.main()
