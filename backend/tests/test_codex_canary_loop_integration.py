from __future__ import annotations

import unittest
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
        self.cfg = {"sessions": []}
        self.save_events = []
        self._CODEX_CANARY_SESSION_LOCK_INSTALLED = False
        self.continuity_context = SimpleNamespace(
            derive_continuity_context=lambda *_args: None,
        )

    def load_config(self):
        cfg = dict(self.cfg)
        cfg["sessions"] = [dict(row) for row in self.cfg.get("sessions", [])]
        return cfg

    def save_config(self, cfg):
        self.cfg = dict(cfg)
        self.cfg["sessions"] = [dict(row) for row in cfg.get("sessions", [])]
        self.rows = [dict(row) for row in self.cfg["sessions"]]
        self.save_events.append(("save", self.load_config()))

    def active_session_id(self):
        active = str(self.cfg.get("active_session") or "")
        ids = {row.get("id") for row in self.cfg.get("sessions", [])}
        if active in ids:
            return active
        rows = self.cfg.get("sessions", [])
        return str(rows[-1].get("id") or "") if rows else "api-normal"

    def sessions_public(self):
        return {"active_session": self.active_session_id(), "sessions": self.session_rows()}

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

    def session_rows(self): return [dict(row) for row in self.cfg.get("sessions", [])]

    def save_sessions(self, rows, active=None):
        cfg = self.load_config()
        cfg["sessions"] = [dict(row) for row in rows]
        if active is not None:
            cfg["active_session"] = active
        self.save_config(cfg)
        return self.sessions_public()

    def create_session(self, *args, **kwargs):
        self.save_events.append(("legacy-create", args, kwargs))
        return {"id": "legacy"}

    def patch_session(self, *args, **kwargs):
        self.save_events.append(("legacy-patch", args, kwargs))
        return {"ok": True}

    def now_iso(self): return "2026-08-27T14:00:00+00:00"

    def add_session(self, session_id, provider=None):
        row = {
            "id": session_id,
            "title": session_id,
            "since_id": 0,
            "created_at": self.now_iso(),
        }
        if provider is not None:
            row["provider"] = provider
        self.cfg.setdefault("sessions", []).append(row)
        self.rows = [dict(item) for item in self.cfg["sessions"]]


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

    async def test_generation_gate_off_api_authority_uses_legacy_api_path(self):
        self.runtime.generation_enabled = False
        result = await self.integration.handle_ingest({
            "id": 41, "text": "hello", "session_id": "api-normal"
        })
        self.assertTrue(result["callback_delivered"])
        self.assertEqual(len(self.legacy.legacy_ingest_calls), 1)
        self.assertEqual(self.controller.calls, [])

    async def test_pre_p3_session_without_provider_defaults_to_api(self):
        self.legacy.add_session("api-old")
        result = await self.integration.handle_ingest({
            "id": 41, "text": "hello", "session_id": "api-old"
        })
        self.assertTrue(result["callback_delivered"])
        self.assertEqual(self.integration.sessions_public()["sessions"][0]["provider"], "api")

    async def test_explicit_api_session_uses_legacy_api_path(self):
        self.legacy.add_session("api-normal", "api")
        result = await self.integration.handle_ingest({
            "id": 41, "text": "hello", "session_id": "api-normal"
        })
        self.assertTrue(result["callback_delivered"])
        self.assertEqual(len(self.legacy.legacy_ingest_calls), 1)

    async def test_codex_authority_returns_fast_queued_ack(self):
        self.legacy.add_session("api-canary", "codex")
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

    async def test_api_authority_with_active_codex_pin_fails_closed(self):
        self.legacy.add_session("api-mismatch", "api")
        self.controller.pinned.add("api-mismatch")
        with self.assertRaisesRegex(
            CodexCanaryLoopIntegrationError, "provider_authority_mismatch"
        ):
            await self.integration.handle_ingest({
                "id": 41, "text": "hello", "session_id": "api-mismatch"
            })
        self.assertEqual(self.legacy.legacy_ingest_calls, [])

    async def test_codex_authority_without_active_pin_fails_closed(self):
        self.legacy.add_session("api-mismatch", "codex")
        with self.assertRaisesRegex(
            CodexCanaryLoopIntegrationError, "provider_authority_mismatch"
        ):
            await self.integration.handle_ingest({
                "id": 41, "text": "hello", "session_id": "api-mismatch"
            })
        self.assertEqual(self.legacy.legacy_ingest_calls, [])

    async def test_codex_authority_generation_freeze_fails_closed(self):
        self.legacy.add_session("api-canary", "codex")
        self.controller.pinned.add("api-canary")
        self.runtime.generation_enabled = False
        with self.assertRaises(CodexCanaryLoopIntegrationError) as raised:
            await self.integration.handle_ingest({
                "id": 41, "text": "hello", "session_id": "api-canary"
            })
        self.assertEqual(raised.exception.category, "codex_generation_disabled")
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(self.legacy.legacy_ingest_calls, [])

    async def test_codex_dry_request_fails_closed(self):
        self.legacy.add_session("api-canary", "codex")
        self.controller.pinned.add("api-canary")
        with self.assertRaisesRegex(CodexCanaryLoopIntegrationError, "dry_unsupported"):
            await self.integration.handle_ingest({
                "id": 41, "text": "hello", "session_id": "api-canary", "dry": True
            })
        self.assertEqual(self.legacy.legacy_ingest_calls, [])

    async def test_codex_continuity_applied_fails_closed_not_api_fallback(self):
        self.legacy.add_session("api-canary", "codex")
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

    async def test_continuity_derivation_failure_is_fail_closed_for_codex_authority(self):
        self.legacy.add_session("api-canary", "codex")
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

    async def test_create_api_session_persists_explicit_api_authority(self):
        row = await self.integration.create_web_session(
            provider="api", title="API chat", since_id=12, activate=True
        )
        self.assertEqual(row["provider"], "api")
        self.assertEqual(row["since_id"], 12)
        self.assertEqual(self.legacy.cfg["active_session"], row["id"])
        self.assertEqual(self.controller.calls, [])

    async def test_create_codex_session_pins_before_publishing(self):
        row = await self.integration.create_web_session(
            provider="codex", title="Codex chat", since_id=8, activate=True
        )
        self.assertIn(row["id"], self.controller.pinned)
        self.assertEqual(self.controller.calls[0], ("pin", row["id"]))
        self.assertEqual(row["provider"], "codex")
        self.assertEqual(self.legacy.rows[-1]["provider"], "codex")
        self.assertEqual(self.legacy.cfg["active_session"], row["id"])

    async def test_create_canary_pins_before_publishing_and_does_not_activate(self):
        row = await self.integration.create_canary_session(title="Canary")
        self.assertIn(row["id"], self.controller.pinned)
        self.assertEqual(self.controller.calls[0], ("pin", row["id"]))
        self.assertEqual(row["provider"], "codex")
        self.assertNotIn("active_session", self.legacy.cfg)
        self.assertEqual(self.legacy.rows[-1]["title"], "Canary")

    async def test_publish_failure_compensates_by_retiring_new_pin(self):
        def fail_save(_cfg):
            raise RuntimeError("disk error")
        self.legacy.save_config = fail_save
        with self.assertRaisesRegex(RuntimeError, "disk error"):
            await self.integration.create_canary_session()
        self.assertEqual(self.controller.calls[-1][0], "retire")
        self.assertEqual(self.controller.pinned, set())

    async def test_provider_is_immutable_after_creation(self):
        row = await self.integration.create_web_session(
            provider="api", title="API chat", activate=True
        )
        with self.assertRaises(CodexCanaryLoopIntegrationError) as raised:
            self.integration.patch_web_session(row["id"], {"provider": "codex"})
        self.assertEqual(raised.exception.category, "web_session_provider_immutable")
        self.assertEqual(self.integration.provider_for_session(row["id"]), "api")

    async def test_retirement_keeps_codex_authority_and_removes_active_pin(self):
        row = await self.integration.create_web_session(
            provider="codex", title="Codex chat", activate=False
        )
        retired = self.integration.retire_canary_session(row["id"])
        self.assertEqual(retired["status"], "retired")
        self.assertNotIn(row["id"], self.controller.pinned)
        self.assertEqual(self.integration.provider_for_session(row["id"]), "codex")
        with self.assertRaisesRegex(
            CodexCanaryLoopIntegrationError, "provider_authority_mismatch"
        ):
            await self.integration.handle_ingest({
                "id": 42, "text": "after retirement", "session_id": row["id"]
            })

    async def test_install_replaces_legacy_control_and_session_projection(self):
        self.legacy.add_session("api-old")
        self.integration.install_legacy_globals()
        self.assertIsInstance(self.legacy.CODEX_CONTROL, LegacyControlAdapter)
        self.assertEqual(self.legacy.session_rows()[0]["provider"], "api")
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
