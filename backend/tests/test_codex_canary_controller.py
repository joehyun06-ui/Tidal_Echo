from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend import codex_generation_provider_binding as provider_binding
from backend import codex_generation_store as store
from backend.codex_canary_controller import CodexCanaryController, CodexCanaryControllerError


class FakeProtocol:
    def __init__(self):
        self.calls = 0

    async def qualify(self):
        self.calls += 1
        return SimpleNamespace(model="gpt-5.6-sol", reasoning_effort="high")


class CodexCanaryControllerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store_path = self.root / "codex-generation.db"
        self.relay_db = self.root / "relay.db"
        store.initialize(self.store_path)
        with sqlite3.connect(self.relay_db) as conn:
            conn.execute("""CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                direction TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                meta TEXT NOT NULL DEFAULT '{}')""")
        self.protocol = FakeProtocol()
        self.persona = "persona"
        self.controller = CodexCanaryController(
            store_path=self.store_path,
            relay_db=self.relay_db,
            protocol=self.protocol,
            persona_loader=lambda: self.persona,
        )

    def insert(self, *, text="hello", attachments=None, session="api-canary") -> int:
        meta = {
            "user": "human",
            "channel": "web",
            "source": "relay",
            "api_session": session,
            "attachments": [] if attachments is None else attachments,
        }
        with sqlite3.connect(self.relay_db) as conn:
            cur = conn.execute(
                "INSERT INTO messages(ts,direction,kind,text,meta) VALUES(?,?,?,?,?)",
                (
                    "2026-08-27T12:00:00+00:00",
                    "in",
                    "user",
                    text,
                    json.dumps(meta),
                ),
            )
            return int(cur.lastrowid)

    async def test_explicit_pin_freezes_model_effort_persona_but_not_guessed_provider(self):
        row = await self.controller.pin_session("api-canary")
        self.assertEqual(row["model"], "gpt-5.6-sol")
        self.assertEqual(row["reasoning_effort"], "high")
        self.assertEqual(row["model_provider"], provider_binding.UNRESOLVED_MODEL_PROVIDER)
        self.assertTrue(self.controller.is_pinned("api-canary"))

    async def test_normal_unpinned_session_is_not_intercepted(self):
        mid = self.insert(session="api-normal")
        result = self.controller.admit_if_pinned(
            canonical_message_id=mid,
            api_session="api-normal",
            ingress_text="hello",
            continuity_status="empty",
        )
        self.assertIsNone(result)

    async def test_pinned_plain_text_is_durably_accepted_without_generation(self):
        await self.controller.pin_session("api-canary")
        mid = self.insert()
        result = self.controller.admit_if_pinned(
            canonical_message_id=mid,
            api_session="api-canary",
            ingress_text="hello",
            continuity_status="empty",
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["provider"], "codex")
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["generation_id"], f"codex-gen-{mid}")
        self.assertEqual(result["client_message_id"], f"codex-client-{mid}")
        self.assertEqual(result["callback_identity"], f"codex-callback-{mid}")

    async def test_repeated_admission_of_same_canonical_message_is_idempotent(self):
        await self.controller.pin_session("api-canary")
        mid = self.insert()
        one = self.controller.admit_if_pinned(
            canonical_message_id=mid,
            api_session="api-canary",
            ingress_text="hello",
            continuity_status="empty",
        )
        two = self.controller.admit_if_pinned(
            canonical_message_id=mid,
            api_session="api-canary",
            ingress_text="hello",
            continuity_status="empty",
        )
        self.assertEqual(one, two)
        with store.connect(self.store_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM codex_generation_jobs").fetchone()[0], 1)

    async def test_continuity_applied_fails_closed_not_api_fallback(self):
        await self.controller.pin_session("api-canary")
        mid = self.insert()
        with self.assertRaisesRegex(CodexCanaryControllerError, "continuity_unsupported"):
            self.controller.admit_if_pinned(
                canonical_message_id=mid,
                api_session="api-canary",
                ingress_text="hello",
                continuity_status="applied",
            )
        with store.connect(self.store_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM codex_generation_jobs").fetchone()[0], 0)

    async def test_attachment_fails_closed_after_pin(self):
        await self.controller.pin_session("api-canary")
        mid = self.insert(attachments=[{"name": "photo.jpg"}])
        with self.assertRaisesRegex(CodexCanaryControllerError, "attachments_unsupported"):
            self.controller.admit_if_pinned(
                canonical_message_id=mid,
                api_session="api-canary",
                ingress_text="hello",
                continuity_status="empty",
            )

    async def test_ingress_text_must_match_canonical_text(self):
        await self.controller.pin_session("api-canary")
        mid = self.insert(text="canonical")
        with self.assertRaisesRegex(CodexCanaryControllerError, "input_contract_changed"):
            self.controller.admit_if_pinned(
                canonical_message_id=mid,
                api_session="api-canary",
                ingress_text="vision rewritten text",
                continuity_status="empty",
            )

    async def test_retired_canary_falls_out_of_codex_routing(self):
        await self.controller.pin_session("api-canary")
        self.controller.retire_session("api-canary")
        mid = self.insert()
        self.assertIsNone(self.controller.admit_if_pinned(
            canonical_message_id=mid,
            api_session="api-canary",
            ingress_text="hello",
            continuity_status="empty",
        ))

    async def test_pin_retry_after_provider_resolution_preserves_authoritative_provider(self):
        await self.controller.pin_session("api-canary")
        with store.connect(self.store_path) as conn:
            conn.execute(
                "UPDATE codex_sessions SET model_provider='openai' WHERE api_session='api-canary'"
            )
        row = await self.controller.pin_session("api-canary")
        self.assertEqual(row["model_provider"], "openai")


if __name__ == "__main__":
    unittest.main()
