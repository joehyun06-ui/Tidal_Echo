from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.codex_canary_controller import CodexCanaryControllerError
from backend.codex_canary_loop_integration import (
    CodexCanaryLoopIntegration,
    CodexCanaryLoopIntegrationError,
    LegacyControlAdapter,
    build_completion_callback,
)
from backend.codex_app_server_control import CodexControlError
from backend.codex_account_control_facade import CodexAccountFacadeError


class FakeController:
    def __init__(self):
        self.pinned = set()
        self.calls = []
        self.fail_pin = None
        self.fail_admit = None

    def is_pinned(self, session):
        return session in self.pinned

    async def pin_session(self, session):
        self.calls.append(("pin", session))
        if self.fail_pin:
            raise self.fail_pin
        self.pinned.add(session)
        return {"api_session": session, "status": "active"}

    def admit_if_pinned(self, **kwargs):
        self.calls.append(("admit", kwargs))
        if self.fail_admit:
            raise self.fail_admit
        if kwargs["api_session"] not in self.pinned:
            return None
        mid = kwargs["canonical_message_id"]
        return {
            "generation_id": f"codex-gen-{mid}",
            "client_message_id": f"codex-client-{mid}",
            "callback_identity": f"codex-callback-{mid}",
            "api_session": kwargs["api_session"],
            "canonical_message_id": mid,
            "status": "queued",
        }

    def retire_session(self, session):
        self.calls.append(("retire", session))
        self.pinned.discard(session)
        return {"api_session": session, "status": "retired"}


class FakeControl:
    def __init__(self):
        self.error = None

    async def status(self):
        if self.error:
            raise self.error
        return {"connected": True}

    async def usage(self): return {"usage": True}
    async def login_start(self): return {"status": "pending"}
    async def login_cancel(self): return {"cancelled": True}
    async def logout(self): return {"logged_out": True}


class FakeLegacy:
    def __init__(self):
        self.CODEX_CONTROL = object()
        self.TRANSIENT_CONTINUITY_ENABLED = False
        self.RELAY_DB = "/tmp/relay.db"
        self.legacy_ingest_calls = []
        self.logs = []
        self.rows = []
        self.save_events = []
        self._CODEX_CANARY_SESSION_LOCK_INSTALLED = False
        self.continuity_context = SimpleNamespace(
            derive_continuity_context=lambda *_args: None,
        )

    def active_session_id(self): return "api-normal"

    async def handle_ingest(self, text, before_id, session_id, **kwargs):
        self.legacy_ingest_calls.append((text, before_id, session_id, kwargs))
        return {
            "ok": True,
            "callback_delivered": True,
            "generation_id": "api-gen",
            "stream_id": "api-stream",
            "api_session": session_id,
        }

    def _log_continuity_context(self, status, **kwargs):
        self.logs.append((status, kwargs))

    def session_rows(self): return [dict(row) for row in self.rows]

    def save_sessions(self, rows, active=None):
        self.save_events.append(("save", [dict(row) for row in rows], active))
        self.rows = [dict(row) for row in rows]
        return {"sessions": self.rows}

    def create_session(self, *args, **kwargs):
        self.save_events.append(("legacy-create", args, kwargs))
        return {"id": "legacy"}

    def patch_session(self, *args, **kwargs):
        self.save_events.append(("legacy-patch", args, kwargs))
        return {"ok": True}

    def now_iso(self): return "2026-08-27T14:00:00+00:00"


class CodexCanaryLoopIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.legacy = FakeLegacy()
        self.controller = FakeController()
        self.runtime = SimpleNamespace(
            generation_enabled=True,
            controller=self.controller,
            control=FakeControl(),
        )
        self.integration = CodexCanaryLoopIntegration(self.legacy, self.runtime)

    async def test_generation_gate_off_uses_legacy_api_path(self):
        self.runtime.generation_enabled = False
        result = await self.integration.handle_ingest({
            "id": 41, "text": "hello", "session_id": "api-normal"
        })
        self.assertTrue(result["callback_delivered"])
        self.assertEqual(len(self.legacy.legacy_ingest_calls), 1)
        self.assertEqual(self.controller.calls, [])

    async def test_unpinned_session_uses_legacy_api_path(self):
        result = await self.integration.handle_ingest({
            "id": 41, "text": "hello", "session_id": "api-normal"
        })
        self.assertTrue(result["callback_delivered"])
        self.assertEqual(len(self.legacy.legacy_ingest_calls), 1)

    async def test_pinned_session_returns_fast_codex_queued_ack_without_legacy_generation(self):
        self.controller.pinned.add("api-canary")
        result = await self.integration.handle_ingest({
            "id": 41, "text": "hello", "session_id": "api-canary"
        })
        self.assertEqual(result, {
            "ok": True,
            "queued": True,
            "provider": "codex",
            "generation_provider": "codex",
            "generation_id": "codex-gen-41",
            "api_session": "api-canary",
            "canonical_message_id": 41,
            "status": "queued",
        })
        self.assertEqual(self.legacy.legacy_ingest_calls, [])

    async def test_pinned_dry_request_fails_closed(self):
        self.controller.pinned.add("api-canary")
        with self.assertRaisesRegex(CodexCanaryLoopIntegrationError, "dry_unsupported"):
            await self.integration.handle_ingest({
                "id": 41, "text": "hello", "session_id": "api-canary", "dry": True
            })
        self.assertEqual(self.legacy.legacy_ingest_calls, [])

    async def test_pinned_continuity_applied_fails_closed_not_api_fallback(self):
        self.controller.pinned.add("api-canary")
        self.legacy.TRANSIENT_CONTINUITY_ENABLED = True
        self.legacy.continuity_context = SimpleNamespace(
            derive_continuity_context=lambda *_args: SimpleNamespace(
                developer_message={"role": "developer", "content": "handoff"},
                current_channel="web",
                items=[1],
                total_chars=12,
            )
        )
        self.controller.fail_admit = CodexCanaryControllerError(
            "codex_canary_continuity_unsupported"
        )
        with self.assertRaisesRegex(
            CodexCanaryLoopIntegrationError, "continuity_unsupported"
        ):
            await self.integration.handle_ingest({
                "id": 41, "text": "hello", "session_id": "api-canary"
            })
        self.assertEqual(self.legacy.legacy_ingest_calls, [])
        self.assertEqual(self.legacy.logs[0][0], "applied")

    async def test_continuity_derivation_failure_is_fail_closed_for_pin(self):
        self.controller.pinned.add("api-canary")
        self.legacy.TRANSIENT_CONTINUITY_ENABLED = True
        def fail(*_args): raise RuntimeError("unavailable")
        self.legacy.continuity_context = SimpleNamespace(derive_continuity_context=fail)
        self.controller.fail_admit = CodexCanaryControllerError(
            "codex_canary_continuity_unavailable"
        )
        with self.assertRaisesRegex(
            CodexCanaryLoopIntegrationError, "continuity_unavailable"
        ):
            await self.integration.handle_ingest({
                "id": 41, "text": "hello", "session_id": "api-canary"
            })
        self.assertEqual(self.legacy.legacy_ingest_calls, [])

    async def test_create_canary_pins_before_publishing_and_does_not_activate(self):
        row = await self.integration.create_canary_session(title="Canary")
        self.assertIn(row["id"], self.controller.pinned)
        self.assertEqual(self.controller.calls[0], ("pin", row["id"]))
        self.assertEqual(self.legacy.save_events[0][0], "save")
        self.assertIsNone(self.legacy.save_events[0][2])
        self.assertEqual(self.legacy.rows[-1]["title"], "Canary")

    async def test_publish_failure_compensates_by_retiring_new_pin(self):
        def fail_save(*_args, **_kwargs):
            raise RuntimeError("disk error")
        self.integration._original_save_sessions = fail_save
        with self.assertRaisesRegex(RuntimeError, "disk error"):
            await self.integration.create_canary_session()
        self.assertEqual(self.controller.calls[-1][0], "retire")
        self.assertEqual(self.controller.pinned, set())

    async def test_retire_returns_session_to_api_authority(self):
        self.controller.pinned.add("api-canary")
        row = self.integration.retire_canary_session("api-canary")
        self.assertEqual(row["status"], "retired")
        self.assertNotIn("api-canary", self.controller.pinned)

    async def test_install_replaces_legacy_control_with_error_compatible_adapter(self):
        self.integration.install_legacy_globals()
        self.assertIsInstance(self.legacy.CODEX_CONTROL, LegacyControlAdapter)
        self.runtime.control.error = CodexAccountFacadeError("codex_control_disabled")
        with self.assertRaisesRegex(CodexControlError, "codex_control_disabled"):
            await self.legacy.CODEX_CONTROL.status()


class CompletionCallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_callback_uses_stable_codex_correlation(self):
        seen = []
        class Legacy:
            async def relay_out(self, payload):
                seen.append(payload)
                return True, {"id": 77}, False
        callback = build_completion_callback(Legacy())
        result = await callback(
            {
                "api_session": "api-canary",
                "canonical_message_id": 41,
                "generation_id": "codex-gen-41",
                "client_message_id": "codex-client-41",
                "callback_identity": "codex-callback-41",
            },
            "answer",
            {"input_tokens": 4},
        )
        self.assertEqual(result, 77)
        self.assertEqual(seen[0]["reply_to"], "41")
        self.assertEqual(seen[0]["codex_callback_identity"], "codex-callback-41")
        self.assertEqual(seen[0]["provider"], "codex")
        self.assertEqual(seen[0]["channel"], "web")

    async def test_uncertain_callback_raises_without_fabricating_message_id(self):
        class Legacy:
            async def relay_out(self, _payload):
                return False, {"error": "callback_uncertain"}, True
        callback = build_completion_callback(Legacy())
        with self.assertRaisesRegex(CodexCanaryLoopIntegrationError, "callback_uncertain"):
            await callback(
                {
                    "api_session": "api-canary",
                    "canonical_message_id": 41,
                    "generation_id": "codex-gen-41",
                    "client_message_id": "codex-client-41",
                    "callback_identity": "codex-callback-41",
                },
                "answer",
                None,
            )


if __name__ == "__main__":
    unittest.main()
