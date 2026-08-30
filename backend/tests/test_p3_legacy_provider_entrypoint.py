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
from backend.tests._support import NoNetworkMixin, request


class LegacyProviderGuardTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "relay.sqlite3"
        self.config_path = self.root / "loop.json"
        self.store_path = self.root / "codex-generation.db"
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("""CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, direction TEXT,
                kind TEXT, text TEXT, meta TEXT)""")
            conn.commit()
        self.token = "test-internal-loop-token-1234567890"
        self.headers = {"X-API-Loop-Internal-Token": self.token}
        os.environ.update({
            "RELAY_DB": str(self.db_path),
            "LOOP_CONFIG": str(self.config_path),
            "RELAY_SECRET": "invalid-test-relay-secret",
            "RELAY_URL": "http://invalid.test",
            "LLM_API_BASE": "https://model.invalid/v1",
            "LLM_API_KEY": "invalid-key",
            "LLM_MODEL": "model-one",
            "LOOP_STREAM": "0",
            "LOOP_MODEL_TOTAL_TIMEOUT_SECONDS": "1",
            "LOOP_CALLBACK_TIMEOUT_SECONDS": "1",
            "LOOP_TIMEOUT_SAFETY_MARGIN_SECONDS": "1",
            "LOOP_DISPATCH_TIMEOUT_SECONDS": "3",
            "API_LOOP_INTERNAL_TOKEN": self.token,
            "CODEX_CONTROL_ENABLED": "false",
            "CODEX_GENERATION_ENABLED": "false",
            "CODEX_GENERATION_DB": str(self.store_path),
            "RENDER_TELEGRAM_MVP": "false",
        })
        for name in ("examples.api_loop_provider_guard", "examples.api_loop"):
            sys.modules.pop(name, None)
        self.module = importlib.import_module("examples.api_loop_provider_guard")
        self.legacy = self.module.legacy

    def write_sessions(self, rows, *, active=""):
        payload = {"sessions": rows}
        if active:
            payload["active_session"] = active
        self.config_path.write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

    def pin_codex(self, session_id: str, *, retire: bool = False):
        codex_generation_store.initialize(self.store_path)
        codex_generation_store.pin_session(
            self.store_path,
            api_session=session_id,
            model="gpt-test",
            model_provider="openai",
            reasoning_effort="low",
            persona_hash="a" * 64,
        )
        if retire:
            codex_generation_store.retire_session(
                self.store_path,
                api_session=session_id,
            )

    async def test_providerless_pre_p3_row_projects_api_without_ui_inference(self):
        sid = "api-old-1"
        self.write_sessions([{
            "id": sid,
            "title": "Codex canary",
            "since_id": 0,
            "created_at": "2026-01-01T00:00:00+00:00",
            "pinned": True,
        }], active=sid)
        response = await request(
            self.module,
            "GET",
            "/loop/sessions",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sessions"][0]["provider"], "api")
        self.assertFalse(self.store_path.exists())

    async def test_explicit_codex_row_fails_closed_before_legacy_generation(self):
        sid = "api-codex-1"
        self.write_sessions([{
            "id": sid,
            "title": "anything",
            "since_id": 0,
            "created_at": "2026-01-01T00:00:00+00:00",
            "provider": "codex",
        }], active=sid)
        generated = mock.AsyncMock(side_effect=AssertionError("legacy generation must not run"))
        with mock.patch.object(self.legacy, "handle_ingest", new=generated):
            response = await request(
                self.module,
                "POST",
                "/loop/ingest",
                headers=self.headers,
                json={"text": "do not cross providers", "api_session": sid},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "codex_generation_disabled")
        generated.assert_not_awaited()
        self.assertFalse(self.store_path.exists())

    async def test_retired_pre_p3_codex_history_stays_codex_and_fails_closed(self):
        sid = "api-historical-codex"
        self.write_sessions([{
            "id": sid,
            "title": "old window",
            "since_id": 0,
            "created_at": "2026-01-01T00:00:00+00:00",
        }], active=sid)
        self.pin_codex(sid, retire=True)
        sessions = await request(
            self.module,
            "GET",
            "/loop/sessions",
            headers=self.headers,
        )
        self.assertEqual(sessions.status_code, 200)
        self.assertEqual(sessions.json()["sessions"][0]["provider"], "codex")
        generated = mock.AsyncMock(side_effect=AssertionError("legacy generation must not run"))
        with mock.patch.object(self.legacy, "handle_ingest", new=generated):
            response = await request(
                self.module,
                "POST",
                "/loop/ingest",
                headers=self.headers,
                json={"text": "still codex", "api_session": sid},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "codex_generation_disabled")
        generated.assert_not_awaited()

    async def test_explicit_api_conflicting_with_codex_history_fails_closed(self):
        sid = "api-conflict"
        self.write_sessions([{
            "id": sid,
            "title": "conflict",
            "since_id": 0,
            "created_at": "2026-01-01T00:00:00+00:00",
            "provider": "api",
        }], active=sid)
        self.pin_codex(sid)
        response = await request(
            self.module,
            "GET",
            "/loop/sessions",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"],
            "web_session_provider_authority_unavailable",
        )

    async def test_unknown_non_web_session_delegates_without_validating_other_web_row(self):
        self.write_sessions([{
            "id": "api-codex-other",
            "title": "other",
            "since_id": 0,
            "created_at": "2026-01-01T00:00:00+00:00",
            "provider": "codex",
        }])
        generated = mock.AsyncMock(return_value={
            "ok": True,
            "callback_delivered": True,
            "dispatch_uncertain": False,
            "api_session": "telegram-1",
        })
        with mock.patch.object(self.legacy, "handle_ingest", new=generated):
            response = await request(
                self.module,
                "POST",
                "/loop/ingest",
                headers=self.headers,
                json={"text": "ordinary routed traffic", "api_session": "telegram-1"},
            )
        self.assertEqual(response.status_code, 200)
        generated.assert_awaited_once()
        self.assertEqual(generated.await_args.args[:3], (
            "ordinary routed traffic", None, "telegram-1"
        ))

    async def test_session_creation_persists_explicit_api_authority(self):
        response = await request(
            self.module,
            "POST",
            "/loop/sessions",
            headers=self.headers,
            json={"title": "ordinary", "activate": True},
        )
        self.assertEqual(response.status_code, 200)
        created = response.json()["created"]
        self.assertEqual(created["provider"], "api")
        persisted = json.loads(self.config_path.read_text(encoding="utf-8"))
        row = next(item for item in persisted["sessions"] if item["id"] == created["id"])
        self.assertEqual(row["provider"], "api")
        self.assertEqual(persisted["active_session"], created["id"])
        self.assertFalse(self.store_path.exists())

    async def test_codex_creation_is_unavailable_and_publishes_nothing(self):
        before = self.config_path.read_bytes() if self.config_path.exists() else None
        response = await request(
            self.module,
            "POST",
            "/loop/sessions",
            headers=self.headers,
            json={"title": "codex", "provider": "codex"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "codex_generation_disabled")
        after = self.config_path.read_bytes() if self.config_path.exists() else None
        self.assertEqual(after, before)
        self.assertFalse(self.store_path.exists())

    async def test_provider_is_immutable_through_legacy_guard(self):
        created = await request(
            self.module,
            "POST",
            "/loop/sessions",
            headers=self.headers,
            json={"title": "ordinary"},
        )
        sid = created.json()["created"]["id"]
        response = await request(
            self.module,
            "PATCH",
            f"/loop/sessions/{sid}",
            headers=self.headers,
            json={"provider": "codex"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "web_session_provider_immutable")
        persisted = json.loads(self.config_path.read_text(encoding="utf-8"))
        row = next(item for item in persisted["sessions"] if item["id"] == sid)
        self.assertEqual(row["provider"], "api")


if __name__ == "__main__":
    unittest.main()
