"""Offline tests for one-use confirmed session writes (no HTTP/provider).

This file is deliberately an isolated CI worker to protect v48 test module
state. Synthetic SQLite, no private memory, no API secret, no network.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from backend import (
    channel_store,
    memory_explicit_actions,
    memory_session_confirmation,
)
from backend.memory_evidence_readonly import build_snapshot
from backend.tests._support import NoNetworkMixin
from backend.tests.test_memory_service import bootstrap_runtime, memory_config

ERR = memory_session_confirmation.SessionConfirmationError


def valid_request(session="s1", marker="a", content="Aurora demo Friday at 16:30"):
    return memory_explicit_actions.RememberExplicitMemoryRequest(
        marker*32, "project", "session", session, content, "normal",
    )


class FakeService:
    def __init__(self):
        self.writes = []

    def remember_explicit_user_memory_for_server_session(
        self, request, *, trusted_session_id, canonical_sessions,
    ):
        self.writes.append((request, trusted_session_id))
        return "written"


class ConfirmationProtocolTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {
            "MEMORY_SESSION_BOUND_WRITE_ENABLED":"true",
            "MEMORY_SESSION_CONFIRMATION_ENABLED":"true",
        })
        patch.start()
        self.addCleanup(patch.stop)
        self.server = {"active":"s1", "rows":[{"id":"s1"},{"id":"s2"}],
                       "principal":"synthetic-user", "epoch":"A"*32}
        self.now = 400.0
        self.service = FakeService()
        self.guard = memory_session_confirmation.SessionWriteConfirmation(
            service=self.service,
            authorization_provider=lambda: memory_session_confirmation.AuthorizedSessionContext(
                principal_id=self.server["principal"],
                auth_epoch=self.server["epoch"],
                session_id=self.server["active"],
                canonical_sessions=tuple(self.server["rows"]),
            ),
            clock=lambda: self.now,
        )

    def prepare(self, request=None):
        return self.guard.prepare(valid_request() if request is None else request)

    def confirm(self, offer, **changes):
        args={"ticket":offer.ticket, "content_sha256":offer.content_sha256,
              "confirmation_text":offer.confirmation_text}
        args.update(changes)
        return self.guard.confirm(**args)

    def test_prepare_does_not_write(self):
        offer=self.prepare()
        self.assertEqual(self.service.writes, [])
        self.assertEqual(offer.preview_content, "Aurora demo Friday at 16:30")
        self.assertEqual(offer.expires_in_seconds, 120)

    def test_confirm_requires_explicit_phrase_and_digest(self):
        offer=self.prepare()
        self.assertEqual(self.confirm(offer), "written")
        self.assertEqual(len(self.service.writes), 1)
        self.assertEqual(self.service.writes[0][1], "s1")

    def test_ticket_cannot_be_replayed(self):
        offer=self.prepare()
        self.confirm(offer)
        with self.assertRaises(ERR):
            self.confirm(offer)
        self.assertEqual(len(self.service.writes), 1)

    def test_wrong_phrase_consumes_ticket(self):
        offer=self.prepare()
        with self.assertRaises(ERR):
            self.confirm(offer, confirmation_text="okay")
        with self.assertRaises(ERR):
            self.confirm(offer)
        self.assertEqual(self.service.writes, [])

    def test_wrong_digest_consumes_ticket(self):
        offer=self.prepare()
        with self.assertRaises(ERR):
            self.confirm(offer, content_sha256="0"*64)
        with self.assertRaises(ERR):
            self.confirm(offer)
        self.assertEqual(self.service.writes, [])

    def test_ticket_expires_strictly(self):
        offer=self.prepare()
        self.now += memory_session_confirmation.TTL_SECONDS
        with self.assertRaises(ERR):
            self.confirm(offer)
        self.assertEqual(self.service.writes, [])

    def test_session_changed_between_prepare_and_confirm(self):
        offer=self.prepare()
        self.server["active"] = "s2"
        with self.assertRaises(ERR):
            self.confirm(offer)
        self.assertEqual(self.service.writes, [])

    def test_session_removed_after_prepare(self):
        offer=self.prepare()
        self.server["rows"]=[{"id":"s2"}]
        with self.assertRaises(ERR):
            self.confirm(offer)
        self.assertEqual(self.service.writes, [])

    def test_different_principal_cannot_redeem_old_ticket(self):
        offer = self.prepare()
        self.server["principal"] = "other-synthetic-user"
        with self.assertRaises(ERR):
            self.confirm(offer)
        self.server["principal"] = "synthetic-user"
        with self.assertRaises(ERR):
            self.confirm(offer)
        self.assertEqual(self.service.writes, [])

    def test_logout_login_same_user_rotates_epoch_and_revokes_ticket(self):
        offer = self.prepare()
        self.server["epoch"] = "B"*32
        with self.assertRaises(ERR):
            self.confirm(offer)
        self.server["epoch"] = "A"*32
        with self.assertRaises(ERR):
            self.confirm(offer)
        self.assertEqual(self.service.writes, [])

    def test_auth_epoch_with_invalid_format_fails_closed(self):
        self.server["epoch"] = "short"
        with self.assertRaises(ERR):
            self.prepare()
        self.assertEqual(self.service.writes, [])

    def test_principal_with_invalid_format_fails_closed(self):
        self.server["principal"] = ""
        with self.assertRaises(ERR):
            self.prepare()
        self.assertEqual(self.service.writes, [])

    def test_auth_provider_exception_fails_closed(self):
        self.guard._authorization_provider = lambda: 1/0
        with self.assertRaises(ERR):
            self.prepare()
        self.assertEqual(self.service.writes, [])

    def test_untyped_auth_provider_cannot_authorize(self):
        self.guard._authorization_provider = lambda: ("s1", [{"id":"s1"}])
        with self.assertRaises(ERR):
            self.prepare()
        self.assertEqual(self.service.writes, [])

    def test_rejected_auth_after_prepare_never_executes_write(self):
        offer = self.prepare()
        self.guard._authorization_provider = lambda: None
        with self.assertRaises(ERR):
            self.confirm(offer)
        with self.assertRaises(ERR):
            self.confirm(offer)
        self.assertEqual(self.service.writes, [])

    def test_auth_context_repr_does_not_expose_identity_or_epoch(self):
        context = memory_session_confirmation.AuthorizedSessionContext(
            principal_id="synthetic-user",
            auth_epoch="A"*32,
            session_id="s1",
            canonical_sessions=({"id":"s1"},),
        )
        self.assertNotIn("synthetic-user", repr(context))
        self.assertNotIn("AAAA", repr(context))

    def test_unknown_session_prepare_rejected(self):
        self.server["active"]="unknown"
        with self.assertRaises(ERR):
            self.prepare()
        self.assertEqual(self.service.writes, [])

    def test_cross_session_scope_prepare_rejected(self):
        with self.assertRaises(ERR):
            self.prepare(valid_request(session="s2"))
        self.assertEqual(self.service.writes, [])

    def test_bogus_request_and_id_rejected(self):
        for item in (object(), valid_request(marker="!")):
            with self.subTest(item=type(item).__name__):
                with self.assertRaises(ERR):
                    self.prepare(item)

    def test_sensitive_content_not_prepared(self):
        request=memory_explicit_actions.RememberExplicitMemoryRequest(
            "a"*32, "project", "session", "s1", "Private synthetic fact",
            "sensitive",
        )
        with self.assertRaises(ERR):
            self.prepare(request)

    def test_oversized_content_rejected(self):
        with self.assertRaises(ERR):
            self.prepare(valid_request(content="x"*1001))

    def test_only_eight_pending_offers(self):
        offers=[self.prepare(valid_request(marker=chr(97+i)))
                for i in range(8)]
        with self.assertRaises(ERR):
            self.prepare(valid_request(marker="z"))
        self.now += 121
        self.prepare(valid_request(marker="z"))
        self.assertEqual(len(self.service.writes),0)
        self.assertEqual(len(offers),8)

    def test_clear_pending_revokes_all(self):
        a=self.prepare()
        self.guard.clear_pending()
        with self.assertRaises(ERR):
            self.confirm(a)

    def test_offer_repr_never_reveals_preview_or_ticket(self):
        offer=self.prepare()
        self.assertNotIn(offer.ticket, repr(offer))
        self.assertNotIn("Aurora", repr(offer))

    def test_invalid_ticket_or_empty_digest_rejected(self):
        offer=self.prepare()
        with self.assertRaises(ERR):
            self.guard.confirm(ticket=10, content_sha256=offer.content_sha256,
                               confirmation_text=offer.confirmation_text)
        with self.assertRaises(ERR):
            self.confirm(offer, content_sha256="")

    def test_disabled_prepare_rejected(self):
        os.environ["MEMORY_SESSION_CONFIRMATION_ENABLED"]="false"
        with self.assertRaises(ERR):
            self.prepare()
        self.assertEqual(self.service.writes, [])

    def test_write_gate_also_required(self):
        os.environ["MEMORY_SESSION_BOUND_WRITE_ENABLED"]="false"
        with self.assertRaises(ERR):
            self.prepare()

    def test_disabled_between_prepare_and_confirm_revokes_ticket(self):
        offer=self.prepare()
        os.environ["MEMORY_SESSION_CONFIRMATION_ENABLED"]="false"
        with self.assertRaises(ERR):
            self.confirm(offer)
        os.environ["MEMORY_SESSION_CONFIRMATION_ENABLED"]="true"
        with self.assertRaises(ERR):
            self.confirm(offer)
        self.assertEqual(self.service.writes, [])

    def test_write_failure_is_one_attempt_only(self):
        offer=self.prepare()
        def fail(*args,**kwargs):
            raise RuntimeError("synthetic_write_failure")
        self.service.remember_explicit_user_memory_for_server_session=fail
        with self.assertRaises(RuntimeError):
            self.confirm(offer)
        with self.assertRaises(ERR):
            self.confirm(offer)

    def test_simultaneous_double_submit_calls_service_once(self):
        offer=self.prepare()
        results=[]
        lock=threading.Lock()
        def slow(*args,**kwargs):
            time.sleep(0.04)
            with lock:
                results.append("written")
            return "written"
        self.service.remember_explicit_user_memory_for_server_session=slow
        errors=[]
        def call():
            try:
                self.confirm(offer)
            except ERR:
                errors.append("replay_denied")
        threads=[threading.Thread(target=call) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(results, ["written"])
        self.assertEqual(errors, ["replay_denied"])


class ConfirmationSqliteIntegrationTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch=mock.patch.dict(os.environ,{
            "MEMORY_SESSION_BOUND_WRITE_ENABLED":"true",
            "MEMORY_SESSION_CONFIRMATION_ENABLED":"true",
        })
        patch.start()
        self.addCleanup(patch.stop)
        self.path=str(Path(self.temp.name)/"session-confirmed.sqlite")
        with channel_store.connect(self.path) as conn:
            conn.execute(channel_store.RELAY_TABLE_DDL["messages"])
        channel_store.run_migrations(self.path)
        runtime=bootstrap_runtime(self.path,memory_config())
        backend=memory_explicit_actions.create_entry_backend(runtime.privileged_actions)
        service=memory_explicit_actions.bind_operator_cli(backend)
        self.active="s1"
        self.principal="synthetic-user"
        self.epoch="A"*32
        self.coordinator=memory_session_confirmation.SessionWriteConfirmation(
            service=service,
            authorization_provider=lambda: memory_session_confirmation.AuthorizedSessionContext(
                principal_id=self.principal, auth_epoch=self.epoch,
                session_id=self.active, canonical_sessions=({"id":"s1"},{"id":"s2"}),
            ),
        )

    def _count(self,name):
        with channel_store.connect(self.path) as conn:
            return conn.execute("SELECT COUNT(*) FROM "+name).fetchone()[0]

    def test_prepare_zero_writes_commit_one_and_provenance_visible(self):
        request=valid_request()
        offer=self.coordinator.prepare(request)
        self.assertEqual(self._count("messages"),0)
        self.assertEqual(self._count("memory_action_requests"),0)
        result=self.coordinator.confirm(
            ticket=offer.ticket,
            content_sha256=offer.content_sha256,
            confirmation_text=offer.confirmation_text,
        )
        self.assertEqual(result.status,"completed")
        self.assertEqual(self._count("messages"),1)
        self.assertEqual(self._count("memory_action_requests"),1)
        with channel_store.connect_read_only(
            str(Path(self.path).resolve()),timeout_seconds=2.0,
        ) as conn:
            value=build_snapshot(conn,session_id="s1",allowed_sessions=("s1","s2"))
        self.assertEqual(value["views"][0]["answer"],
                         "Aurora demo Friday at 16:30")
        with self.assertRaises(ERR):
            self.coordinator.confirm(
                ticket=offer.ticket,content_sha256=offer.content_sha256,
                confirmation_text=offer.confirmation_text,
            )
        self.assertEqual(self._count("messages"),1)

    def test_new_offer_same_request_id_preserves_ledger_idempotency(self):
        first=self.coordinator.prepare(valid_request())
        self.coordinator.confirm(
            ticket=first.ticket,content_sha256=first.content_sha256,
            confirmation_text=first.confirmation_text,
        )
        second=self.coordinator.prepare(valid_request())
        result=self.coordinator.confirm(
            ticket=second.ticket,content_sha256=second.content_sha256,
            confirmation_text=second.confirmation_text,
        )
        self.assertTrue(result.replayed)
        self.assertEqual(self._count("messages"),1)
        self.assertEqual(self._count("memory_action_requests"),1)

    def test_changed_login_epoch_refuses_sqlite_commit(self):
        offer = self.coordinator.prepare(valid_request())
        self.epoch = "B"*32
        with self.assertRaises(ERR):
            self.coordinator.confirm(
                ticket=offer.ticket, content_sha256=offer.content_sha256,
                confirmation_text=offer.confirmation_text,
            )
        self.assertEqual(self._count("messages"), 0)
        self.assertEqual(self._count("memory_action_requests"), 0)

    def test_changed_principal_refuses_sqlite_commit(self):
        offer = self.coordinator.prepare(valid_request())
        self.principal = "someone-else"
        with self.assertRaises(ERR):
            self.coordinator.confirm(
                ticket=offer.ticket, content_sha256=offer.content_sha256,
                confirmation_text=offer.confirmation_text,
            )
        self.assertEqual(self._count("messages"), 0)
        self.assertEqual(self._count("memory_action_requests"), 0)

    def test_revalidate_active_session_before_sqlite_commit(self):
        offer=self.coordinator.prepare(valid_request())
        self.active="s2"
        with self.assertRaises(ERR):
            self.coordinator.confirm(
                ticket=offer.ticket,content_sha256=offer.content_sha256,
                confirmation_text=offer.confirmation_text,
            )
        self.assertEqual(self._count("messages"),0)
        self.assertEqual(self._count("memory_action_requests"),0)


if __name__=="__main__":
    unittest.main()
