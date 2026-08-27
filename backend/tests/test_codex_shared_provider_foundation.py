from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from backend.codex_account_control_facade import CodexAccountFacadeError
from backend.codex_app_server_shared_transport import CodexSharedTransportConfig
from backend.codex_generation_hardening_transport import OFFICIAL_0147_DENY_CONFIG
from backend.codex_generation_protocol import CodexGenerationConfig, CodexGenerationError
from backend.codex_shared_provider_foundation import SharedCodexProviderFoundation


class FakeScopedTransport:
    def __init__(self, methods, responses):
        self.methods = methods
        self.responses = responses
        self.calls = []

    async def request(self, method, params):
        if method not in self.methods:
            raise AssertionError(f"scope violation: {method}")
        self.calls.append((method, params))
        response = self.responses.get(method, {})
        if callable(response):
            return response(params)
        return response


class FakeRuntime:
    def __init__(self, responses):
        self.responses = responses
        self.scopes = []
        self.closed = False

    def scope(self, *, methods, notifications=frozenset(), handler=None):
        scope = FakeScopedTransport(methods, self.responses)
        scope.notifications = notifications
        scope.handler = handler
        self.scopes.append(scope)
        return scope

    async def close(self):
        self.closed = True


class SharedProviderFoundationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.responses = {
            "account/read": {"account": {"type": "chatgpt", "planType": "plus"}},
            "account/rateLimits/read": {"rateLimits": {"limitId": "primary", "primary": {"usedPercent": 1}}},
            "account/usage/read": {"summary": {"lifetimeTokens": 12}},
            "account/login/start": {"loginId": "login-1", "verificationUrl": "https://example.test", "userCode": "ABCD"},
            "account/login/cancel": {"status": "canceled"},
            "account/logout": {},
            "model/list": {"data": [{"model": "gpt-5.6-sol", "isDefault": True, "defaultReasoningEffort": "high"}]},
            "config/read": {"config": {"mcp_servers": {"browser": {"url": "PRIVATE"}}}},
            "thread/start": lambda params: {
                "thread": {"id": "thr-1", "ephemeral": False, "historyMode": "paginated"},
                "model": params["model"],
                "cwd": params["cwd"],
            },
        }
        self.runtime = FakeRuntime(self.responses)
        transport_config = CodexSharedTransportConfig(True, self.root / "home", self.root / "workspace", 1)
        generation_config = CodexGenerationConfig(True, self.root / "workspace")
        self.foundation = SharedCodexProviderFoundation(
            transport_config,
            generation_config,
            _runtime=self.runtime,
        )

    def tearDown(self):
        self.temp.cleanup()

    async def test_control_and_generation_receive_disjoint_scopes(self):
        control_scope, generation_scope = self.runtime.scopes
        self.assertIn("account/login/start", control_scope.methods)
        self.assertNotIn("turn/start", control_scope.methods)
        self.assertIn("turn/start", generation_scope.methods)
        self.assertNotIn("account/login/start", generation_scope.methods)

    async def test_p1_status_shape_is_preserved_over_shared_scope(self):
        status = await self.foundation.control.status()
        self.assertTrue(status["connected"])
        self.assertEqual(status["plan_type"], "plus")
        self.assertEqual(status["rate_limits"][0]["limit_id"], "primary")

    async def test_p1_device_login_contract_is_preserved(self):
        result = await self.foundation.control.login_start()
        self.assertEqual(result, {
            "verification_url": "https://example.test",
            "user_code": "ABCD",
            "status": "pending",
        })
        self.assertNotIn("login-1", repr(result))
        self.assertEqual(await self.foundation.control.login_cancel(), {"cancelled": True})

    async def test_control_is_fail_fast_busy_while_generation_slot_is_owned(self):
        async with self.foundation.activity_gate.generation():
            with self.assertRaisesRegex(CodexAccountFacadeError, "codex_generation_busy"):
                await self.foundation.control.status()

    async def test_generation_qualification_cannot_mutate_account(self):
        selected = await self.foundation.generation.qualify()
        self.assertEqual(selected.model, "gpt-5.6-sol")
        generation_calls = [method for method, _ in self.runtime.scopes[1].calls]
        self.assertEqual(generation_calls, ["account/read", "model/list"])
        self.assertNotIn("account/login/start", generation_calls)
        self.assertNotIn("account/logout", generation_calls)

    async def test_generation_default_off_stays_transport_free(self):
        runtime = FakeRuntime(self.responses)
        foundation = SharedCodexProviderFoundation(
            CodexSharedTransportConfig(True, self.root / "home", self.root / "workspace", 1),
            CodexGenerationConfig(False, self.root / "workspace"),
            _runtime=runtime,
        )
        with self.assertRaisesRegex(CodexGenerationError, "codex_generation_disabled"):
            await foundation.generation.qualify()
        self.assertEqual(runtime.scopes[1].calls, [])

    async def test_composed_generation_uses_pinned_0147_hardening_profile(self):
        await self.foundation.generation.start_thread(
            api_session="api-canary", attempt_id="attempt-1", persona="persona"
        )
        method, params = self.runtime.scopes[1].calls[-1]
        self.assertEqual(method, "thread/start")
        for key, value in OFFICIAL_0147_DENY_CONFIG.items():
            self.assertEqual(params["config"][key], value)
        self.assertEqual(params["config"]["mcp_servers"], {
            "browser": {"enabled": False}
        })
        self.assertEqual(params["environments"], [])
        self.assertEqual(params["runtimeWorkspaceRoots"], [])
        self.assertEqual(params["dynamicTools"], [])
        self.assertEqual(params["selectedCapabilityRoots"], [])

    async def test_generation_notifications_are_sanitized_before_callback(self):
        seen = []
        runtime = FakeRuntime(self.responses)
        foundation = SharedCodexProviderFoundation(
            CodexSharedTransportConfig(True, self.root / "home", self.root / "workspace", 1),
            CodexGenerationConfig(True, self.root / "workspace"),
            generation_event_handler=lambda event: seen.append(event),
            _runtime=runtime,
        )
        handler = runtime.scopes[1].handler
        await handler("error", {
            "threadId": "thr-1",
            "turnId": "turn-1",
            "willRetry": False,
            "error": {"message": "PRIVATE", "codexErrorInfo": "serverOverloaded"},
        })
        self.assertEqual(seen[0].error_info, "serverOverloaded")
        self.assertNotIn("PRIVATE", repr(seen[0]))

    async def test_async_generation_callback_does_not_block_reader_handler(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_callback(_event):
            started.set()
            await release.wait()

        runtime = FakeRuntime(self.responses)
        foundation = SharedCodexProviderFoundation(
            CodexSharedTransportConfig(True, self.root / "home", self.root / "workspace", 1),
            CodexGenerationConfig(True, self.root / "workspace"),
            generation_event_handler=slow_callback,
            _runtime=runtime,
        )
        handler = runtime.scopes[1].handler
        await asyncio.wait_for(handler("turn/started", {
            "threadId": "thr-1",
            "turn": {"id": "turn-1", "status": "inProgress"},
        }), timeout=0.1)
        await asyncio.sleep(0)
        self.assertTrue(started.is_set())
        release.set()
        await asyncio.sleep(0)

    async def test_close_closes_only_shared_runtime_once(self):
        await self.foundation.close()
        self.assertTrue(self.runtime.closed)


if __name__ == "__main__":
    unittest.main()
