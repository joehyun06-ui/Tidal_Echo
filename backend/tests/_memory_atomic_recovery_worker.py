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
from backend.tests import _memory_atomic_confirmed_worker as atomic_fixture_module


class CrashAndRecoveryTests(unittest.TestCase):
    def setUp(self):
        # Reuse the thoroughly reviewed fixture and its NoNetworkMixin.
        fixture = atomic_fixture_module.AtomicConfirmedMemoryTests(
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

    def _reconcile_after_crashed_process(self):
        """Test-side read-only reconstruction after the original process dies.

        No new ticket consumption and no memory write; uses the signed-ledger
        recovery UOW plus the exact consumed ticket from the original request.
        """
        request = self.fx.request
        binding = memory_action_ledger.MemoryActionRequestBinding(
            request_id=request.request_id,
            action_kind="remember",
            origin="operator_cli",
            scope_type=request.scope_type,
            scope_ref=request.scope_ref,
            kind=request.kind,
            sensitivity=request.sensitivity,
            normalized_content=request.content,
        )
        return self.fx.backend._resolve_uncertain(
            binding,
            validate_uncertain=lambda uow: (
                self.fx.gate.assert_consumed_for_uncertain_in_existing_uow(
                    uow=uow,
                    request=request,
                    ticket=self.fx.offer.ticket,
                    content_sha256=self.fx.offer.content_sha256,
                    confirmation_text=self.fx.offer.confirmation_text,
                )
            ),
        )

    def test_exit_after_ticket_rolls_back_all(self):
        self._hard_exit("after_ticket")
        self._assert_db(committed=False)
        with self.assertRaisesRegex(
            memory_explicit_actions.ExplicitMemoryActionError,
            "transaction_outcome_uncertain",
        ):
            self._reconcile_after_crashed_process()
        self.assertEqual(self.fx.commit().status, "completed")
        self._assert_db(committed=True)

    def test_exit_after_canonical_rolls_back_all(self):
        self._hard_exit("after_canonical")
        self._assert_db(committed=False)
        with self.assertRaisesRegex(
            memory_explicit_actions.ExplicitMemoryActionError,
            "transaction_outcome_uncertain",
        ):
            self._reconcile_after_crashed_process()
        self.assertEqual(self.fx.commit().status, "completed")
        self._assert_db(committed=True)

    def test_exit_after_ledger_rolls_back_all(self):
        self._hard_exit("after_ledger")
        self._assert_db(committed=False)
        with self.assertRaisesRegex(
            memory_explicit_actions.ExplicitMemoryActionError,
            "transaction_outcome_uncertain",
        ):
            self._reconcile_after_crashed_process()
        self.assertEqual(self.fx.commit().status, "completed")
        self._assert_db(committed=True)

    def test_exit_after_commit_retains_all_and_blocks_same_ticket(self):
        self._hard_exit("after_commit")
        self._assert_db(committed=True)
        recovered = self._reconcile_after_crashed_process()
        self.assertTrue(recovered.replayed)
        self.assertEqual(recovered.status, "completed")
        with self.assertRaises(UowConfirmationGateError):
            self.fx.commit()
        self._assert_db(committed=True)

    def test_after_crashed_commit_logout_refuses_result_disclosure(self):
        self._hard_exit("after_commit")
        self._assert_db(committed=True)
        self.assertTrue(self.fx.registry.revoke_before_logout())
        with self.assertRaises(UowConfirmationGateError):
            self._reconcile_after_crashed_process()
        self._assert_db(committed=True)

    def test_after_crashed_commit_ttl_expiry_does_not_invalidate_recovery(self):
        self._hard_exit("after_commit")
        self._assert_db(committed=True)
        self.fx.gate._clock = lambda: time.time() + 3600
        recovered = self._reconcile_after_crashed_process()
        self.assertTrue(recovered.replayed)
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

    def test_immutable_hmac_ledger_rejects_tamper_and_recovers(self):
        def attempt_signed_terminal_tamper():
            with closing(sqlite3.connect(self.fx.path)) as conn:
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "memory_action_request_immutable",
                ):
                    conn.execute(
                        "UPDATE memory_action_requests "
                        "SET request_binding_digest=?",
                        (b"X" * 32,),
                    )
                conn.rollback()
        result = self._lose_ack(attempt_signed_terminal_tamper)
        self.assertTrue(result.replayed)
        self._assert_db(committed=True)

    def test_no_unexpected_public_confirmation_write_entry(self):
        service = memory_explicit_actions.bind_operator_cli(self.fx.backend)
        self.assertFalse(hasattr(service, "remember_confirmed"))
        self.assertFalse(hasattr(service, "_remember_confirmed_offline_review"))


if __name__ == "__main__":
    unittest.main()
