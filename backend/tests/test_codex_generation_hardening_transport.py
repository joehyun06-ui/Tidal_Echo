from __future__ import annotations

import unittest

from backend.codex_generation_hardening_transport import (
    OFFICIAL_0147_DENY_CONFIG,
    CodexGenerationHardeningTransport,
)


class CaptureTransport:
    def __init__(self):
        self.calls = []

    async def request(self, method, params):
        self.calls.append((method, params))
        return {}


class HardeningTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_thread_start_is_rewritten_to_exact_pinned_deny_profile(self):
        inner = CaptureTransport()
        transport = CodexGenerationHardeningTransport(inner)
        await transport.request("thread/start", {
            "model": "gpt-5.6-sol",
            "config": {
                "features.shell_tool": True,
                "mcp_servers.browser.enabled": False,
                "mcp_servers.local_tools.enabled": False,
                "unreviewed": "must disappear",
            },
            "environments": ["unsafe"],
            "dynamicTools": ["unsafe"],
            "runtimeWorkspaceRoots": ["/unsafe"],
            "selectedCapabilityRoots": ["unsafe"],
            "experimentalRawEvents": True,
        })
        method, params = inner.calls[-1]
        self.assertEqual(method, "thread/start")
        for key, value in OFFICIAL_0147_DENY_CONFIG.items():
            self.assertEqual(params["config"][key], value)
        self.assertNotIn("unreviewed", params["config"])
        self.assertEqual(params["config"]["mcp_servers"], {
            "browser": {"enabled": False},
            "local_tools": {"enabled": False},
        })
        self.assertEqual(params["environments"], [])
        self.assertEqual(params["runtimeWorkspaceRoots"], [])
        self.assertEqual(params["dynamicTools"], [])
        self.assertEqual(params["selectedCapabilityRoots"], [])
        self.assertFalse(params["experimentalRawEvents"])
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertEqual(params["sandbox"], "read-only")

    async def test_resume_is_hardened_without_adding_start_only_fields(self):
        inner = CaptureTransport()
        transport = CodexGenerationHardeningTransport(inner)
        await transport.request("thread/resume", {
            "threadId": "thr-1",
            "config": {"mcp_servers.a.enabled": False},
            "environments": ["unsafe"],
        })
        _, params = inner.calls[-1]
        self.assertEqual(params["environments"], [])
        self.assertEqual(params["runtimeWorkspaceRoots"], [])
        self.assertEqual(params["config"]["mcp_servers"], {"a": {"enabled": False}})
        self.assertNotIn("dynamicTools", params)

    async def test_non_thread_methods_pass_through_unchanged(self):
        inner = CaptureTransport()
        transport = CodexGenerationHardeningTransport(inner)
        payload = {"refreshToken": False}
        await transport.request("account/read", payload)
        self.assertEqual(inner.calls[-1], ("account/read", payload))


if __name__ == "__main__":
    unittest.main()
