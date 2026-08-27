from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.codex_account_control_facade import CodexAccountFacadeError
from backend.codex_app_server_shared_transport import CodexSharedTransportConfig
from backend.codex_generation_protocol import CodexGenerationConfig, CodexGenerationError
from backend.codex_shared_provider_foundation import SharedCodexProviderFoundation


class Scope:
    def __init__(self, methods, responses):
        self.methods = methods
        self.responses = responses
        self.calls = []

    async def request(self, method, params):
        self.calls.append((method, params))
        return self.responses.get(method, {})


class Runtime:
    def __init__(self):
        self.responses = {
            "account/read": {"account": {"type": "chatgpt", "planType": "plus"}},
            "account/rateLimits/read": {"rateLimits": {}},
            "model/list": {"data": [{
                "model": "gpt-5.6-sol",
                "isDefault": True,
                "defaultReasoningEffort": "high",
                "inputModalities": ["text"],
            }]},
        }
        self.scopes = []

    def scope(self, *, methods, notifications=frozenset(), handler=None):
        scope = Scope(methods, self.responses)
        self.scopes.append(scope)
        return scope

    async def close(self):
        pass


class CodexSharedGateIndependenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_generation_enabled_does_not_open_control_facade(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = Runtime()
            foundation = SharedCodexProviderFoundation(
                CodexSharedTransportConfig(True, root / "home", root / "workspace", 1),
                CodexGenerationConfig(True, root / "workspace"),
                control_enabled=False,
                _runtime=runtime,
            )
            with self.assertRaisesRegex(CodexAccountFacadeError, "codex_control_disabled"):
                await foundation.control.status()
            self.assertEqual(runtime.scopes[0].calls, [])
            selected = await foundation.generation.qualify()
            self.assertEqual(selected.model, "gpt-5.6-sol")
            self.assertEqual([call[0] for call in runtime.scopes[1].calls], ["account/read", "model/list"])

    async def test_control_enabled_does_not_open_generation_when_generation_gate_is_off(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = Runtime()
            foundation = SharedCodexProviderFoundation(
                CodexSharedTransportConfig(True, root / "home", root / "workspace", 1),
                CodexGenerationConfig(False, root / "workspace"),
                control_enabled=True,
                _runtime=runtime,
            )
            status = await foundation.control.status()
            self.assertTrue(status["connected"])
            with self.assertRaisesRegex(CodexGenerationError, "codex_generation_disabled"):
                await foundation.generation.qualify()
            self.assertEqual(runtime.scopes[1].calls, [])


if __name__ == "__main__":
    unittest.main()
