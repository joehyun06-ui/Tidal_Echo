"""Independent offline integration of opt-in server-owned session write binding.

This worker is intentionally not named test_*.py: importing the memory
composition multiple times in the historical unittest suite has side effects.
Runs with synthetic SQLite/secret only, no sockets or HTTP exposure.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from backend import channel_store, memory_explicit_actions
from backend.memory_evidence_readonly import build_snapshot
from backend.tests._support import NoNetworkMixin
from backend.tests.test_memory_service import bootstrap_runtime, memory_config


class ServerBoundMemoryWriteTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "session-write-test.sqlite")
        with channel_store.connect(self.path) as conn:
            conn.execute(channel_store.RELAY_TABLE_DDL["messages"])
        channel_store.run_migrations(self.path)
        runtime = bootstrap_runtime(self.path, memory_config())
        backend = memory_explicit_actions.create_entry_backend(runtime.privileged_actions)
        self.service = memory_explicit_actions.bind_operator_cli(backend)
        self.other_origin = memory_explicit_actions.bind_mcp(backend)
        os.environ["MEMORY_SESSION_BOUND_WRITE_ENABLED"] = "false"
        self.sessions = [{"id": "s1", "title": "Synthetic A"},
                         {"id": "s2", "title": "Synthetic B"}]

    def make_request(self, session="s1", marker="a", content="Aurora demo Friday at 16:30"):
        return memory_explicit_actions.RememberExplicitMemoryRequest(
            marker * 32, "project", "session", session, content, "normal",
        )

    def write(self, request=None, *, context="s1", sessions=None):
        return self.service.remember_explicit_user_memory_for_server_session(
            request if request is not None else self.make_request(),
            trusted_session_id=context,
            canonical_sessions=sessions if sessions is not None else self.sessions,
        )

    def count(self, table):
        with channel_store.connect(self.path) as conn:
            return conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def test_disabled_has_no_canonical_or_memory_write(self):
        with self.assertRaisesRegex(
            memory_explicit_actions.ExplicitMemoryActionError, "session_write_disabled",
        ):
            self.write()
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.count("memory_items"), 0)
        self.assertEqual(self.count("memory_action_requests"), 0)

    def test_enabled_canonical_and_source_bound_in_original_transaction(self):
        os.environ["MEMORY_SESSION_BOUND_WRITE_ENABLED"] = "true"
        result = self.write()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.scope_type, "session")
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(self.count("memory_action_requests"), 1)
        with channel_store.connect(self.path) as conn:
            row = conn.execute(
                "SELECT direction,kind,meta FROM messages"
            ).fetchone()
            self.assertEqual((row[0], row[1]), ("in", "user"))
            self.assertEqual(
                json.loads(row[2]),
                {"channel": "web", "source": "relay", "api_session": "s1"},
            )
            assert conn.execute(
                "SELECT COUNT(*) FROM memory_sources JOIN memory_evidence_events "
                "ON memory_sources.evidence_event_id=memory_evidence_events.id"
            ).fetchone()[0] >= 1
        with channel_store.connect_read_only(
            str(Path(self.path).resolve()), timeout_seconds=2.0,
        ) as conn:
            snapshot = build_snapshot(
                conn, session_id="s1", allowed_sessions=("s1", "s2"),
            )
            other = build_snapshot(
                conn, session_id="s2", allowed_sessions=("s1", "s2"),
            )
        self.assertEqual(len(snapshot["views"]), 1)
        self.assertEqual(snapshot["views"][0]["answer"], "Aurora demo Friday at 16:30")
        self.assertEqual(other["views"], [])

    def test_client_claiming_other_session_is_rejected_without_side_effects(self):
        os.environ["MEMORY_SESSION_BOUND_WRITE_ENABLED"] = "true"
        with self.assertRaisesRegex(
            memory_explicit_actions.ExplicitMemoryActionError,
            "session_write_binding_invalid",
        ):
            self.write(self.make_request("s2"), context="s1")
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.count("memory_action_requests"), 0)

    def test_unknown_session_is_rejected(self):
        os.environ["MEMORY_SESSION_BOUND_WRITE_ENABLED"] = "true"
        with self.assertRaisesRegex(
            memory_explicit_actions.ExplicitMemoryActionError,
            "session_write_binding_invalid",
        ):
            self.write(context="s1", sessions=[{"id":"s2"}])
        self.assertEqual(self.count("messages"), 0)

    def test_duplicate_server_session_ids_are_rejected(self):
        os.environ["MEMORY_SESSION_BOUND_WRITE_ENABLED"] = "true"
        with self.assertRaisesRegex(
            memory_explicit_actions.ExplicitMemoryActionError,
            "session_write_binding_invalid",
        ):
            self.write(sessions=[{"id":"s1"},{"id":"s1"}])
        self.assertEqual(self.count("messages"), 0)

    def test_foreign_origin_is_rejected(self):
        os.environ["MEMORY_SESSION_BOUND_WRITE_ENABLED"] = "true"
        with self.assertRaisesRegex(
            memory_explicit_actions.ExplicitMemoryActionError,
            "session_write_binding_invalid",
        ):
            self.other_origin.remember_explicit_user_memory_for_server_session(
                self.make_request(), trusted_session_id="s1",
                canonical_sessions=self.sessions,
            )
        self.assertEqual(self.count("messages"), 0)

    def test_idempotent_replay_creates_no_second_canonical_message(self):
        os.environ["MEMORY_SESSION_BOUND_WRITE_ENABLED"] = "true"
        first = self.write()
        repeated = self.write()
        self.assertFalse(first.replayed)
        self.assertTrue(repeated.replayed)
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(self.count("memory_action_requests"), 1)

    def test_same_request_id_other_session_is_conflict_not_replay(self):
        os.environ["MEMORY_SESSION_BOUND_WRITE_ENABLED"] = "true"
        self.write()
        before = self.count("messages")
        with self.assertRaises(
            memory_explicit_actions.ExplicitMemoryActionError,
        ):
            self.write(self.make_request(session="s2"),
                       context="s2")
        self.assertEqual(self.count("messages"), before)
        self.assertEqual(self.count("memory_action_requests"), 1)

    def test_legacy_write_does_not_add_new_session_metadata(self):
        result = self.service.remember_explicit_user_memory(
            memory_explicit_actions.RememberExplicitMemoryRequest(
                "b"*32,"project","global_user","",
                "Synthetic legacy global memory","normal"
            )
        )
        self.assertEqual(result.status,"completed")
        with channel_store.connect(self.path) as conn:
            data = json.loads(conn.execute("SELECT meta FROM messages").fetchone()[0])
        self.assertEqual(data,{"channel":"web","source":"relay"})

    def test_bogus_request_object_and_wrong_scope_fail_closed(self):
        os.environ["MEMORY_SESSION_BOUND_WRITE_ENABLED"] = "true"
        cases = [
            object(),
            memory_explicit_actions.RememberExplicitMemoryRequest(
                "c"*32, "project", "global_user","",
                "Synthetic test","normal",
            ),
        ]
        for request in cases:
            with self.subTest(case=type(request).__name__):
                with self.assertRaises(
                    memory_explicit_actions.ExplicitMemoryActionError,
                ):
                    self.write(request)
        self.assertEqual(self.count("messages"), 0)

    def test_disabled_after_creation_still_blocks_new_session_write(self):
        os.environ["MEMORY_SESSION_BOUND_WRITE_ENABLED"] = "true"
        self.write()
        os.environ["MEMORY_SESSION_BOUND_WRITE_ENABLED"] = "false"
        with self.assertRaisesRegex(
            memory_explicit_actions.ExplicitMemoryActionError, "session_write_disabled",
        ):
            self.write(self.make_request(marker="c"))
        self.assertEqual(self.count("messages"), 1)


if __name__ == "__main__":
    unittest.main()
