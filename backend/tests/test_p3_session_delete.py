from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from backend import codex_generation_store
from backend import web_session_delete
from backend import web_session_provider_authority
from backend.tests._support import NoNetworkMixin, load_app, request


class FakeLegacy:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.saved = []
        self.counter = 0

    def load_config(self):
        cfg = dict(self.cfg)
        cfg["sessions"] = [dict(row) for row in cfg.get("sessions", [])]
        if web_session_provider_authority.DELETED_SESSIONS_KEY in cfg:
            cfg[web_session_provider_authority.DELETED_SESSIONS_KEY] = [
                dict(row)
                for row in cfg.get(web_session_provider_authority.DELETED_SESSIONS_KEY, [])
            ]
        return cfg

    def save_config(self, cfg):
        self.cfg = dict(cfg)
        self.cfg["sessions"] = [dict(row) for row in cfg.get("sessions", [])]
        if web_session_provider_authority.DELETED_SESSIONS_KEY in cfg:
            self.cfg[web_session_provider_authority.DELETED_SESSIONS_KEY] = [
                dict(row)
                for row in cfg.get(web_session_provider_authority.DELETED_SESSIONS_KEY, [])
            ]
        self.saved.append(self.load_config())

    def now_iso(self):
        self.counter += 1
        return f"2026-08-30T00:00:{self.counter:02d}+00:00"


def session_row(session_id: str, provider: str = "api"):
    return {
        "id": session_id,
        "title": session_id,
        "since_id": 0,
        "created_at": "2026-08-30T00:00:00+00:00",
        "pinned": False,
        "provider": provider,
    }


def create_relay_db(path: Path, *, with_reference_table: bool = False) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            direction TEXT NOT NULL,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            meta TEXT NOT NULL DEFAULT '{}')""")
        if with_reference_table:
            conn.execute("""CREATE TABLE memory_refs (
                id INTEGER PRIMARY KEY,
                canonical_message_id INTEGER NOT NULL,
                FOREIGN KEY(canonical_message_id) REFERENCES messages(id) ON DELETE RESTRICT)""")
        conn.commit()


def insert_message(path: Path, *, text: str, api_session: str = "", attachments=None) -> int:
    meta = {"channel": "web", "attachments": list(attachments or [])}
    if api_session:
        meta["api_session"] = api_session
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute(
            "INSERT INTO messages(ts,direction,kind,text,meta) VALUES(?,?,?,?,?)",
            (
                "2026-08-30T00:00:00+00:00",
                "in",
                "user",
                text,
                json.dumps(meta),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


class ConversationPurgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "relay.db"
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()

    def test_unreferenced_messages_are_physically_deleted_and_orphan_upload_removed(self):
        create_relay_db(self.db)
        orphan = self.uploads / "att-orphan.txt"
        shared = self.uploads / "att-shared.txt"
        orphan.write_text("private", encoding="utf-8")
        shared.write_text("shared", encoding="utf-8")
        insert_message(
            self.db,
            text="delete me",
            api_session="api-delete",
            attachments=[
                {"url": "/uploads/att-orphan.txt", "kind": "file"},
                {"url": "/uploads/att-shared.txt", "kind": "file"},
            ],
        )
        insert_message(
            self.db,
            text="keep me",
            api_session="api-keep",
            attachments=[{"url": "/uploads/att-shared.txt", "kind": "file"}],
        )

        result = web_session_delete.purge_messages(
            self.db,
            session_id="api-delete",
            upload_dir=self.uploads,
        )

        self.assertEqual(result["messages_purged"], 1)
        self.assertEqual(result["messages_deleted"], 1)
        self.assertEqual(result["messages_redacted"], 0)
        self.assertEqual(result["attachments_deleted"], 1)
        self.assertEqual(result["attachments_retained"], 1)
        self.assertEqual(result["attachment_cleanup_failed"], 0)
        self.assertFalse(orphan.exists())
        self.assertTrue(shared.exists())
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute("SELECT text FROM messages ORDER BY id").fetchall()
        self.assertEqual(rows, [("keep me",)])

    def test_foreign_key_referenced_message_is_content_redacted_not_left_readable(self):
        create_relay_db(self.db, with_reference_table=True)
        attachment = self.uploads / "att-private.txt"
        attachment.write_text("private", encoding="utf-8")
        message_id = insert_message(
            self.db,
            text="sensitive conversation text",
            api_session="api-delete",
            attachments=[{"url": "/uploads/att-private.txt", "kind": "file"}],
        )
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT INTO memory_refs(id,canonical_message_id) VALUES(1,?)",
                (message_id,),
            )
            conn.commit()

        result = web_session_delete.purge_messages(
            self.db,
            session_id="api-delete",
            upload_dir=self.uploads,
        )

        self.assertEqual(result["messages_deleted"], 0)
        self.assertEqual(result["messages_redacted"], 1)
        self.assertEqual(result["attachments_deleted"], 1)
        self.assertFalse(attachment.exists())
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT kind,text,meta FROM messages WHERE id=?", (message_id,)).fetchone()
            ref = conn.execute("SELECT canonical_message_id FROM memory_refs").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], web_session_delete.DELETED_KIND)
        self.assertEqual(row["text"], "")
        self.assertNotIn("sensitive", row["meta"])
        self.assertNotIn("api-delete", row["meta"])
        self.assertNotIn("att-private", row["meta"])
        self.assertEqual(ref[0], message_id)

    def test_legacy_scope_purges_only_untagged_history(self):
        create_relay_db(self.db)
        insert_message(self.db, text="old main")
        insert_message(self.db, text="normal", api_session="api-keep")
        self.assertTrue(web_session_delete.legacy_available(self.db))
        result = web_session_delete.purge_messages(
            self.db,
            session_id=web_session_delete.LEGACY_SESSION_ID,
            upload_dir=self.uploads,
        )
        self.assertEqual(result["messages_purged"], 1)
        self.assertFalse(web_session_delete.legacy_available(self.db))
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute("SELECT text FROM messages").fetchall()
        self.assertEqual(rows, [("normal",)])


class HardDeleteAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "relay.db"
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        create_relay_db(self.db)
        self.store = self.root / "codex-generation.db"

    def test_delete_last_api_creates_fresh_api_and_tombstones_old_id(self):
        insert_message(self.db, text="gone", api_session="api-only")
        legacy = FakeLegacy({
            "sessions": [session_row("api-only")],
            "active_session": "api-only",
        })
        authority = web_session_provider_authority.WebSessionProviderAuthority(legacy)

        result = web_session_delete.delete_conversation(
            authority,
            "api-only",
            relay_db=self.db,
            upload_dir=self.uploads,
            codex_store=self.store,
        )

        self.assertTrue(result["deleted"]["content_deleted"])
        self.assertEqual(result["deleted"]["messages_purged"], 1)
        self.assertEqual(len(result["sessions"]), 1)
        self.assertEqual(result["sessions"][0]["provider"], "api")
        self.assertNotEqual(result["sessions"][0]["id"], "api-only")
        self.assertEqual(result["active_session"], result["sessions"][0]["id"])
        tombstones = legacy.cfg[web_session_provider_authority.DELETED_SESSIONS_KEY]
        self.assertEqual(tombstones[0]["id"], "api-only")
        self.assertEqual(tombstones[0]["provider"], "api")
        with self.assertRaisesRegex(
            web_session_provider_authority.WebSessionProviderAuthorityError,
            "web_session_deleted",
        ):
            authority.provider_for_session("api-only")

    def test_delete_legacy_makes_legacy_unavailable_and_keeps_or_creates_api(self):
        insert_message(self.db, text="old main")
        legacy = FakeLegacy({"sessions": [], "active_session": ""})
        authority = web_session_provider_authority.WebSessionProviderAuthority(legacy)
        result = web_session_delete.delete_conversation(
            authority,
            web_session_delete.LEGACY_SESSION_ID,
            relay_db=self.db,
            upload_dir=self.uploads,
            codex_store=self.store,
        )
        self.assertEqual(result["deleted"]["scope"], "legacy")
        self.assertFalse(result["legacy_available"])
        self.assertFalse(result["legacy_delete_allowed"])
        self.assertEqual(len(result["sessions"]), 1)
        self.assertEqual(result["sessions"][0]["provider"], "api")

    def test_active_codex_requires_retirement(self):
        insert_message(self.db, text="codex", api_session="api-codex")
        codex_generation_store.initialize(self.store)
        codex_generation_store.pin_session(
            self.store,
            api_session="api-codex",
            model="gpt-test",
            model_provider="openai",
            reasoning_effort="low",
            persona_hash="a" * 64,
        )
        legacy = FakeLegacy({"sessions": [session_row("api-codex", "codex")]})
        authority = web_session_provider_authority.WebSessionProviderAuthority(legacy)
        with self.assertRaisesRegex(
            web_session_provider_authority.WebSessionProviderAuthorityError,
            web_session_delete.DELETE_REQUIRES_RETIREMENT,
        ):
            web_session_delete.delete_conversation(
                authority,
                "api-codex",
                relay_db=self.db,
                upload_dir=self.uploads,
                codex_store=self.store,
            )
        self.assertEqual(len(legacy.cfg["sessions"]), 1)

    def test_retired_codex_is_deletable_and_tombstone_preserves_provider_identity(self):
        insert_message(self.db, text="codex gone", api_session="api-codex")
        codex_generation_store.initialize(self.store)
        codex_generation_store.pin_session(
            self.store,
            api_session="api-codex",
            model="gpt-test",
            model_provider="openai",
            reasoning_effort="low",
            persona_hash="a" * 64,
        )
        codex_generation_store.retire_session(self.store, api_session="api-codex")
        legacy = FakeLegacy({
            "sessions": [session_row("api-normal"), session_row("api-codex", "codex")],
            "active_session": "api-codex",
        })
        authority = web_session_provider_authority.WebSessionProviderAuthority(legacy)
        result = web_session_delete.delete_conversation(
            authority,
            "api-codex",
            relay_db=self.db,
            upload_dir=self.uploads,
            codex_store=self.store,
        )
        self.assertEqual(result["deleted"]["provider"], "codex")
        self.assertEqual([row["id"] for row in result["sessions"]], ["api-normal"])
        tombstone = authority.tombstone_for_session("api-codex")
        self.assertEqual(tombstone["provider"], "codex")
        self.assertIsNotNone(codex_generation_store.get_session(self.store, "api-codex"))

    def test_retired_codex_with_nonterminal_job_is_not_deletable(self):
        codex_generation_store.initialize(self.store)
        codex_generation_store.pin_session(
            self.store,
            api_session="api-codex",
            model="gpt-test",
            model_provider="openai",
            reasoning_effort="low",
            persona_hash="a" * 64,
        )
        codex_generation_store.enqueue_job(
            self.store,
            api_session="api-codex",
            canonical_message_id=1,
            input_digest="b" * 64,
            generation_id="gen-1",
            client_message_id="client-1",
            callback_identity="callback-1",
        )
        with closing(codex_generation_store.connect(self.store)) as conn:
            conn.execute(
                "UPDATE codex_sessions SET status='retired',retired_at='2026-08-30T00:00:00+00:00' "
                "WHERE api_session='api-codex'"
            )
        with self.assertRaisesRegex(
            web_session_provider_authority.WebSessionProviderAuthorityError,
            web_session_delete.DELETE_JOB_ACTIVE,
        ):
            web_session_delete.assert_codex_deletable(self.store, "api-codex")


class P3SessionDeleteRelayTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        load_app(self.temp.name, telegram=False)
        os.environ.update({
            "LEGACY_CHAT_BRIDGE_TOKEN": "test-legacy-bridge-token-1234567890",
            "LEGACY_CHAT_BRIDGE_SESSION": "legacy-test",
            "CODEX_CONTROL_ENABLED": "false",
            "CODEX_CANARY_ENTRYPOINTS_ENABLED": "false",
            "CODEX_GENERATION_ENABLED": "false",
        })
        package = sys.modules.get("backend")
        for name in ("backend.p3_relay_app", "backend.legacy_chat_bridge_app"):
            sys.modules.pop(name, None)
            if package is not None:
                attr = name.rsplit(".", 1)[-1]
                if hasattr(package, attr):
                    delattr(package, attr)
        self.module = importlib.import_module("backend.p3_relay_app")
        self.addCleanup(sys.modules.pop, "backend.p3_relay_app", None)
        self.addCleanup(sys.modules.pop, "backend.legacy_chat_bridge_app", None)

    async def test_delete_route_requires_existing_relay_auth(self):
        response = await request(self.module, "DELETE", "/app/sessions/api-test")
        self.assertEqual(response.status_code, 401)

    async def test_delete_route_proxies_exact_session_and_method(self):
        with mock.patch.object(
            self.module.relay_app,
            "loop_json",
            return_value={
                "active_session": "api-next",
                "sessions": [],
                "legacy_available": False,
                "legacy_delete_allowed": False,
                "deleted": {
                    "id": "api-test",
                    "scope": "session",
                    "provider": "api",
                    "content_deleted": True,
                    "messages_purged": 2,
                },
            },
        ) as proxied:
            response = await request(
                self.module,
                "DELETE",
                "/app/sessions/api-test",
                headers={"Authorization": "Bearer test-relay-secret"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["deleted"]["content_deleted"])
        proxied.assert_called_once_with(
            "/loop/sessions/api-test",
            method="DELETE",
        )


class ProviderGuardDeleteContractTests(unittest.TestCase):
    def test_guard_exposes_hard_delete_and_deleted_session_status(self):
        source = Path("examples/api_loop_provider_guard.py").read_text(encoding="utf-8")
        self.assertIn('@app.delete("/loop/sessions/{session_id}")', source)
        self.assertIn("web_session_delete.delete_conversation", source)
        self.assertIn('category == "web_session_deleted"', source)
        self.assertIn("DELETE_REQUIRES_RETIREMENT", source)


if __name__ == "__main__":
    unittest.main()
