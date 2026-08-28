from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import codex_canary_relay_integration as integration


class LoopDispatchError(RuntimeError):
    def __init__(self, category, uncertain):
        super().__init__(category)
        self.category = category
        self.uncertain = uncertain


class FakeResponse:
    def __init__(self, payload):
        self.raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, maximum):
        return self.raw[:maximum]


class RelayIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "relay.db"
        with sqlite3.connect(self.path) as conn:
            conn.execute("""CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                direction TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                meta TEXT NOT NULL DEFAULT '{}')""")
            conn.execute(
                "INSERT INTO messages(id,ts,direction,kind,text,meta) VALUES(?,?,?,?,?,?)",
                (
                    41,
                    "2026-08-27T12:00:00+00:00",
                    "in",
                    "user",
                    "hello",
                    json.dumps({
                        "channel": "web",
                        "source": "relay",
                        "api_session": "api-canary",
                        "attachments": [],
                    }),
                ),
            )
        self.original_completion_calls = []
        self.original_forward_calls = []

        def original_completion(msg):
            self.original_completion_calls.append(msg)
            return {"legacy": True}

        def original_forward(msg, routing=None):
            self.original_forward_calls.append((msg, routing))
            return {"legacy": True, "routing": routing}

        self.relay = SimpleNamespace(
            DB_PATH=str(self.path),
            LOOP_INGEST_URL="http://127.0.0.1:3020/loop/ingest",
            API_LOOP_INTERNAL_TOKEN="x" * 48,
            LOOP_DISPATCH_TIMEOUT_SECONDS=180,
            DEPLOYMENT=SimpleNamespace(
                sqlite_busy_timeout_seconds=30,
                kelivo=SimpleNamespace(internal_response_max_bytes=1024 * 1024),
            ),
            LoopDispatchError=LoopDispatchError,
            now_iso=lambda: "2026-08-27T12:01:00+00:00",
            telegram_completion_for=original_completion,
            _forward_to_loop_sync=original_forward,
        )
        integration.install(self.relay)

    def web_msg(self):
        return {
            "id": 41,
            "text": "hello",
            "meta": {"api_session": "api-canary", "channel": "web", "source": "relay"},
        }

    def test_install_is_idempotent(self):
        first_completion = self.relay.telegram_completion_for
        first_forward = self.relay._forward_to_loop_sync
        integration.install(self.relay)
        self.assertIs(self.relay.telegram_completion_for, first_completion)
        self.assertIs(self.relay._forward_to_loop_sync, first_forward)

    def test_telegram_routing_delegates_to_original_strict_path(self):
        msg = self.web_msg()
        routing = {"channel": "telegram", "generation_id": "tg-gen-1"}
        result = self.relay._forward_to_loop_sync(msg, routing)
        self.assertEqual(result, {"legacy": True, "routing": routing})
        self.assertEqual(self.original_forward_calls, [(msg, routing)])

    def test_old_synchronous_api_web_ack_is_still_accepted(self):
        payload = {
            "ok": True,
            "callback_delivered": True,
            "generation_id": "api-gen-1",
            "stream_id": "api-stream-1",
            "api_session": "api-canary",
        }
        with patch.object(integration.urllib.request, "urlopen", return_value=FakeResponse(payload)):
            result = self.relay._forward_to_loop_sync(self.web_msg())
        self.assertEqual(result, payload)

    def test_new_codex_queued_ack_is_accepted_without_callback_delivered_lie(self):
        payload = {
            "ok": True,
            "queued": True,
            "provider": "codex",
            "generation_provider": "codex",
            "generation_id": "codex-gen-41",
            "api_session": "api-canary",
            "canonical_message_id": 41,
            "status": "queued",
        }
        with patch.object(integration.urllib.request, "urlopen", return_value=FakeResponse(payload)):
            result = self.relay._forward_to_loop_sync(self.web_msg())
        self.assertEqual(result, payload)
        self.assertNotIn("callback_delivered", result)

    def test_bad_codex_queued_correlation_is_explicit_failure_not_uncertain(self):
        payload = {
            "ok": True,
            "queued": True,
            "provider": "codex",
            "generation_provider": "codex",
            "generation_id": "codex-gen-41",
            "api_session": "other-session",
            "canonical_message_id": 41,
            "status": "queued",
        }
        with patch.object(integration.urllib.request, "urlopen", return_value=FakeResponse(payload)):
            with self.assertRaises(LoopDispatchError) as raised:
                self.relay._forward_to_loop_sync(self.web_msg())
        self.assertEqual(raised.exception.category, "loop_queued_ack_correlation_mismatch")
        self.assertFalse(raised.exception.uncertain)

    def test_codex_web_completion_uses_exactly_once_primitive(self):
        msg = {
            "kind": "reply",
            "text": "answer",
            "meta": {
                "channel": "web",
                "source": "codex_generation",
                "provider": "codex",
                "api_session": "api-canary",
                "reply_to": "41",
                "generation_id": "codex-gen-41",
                "client_message_id": "codex-client-41",
                "codex_callback_identity": "codex-callback-41",
            },
        }
        first = self.relay.telegram_completion_for(msg)
        second = self.relay.telegram_completion_for(msg)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["message"]["id"], second["message"]["id"])
        self.assertEqual(self.original_completion_calls, [])

    def test_non_codex_completion_preserves_original_behavior(self):
        msg = {"kind": "reply", "text": "legacy", "meta": {"channel": "telegram"}}
        result = self.relay.telegram_completion_for(msg)
        self.assertEqual(result, {"legacy": True})
        self.assertEqual(self.original_completion_calls, [msg])

    def test_malformed_codex_marker_fails_closed_instead_of_falling_to_generic_reply(self):
        msg = {"kind": "reply", "text": "answer", "meta": {"provider": "codex"}}
        with self.assertRaisesRegex(
            integration.CodexCanaryRelayIntegrationError,
            "codex_web_completion_invalid",
        ):
            self.relay.telegram_completion_for(msg)
        self.assertEqual(self.original_completion_calls, [])


if __name__ == "__main__":
    unittest.main()
