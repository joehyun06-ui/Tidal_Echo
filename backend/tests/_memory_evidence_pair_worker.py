"""Isolated end-to-end ASGI pairing: relay auth -> internal loop -> canonical SQLite.

Not discovered by the legacy unittest suite. No network, keys, provider calls or real data.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import httpx
from fastapi import HTTPException

from backend.tests._support import NoNetworkMixin, load_app, request

TOKEN = "test-internal-loop-token-1234567890"
TIMESTAMP = "2026-10-25T00:00:00+00:00"


class MemoryEvidencePairedASGITests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = load_app(
            self.temp.name, telegram=False, memory=True,
            memory_secret="SYNTHETIC_MEMORY_TEST_SECRET_WITHOUT_REAL_CREDENTIALS",
        )
        self.dbpath = Path(os.environ["RELAY_DB"])
        os.environ.update({
            "MEMORY_EVIDENCE_UI_READONLY_ENABLED": "true",
            "LEGACY_CHAT_BRIDGE_TOKEN": "synthetic-legacy-bridge-token-1234567890",
            "LEGACY_CHAT_BRIDGE_SESSION": "test-legacy",
            "CODEX_CONTROL_ENABLED": "false",
            "RENDER_TELEGRAM_MVP": "false",
        })
        for name in ("backend.p3_relay_app", "backend.legacy_chat_bridge_app", "examples.api_loop"):
            sys.modules.pop(name, None)
            package = sys.modules.get("backend")
            if package is not None and name.startswith("backend."):
                if hasattr(package, name.rsplit(".", 1)[-1]):
                    delattr(package, name.rsplit(".", 1)[-1])
        self.addCleanup(sys.modules.pop, "examples.api_loop", None)
        self.addCleanup(sys.modules.pop, "backend.p3_relay_app", None)
        self.addCleanup(sys.modules.pop, "backend.legacy_chat_bridge_app", None)
        self.loop = importlib.import_module("examples.api_loop")
        self.relay = importlib.import_module("backend.p3_relay_app")
        self.assertIs(self.relay.relay_app.app, self.relay.app)
        self.assertEqual(self.loop.API_LOOP_INTERNAL_TOKEN, TOKEN)
        self.assertEqual(self.relay.relay_app.API_LOOP_INTERNAL_TOKEN, TOKEN)
        self.sessions = mock.patch.object(
            self.loop, "session_rows",
            return_value=[{"id": "s1"}, {"id": "s2"}],
        )
        self.sessions.start()
        self.addCleanup(self.sessions.stop)
        self._insert_real_schema_fixture()

    def _insert_real_schema_fixture(self):
        from backend import channel_store
        with channel_store.connect(str(self.dbpath)) as conn:
            found = conn.execute(
                "SELECT name FROM sqlite_master WHERE name IN "
                "('memory_items','memory_sources','memory_evidence_events')"
            ).fetchall()
            self.assertEqual(len(found), 3, "test requires the actual migrated schema")
            conn.execute(
                """INSERT INTO messages(ts,direction,kind,text,meta)
                   VALUES(?,?,?,?,?)""",
                (TIMESTAMP, "in", "user", "Synthetic source fact",
                 json.dumps({"channel": "web", "source": "relay", "api_session": "s1"})),
            )
            mid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                """INSERT INTO memory_items(
                    memory_key,kind,scope_type,scope_ref,normalized_content,
                    normalized_fingerprint,fingerprint_version,status,explicitness,
                    confidence,sensitivity,first_observed_at,last_confirmed_at,
                    created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("m" * 32, "project", "session", "s1", "Aurora demo: Friday 16:30",
                 bytes(range(32)), 1, "active", "explicit", 1.0, "normal",
                 TIMESTAMP, TIMESTAMP, TIMESTAMP, TIMESTAMP),
            )
            memory_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                """INSERT INTO memory_evidence_events(
                    canonical_message_id,action_id,action_type,action_binding_version,
                    evidence_type,reality_scope,subject_scope,created_by_component,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (mid, "a"*32, "remember_explicit_user", 1, "explicit_user_memory",
                 "real", "user", "web_adapter", TIMESTAMP),
            )
            event_id=conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                """INSERT INTO memory_sources(
                    memory_id,canonical_message_id,evidence_event_id,
                    channel,source,evidence_role,evidence_type,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (memory_id, mid, event_id, "web", "relay", "user",
                 "explicit_user_memory", TIMESTAMP),
            )

    def _add_second_canonical_source(self, metadata: dict):
        """Attach a second provenance row using the production migrated schema."""
        from backend import channel_store
        with channel_store.connect(str(self.dbpath)) as conn:
            conn.execute(
                """INSERT INTO messages(ts,direction,kind,text,meta)
                   VALUES(?,?,?,?,?)""",
                (TIMESTAMP, "in", "user", "Synthetic secondary source",
                 json.dumps(metadata)),
            )
            second_msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            memory_id = conn.execute(
                "SELECT id FROM memory_items WHERE memory_key=?",
                ("m" * 32,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO memory_evidence_events(
                    canonical_message_id,action_id,action_type,action_binding_version,
                    evidence_type,reality_scope,subject_scope,created_by_component,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (second_msg_id, "b" * 32, "remember_explicit_user", 1,
                 "explicit_user_memory", "real", "user", "web_adapter", TIMESTAMP),
            )
            event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                """INSERT INTO memory_sources(
                    memory_id,canonical_message_id,evidence_event_id,
                    channel,source,evidence_role,evidence_type,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (memory_id, second_msg_id, event_id, "web", "relay", "user",
                 "explicit_user_memory", TIMESTAMP),
            )

    async def test_mixed_cross_session_provenance_is_withheld_in_real_schema(self):
        self._add_second_canonical_source(
            {"channel": "web", "source": "relay", "api_session": "s2"},
        )
        response, _ = await self._relay("s1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["views"], [])

    async def test_mixed_legacy_unbound_provenance_is_withheld_in_real_schema(self):
        self._add_second_canonical_source(
            {"channel": "web", "source": "relay"},
        )
        response, _ = await self._relay("s1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["views"], [])

    async def _relay(self, session_id="s1", *, auth=True, forward=True):
        """Bridge the REAL two ASGI routers in memory, avoiding live sockets."""
        calls = []
        def forward_internal(path, method="GET", body=None):
            calls.append((path, method))
            async def invoke():
                transport = httpx.ASGITransport(app=self.loop.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://internal.test") as client:
                    reply = await client.request(
                        method, path, json=body,
                        headers={"X-API-Loop-Internal-Token": TOKEN},
                    )
                if reply.status_code >= 400:
                    raise HTTPException(status_code=reply.status_code, detail="internal_failure")
                return reply.json()
            with ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(lambda: asyncio.run(invoke())).result(timeout=5)
        with mock.patch.object(
            self.relay.relay_app, "loop_json",
            side_effect=forward_internal if forward else AssertionError("unauthorized_reached_loop"),
        ):
            response = await request(
                self.relay, "GET", f"/app/sessions/{session_id}/memory/evidence",
                headers={"Authorization": "Bearer test-relay-secret"} if auth else {},
            )
        return response, calls

    async def test_relay_to_loop_to_canonical_sqlite_returns_bounded_source(self):
        response, calls = await self._relay()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [("/loop/sessions/s1/memory/evidence", "GET")])
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        value = response.json()
        self.assertEqual(value["api_session"], "s1")
        self.assertEqual(value["mode"], "server_readonly")
        self.assertEqual(len(value["views"]), 1)
        self.assertEqual(value["views"][0]["answer"], "Aurora demo: Friday 16:30")
        self.assertEqual(len(value["views"][0]["source_ids"]), 1)

    async def test_other_session_cannot_read_first_session(self):
        response, _ = await self._relay("s2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["views"], [])

    async def test_legacy_source_without_session_metadata_is_withheld(self):
        from backend import channel_store
        with channel_store.connect(str(self.dbpath)) as conn:
            conn.execute("UPDATE messages SET meta=? WHERE text='Synthetic source fact'",
                         (json.dumps({"channel": "web", "source": "relay"}),))
        response, _ = await self._relay()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["views"], [])

    async def test_sensitive_memory_is_withheld(self):
        from backend import channel_store
        with channel_store.connect(str(self.dbpath)) as conn:
            conn.execute("UPDATE memory_items SET sensitivity='sensitive'")
        response, _ = await self._relay()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["views"], [])

    async def test_missing_relay_auth_never_reaches_internal_loop(self):
        response, calls = await self._relay(auth=False, forward=False)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(calls, [])

    async def test_disabled_gate_never_reaches_internal_loop(self):
        del os.environ["MEMORY_EVIDENCE_UI_READONLY_ENABLED"]
        response, calls = await self._relay(forward=False)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(calls, [])

    async def test_missing_session_rejected_inside_loop(self):
        response, calls = await self._relay("missing")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(calls, [("/loop/sessions/missing/memory/evidence", "GET")])


if __name__ == "__main__":
    unittest.main()
