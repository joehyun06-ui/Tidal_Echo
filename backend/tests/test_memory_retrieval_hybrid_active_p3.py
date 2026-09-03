from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

from backend import memory_retrieval_hybrid_runtime_active as runtime_active
from backend.tests._support import NoNetworkMixin, load_app, request


class HybridActiveP3Tests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        load_app(self.temp.name, telegram=False)
        os.environ.update({
            "LEGACY_CHAT_BRIDGE_TOKEN": "test-legacy-bridge-token-1234567890",
            "LEGACY_CHAT_BRIDGE_SESSION": "legacy-test",
            runtime_active.ENV_GATE: "false",
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

    async def test_active_status_requires_existing_relay_auth(self):
        response = await request(
            self.module,
            "GET",
            "/app/memory/hybrid-active/status",
        )
        self.assertEqual(response.status_code, 401)

    async def test_active_status_gate_off_is_zero_and_separate_from_readyz(self):
        response = await request(
            self.module,
            "GET",
            "/app/memory/hybrid-active/status",
            headers={"Authorization": "Bearer test-relay-secret"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["enabled"])
        self.assertTrue(payload["installed"])
        self.assertTrue(payload["observability_available"])
        self.assertEqual(payload["attempts"], 0)
        self.assertEqual(payload["in_flight"], 0)
        self.assertEqual(
            payload["outcomes"],
            {"completed": 0, "failed": 0, "timed_out": 0, "cancelled": 0},
        )
        ready_paths = {
            route.path
            for route in self.module.app.routes
            if getattr(route, "path", None) == "/readyz"
        }
        self.assertEqual(ready_paths, {"/readyz"})
        self.assertTrue(self.module.relay_app._P3_HYBRID_ACTIVE_STATUS_INSTALLED)

    def test_active_install_precedes_shadow_install_in_p3_entrypoint(self):
        source = Path(self.module.__file__).read_text(encoding="utf-8")
        active_call = "memory_retrieval_hybrid_runtime_active.install(relay_app)"
        shadow_call = "memory_retrieval_hybrid_runtime_shadow.install("
        self.assertIn(active_call, source)
        self.assertIn(shadow_call, source)
        self.assertLess(source.index(active_call), source.index(shadow_call))


if __name__ == "__main__":
    unittest.main()
