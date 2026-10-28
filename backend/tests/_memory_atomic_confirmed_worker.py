"""Isolated *synthetic-only* atomic confirmation + real memory UOW rehearsal.

Deliberately excluded from legacy test_*.py discovery to preserve the frozen
v48 baseline. Requires six explicit flags only within these tests, no real
login, user data, HTTP, outbound sockets, or paid API calls.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from backend import channel_store, memory_explicit_actions
from backend.memory_auth_epoch_store import (
    AuthEpochRegistry, AuthenticatedLogin,
)
from backend.memory_confirmation_uow_gate import (
    MemoryUowConfirmationGate, UowConfirmationGateError,
)
from backend.memory_shared_confirmation_ticket import (
    SharedConfirmationTickets, TrustedTicketContext,
)
from backend.memory_evidence_readonly import build_snapshot
from backend.tests._memory_session_confirm_worker import valid_request
from backend.tests._support import NoNetworkMixin
from backend.tests.test_memory_service import bootstrap_runtime, memory_config

FLAGS = (
    "MEMORY_ATOMIC_CONFIRMED_REVIEW_ENABLED",
    "MEMORY_SESSION_BOUND_WRITE_ENABLED",
    "MEMORY_SESSION_CONFIRMATION_ENABLED",
    "MEMORY_TRUSTED_AUTH_EPOCH_ENABLED",
    "MEMORY_SHARED_CONFIRMATION_TICKETS_ENABLED",
    "MEMORY_CONFIRMED_UOW_GATE_ENABLED",
)


class AtomicConfirmedMemoryTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / "atomic-confirmed.sqlite")
        flag_patch = mock.patch.dict(os.environ, {
            **{key: "true" for key in FLAGS},
            "OPENAI_API_KEY": "",
        })
        flag_patch.start()
        self.addCleanup(flag_patch.stop)
        with closing(channel_store.connect(self.path)) as conn:
            conn.execute(channel_store.RELAY_TABLE_DDL["messages"])
        channel_store.run_migrations(self.path)
        runtime = bootstrap_runtime(self.path, memory_config())
        self.backend = memory_explicit_actions.create_entry_backend(
            runtime.privileged_actions
        )
        self.state = {
            "principal": "synthetic-user",
            "login": "L" * 32,
            "epoch": None,
            "session": "s1",
            "rows": ({"id": "s1"}, {"id": "s2"}),
        }
        self.registry = AuthEpochRegistry(
            path=self.path,
            identity_provider=lambda: AuthenticatedLogin(
                self.state["principal"], self.state["login"],
            ),
        )
        self.state["epoch"] = self.registry.issue_after_authenticated_login()
        self.provider = lambda: TrustedTicketContext(
            principal_id=self.state["principal"],
            login_id=self.state["login"],
            auth_epoch=self.state["epoch"].value,
            session_id=self.state["session"],
            canonical_sessions=self.state["rows"],
        )
        self.tickets = SharedConfirmationTickets(
            path=self.path, context_provider=self.provider,
        )
        self.gate = MemoryUowConfirmationGate(
            path=self.path, context_provider=self.provider,
        )
        self.request = valid_request()
        self.offer = self.tickets.prepare(self.request)

    def count(self, table):
        with closing(channel_store.connect(self.path)) as conn:
            return conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def status(self, offer=None):
        import hashlib
        token = (offer or self.offer).ticket
        token_sha = hashlib.sha256(token.encode("ascii")).hexdigest()
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute(
                "SELECT status FROM memory_shared_ticket_v1 WHERE ticket_sha256=?",
                (token_sha,),
            ).fetchone()[0]

    def commit(self, request=None, offer=None, **changes):
        offer = offer or self.offer
        kwargs = dict(
            gate=self.gate, ticket=offer.ticket,
            content_sha256=offer.content_sha256,
            confirmation_text=offer.confirmation_text,
        )
        kwargs.update(changes)
        return self.backend._remember_confirmed_offline_review(
            request or self.request, **kwargs,
        )

    def test_success_commits_ticket_ledger_canonical_and_provenance_together(self):
        result = self.commit()
        self.assertEqual(result.status, "completed")
        self.assertFalse(result.replayed)
        self.assertEqual(self.status(), "consumed")
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(self.count("memory_items"), 1)
        self.assertEqual(self.count("memory_action_requests"), 1)
        with closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute(
                "SELECT direction,kind,meta FROM messages"
            ).fetchone()
            self.assertEqual((row[0], row[1]), ("in", "user"))
            self.assertEqual(json.loads(row[2]), {
                "channel": "web", "source": "relay", "api_session": "s1",
            })
            self.assertGreaterEqual(conn.execute(
                "SELECT COUNT(*) FROM memory_sources"
            ).fetchone()[0], 1)
        with closing(channel_store.connect_read_only(
            str(Path(self.path).resolve()), timeout_seconds=2.0,
        )) as conn:
            snapshot = build_snapshot(
                conn, session_id="s1", allowed_sessions=("s1", "s2"),
            )
        self.assertEqual(snapshot["views"][0]["answer"], self.request.content)

    def test_same_ticket_denied_after_success(self):
        self.commit()
        with self.assertRaises(UowConfirmationGateError):
            self.commit()
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(self.count("memory_action_requests"), 1)

    def test_new_ticket_same_request_replays_existing_signed_terminal(self):
        self.commit()
        second_offer = self.tickets.prepare(self.request)
        result = self.commit(offer=second_offer)
        self.assertTrue(result.replayed)
        self.assertEqual(self.status(second_offer), "consumed")
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(self.count("memory_items"), 1)
        self.assertEqual(self.count("memory_action_requests"), 1)

    def test_disabled_experiment_refuses_even_with_valid_ticket(self):
        os.environ["MEMORY_ATOMIC_CONFIRMED_REVIEW_ENABLED"] = "false"
        with self.assertRaises(memory_explicit_actions.ExplicitMemoryActionError):
            self.commit()
        self.assertEqual(self.status(), "pending")
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.count("memory_action_requests"), 0)

    def test_every_required_flag_fails_closed(self):
        for flag in FLAGS:
            with self.subTest(flag=flag):
                os.environ[flag] = "false"
                with self.assertRaises(Exception):
                    self.commit()
                os.environ[flag] = "true"
                self.assertEqual(self.status(), "pending")
                self.assertEqual(self.count("messages"), 0)

    def test_wrong_digest_phrase_or_content_never_writes(self):
        for changes in (
            {"content_sha256": "0" * 64},
            {"confirmation_text": "I agree"},
        ):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(UowConfirmationGateError):
                    self.commit(**changes)
                self.assertEqual(self.status(), "pending")
                self.assertEqual(self.count("messages"), 0)
        changed = valid_request(content="Different synthetic memory")
        with self.assertRaises(UowConfirmationGateError):
            self.commit(request=changed)
        self.assertEqual(self.status(), "pending")
        self.assertEqual(self.count("memory_action_requests"), 0)

    def test_logout_before_commit_denied(self):
        self.assertTrue(self.registry.revoke_before_logout())
        with self.assertRaises(UowConfirmationGateError):
            self.commit()
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.status(), "pending")

    def test_rotated_epoch_before_commit_denied(self):
        self.state["epoch"] = self.registry.issue_after_authenticated_login()
        with self.assertRaises(UowConfirmationGateError):
            self.commit()
        self.assertEqual(self.count("messages"), 0)

    def test_switch_session_or_principal_before_commit_denied(self):
        self.state["session"] = "s2"
        with self.assertRaises(UowConfirmationGateError):
            self.commit()
        self.state["session"] = "s1"
        self.state["principal"] = "synthetic-other"
        self.state["epoch"] = self.registry.issue_after_authenticated_login()
        with self.assertRaises(UowConfirmationGateError):
            self.commit()
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.status(), "pending")

    def test_exception_after_canonical_insert_rolls_back_ticket_and_ledger(self):
        actions_cls = type(self.backend._actions)
        with mock.patch.object(
            actions_cls, "remember_explicit_user_message",
            side_effect=RuntimeError("synthetic_interrupt_after_canonical"),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic_interrupt"):
                self.commit()
        self.assertEqual(self.status(), "pending")
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.count("memory_items"), 0)
        self.assertEqual(self.count("memory_action_requests"), 0)
        # After confirmed rollback, a new explicit local attempt can succeed.
        result = self.commit()
        self.assertEqual(result.status, "completed")
        self.assertEqual(self.count("messages"), 1)

    def test_kill_switch_after_action_rolls_back_entire_transaction(self):
        cls = type(self.backend._actions)
        original = cls.remember_explicit_user_message

        def disable_after_action(instance, *args, **kwargs):
            result = original(instance, *args, **kwargs)
            os.environ["MEMORY_ATOMIC_CONFIRMED_REVIEW_ENABLED"] = "false"
            return result

        with mock.patch.object(
            cls, "remember_explicit_user_message", new=disable_after_action,
        ):
            with self.assertRaises(memory_explicit_actions.ExplicitMemoryActionError):
                self.commit()
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.count("memory_items"), 0)
        self.assertEqual(self.count("memory_action_requests"), 0)
        self.assertEqual(self.status(), "pending")
        os.environ["MEMORY_ATOMIC_CONFIRMED_REVIEW_ENABLED"] = "true"
        self.assertEqual(self.commit().status, "completed")

    def test_writer_lock_blocks_logout_until_atomic_commit(self):
        cls = type(self.backend._actions)
        original = cls.remember_explicit_user_message
        writing = threading.Event()
        release = threading.Event()
        revoke_finished = threading.Event()
        results = []
        errors = []

        def pause_while_in_uow(instance, *args, **kwargs):
            result = original(instance, *args, **kwargs)
            writing.set()
            if not release.wait(timeout=4):
                raise RuntimeError("synthetic_lock_timeout")
            return result

        def writer():
            try:
                results.append(self.commit())
            except Exception as exc:
                errors.append(type(exc).__name__)

        def revoke():
            try:
                self.registry.revoke_before_logout()
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                revoke_finished.set()

        with mock.patch.object(
            cls, "remember_explicit_user_message", new=pause_while_in_uow,
        ):
            w = threading.Thread(target=writer)
            w.start()
            try:
                self.assertTrue(writing.wait(timeout=4))
                r = threading.Thread(target=revoke)
                r.start()
                time.sleep(0.15)
                self.assertFalse(revoke_finished.is_set())
                release.set()
                w.join(timeout=7)
                r.join(timeout=7)
                self.assertTrue(all(not t.is_alive() for t in (w, r)))
            finally:
                release.set()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].replayed)
        self.assertTrue(revoke_finished.is_set())
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(self.status(), "consumed")

    def test_no_route_or_public_service_method_added(self):
        service = memory_explicit_actions.bind_operator_cli(self.backend)
        self.assertFalse(hasattr(service, "remember_confirmed"))
        self.assertFalse(hasattr(service, "_remember_confirmed_offline_review"))
        self.assertEqual(self.count("messages"), 0)


if __name__ == "__main__":
    unittest.main()
