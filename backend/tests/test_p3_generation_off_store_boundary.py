from __future__ import annotations

import unittest
from types import SimpleNamespace

from backend.codex_canary_loop_integration import (
    CodexCanaryLoopIntegration,
    CodexCanaryLoopIntegrationError,
)


class NoPinLookupController:
    def historical_provider(self, _session_id: str):
        return None

    def is_pinned(self, _session_id: str):
        raise AssertionError("generation-off path touched Codex pin store")


class FakeLegacy:
    TRANSIENT_CONTINUITY_ENABLED = False
    RELAY_DB = "/tmp/relay.db"
    CODEX_CONTROL = object()
    _CODEX_CANARY_SESSION_LOCK_INSTALLED = False

    def __init__(self, provider: str):
        self.cfg = {
            "sessions": [{
                "id": "api-target",
                "title": "target",
                "since_id": 0,
                "created_at": "2026-08-29T00:00:00+00:00",
                "provider": provider,
            }],
            "active_session": "api-target",
        }
        self.legacy_calls = []
        self.create_session = lambda *args, **kwargs: None
        self.patch_session = lambda *args, **kwargs: None
        self.save_sessions = lambda *args, **kwargs: None

    def load_config(self):
        return {
            **self.cfg,
            "sessions": [dict(row) for row in self.cfg["sessions"]],
        }

    def save_config(self, cfg):
        self.cfg = {
            **cfg,
            "sessions": [dict(row) for row in cfg.get("sessions", [])],
        }

    def session_rows(self):
        return [dict(row) for row in self.cfg["sessions"]]

    def active_session_id(self):
        return "api-target"

    def sessions_public(self):
        return {
            "active_session": "api-target",
            "sessions": self.session_rows(),
        }

    def now_iso(self):
        return "2026-08-29T00:00:00+00:00"

    async def handle_ingest(self, text, before_id, session_id, **kwargs):
        self.legacy_calls.append((text, before_id, session_id, kwargs))
        return {
            "ok": True,
            "callback_delivered": True,
            "generation_id": "api-gen",
            "stream_id": "api-stream",
            "api_session": session_id,
        }


class P3GenerationOffStoreBoundaryTest(unittest.IsolatedAsyncioTestCase):
    def integration(self, provider: str):
        legacy = FakeLegacy(provider)
        runtime = SimpleNamespace(
            generation_enabled=False,
            controller=NoPinLookupController(),
        )
        return legacy, CodexCanaryLoopIntegration(legacy, runtime)

    async def test_api_authority_does_not_query_codex_pin_when_generation_off(self):
        legacy, integration = self.integration("api")
        result = await integration.handle_ingest({
            "id": 41,
            "text": "hello",
            "session_id": "api-target",
        })
        self.assertTrue(result["callback_delivered"])
        self.assertEqual(len(legacy.legacy_calls), 1)

    async def test_codex_authority_freezes_before_pin_lookup_when_generation_off(self):
        legacy, integration = self.integration("codex")
        with self.assertRaises(CodexCanaryLoopIntegrationError) as raised:
            await integration.handle_ingest({
                "id": 41,
                "text": "hello",
                "session_id": "api-target",
            })
        self.assertEqual(raised.exception.category, "codex_generation_disabled")
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(legacy.legacy_calls, [])


if __name__ == "__main__":
    unittest.main()
