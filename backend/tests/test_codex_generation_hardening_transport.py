from __future__ import annotations

import unittest

from backend.codex_app_server_shared_transport import CodexTransportError
from backend.codex_generation_hardening_transport import (
    OFFICIAL_0147_DENY_CONFIG,
    CodexGenerationHardeningTransport,
)


class CaptureTransport:
    def __init__(self, effective_mcp=None):
        self.calls = []
        self.effective_mcp = effective_mcp or {}

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == "config/read":
            return {
                "config": {
                    "additional": {
                        "mcp_servers": self.effective_mcp,
                    }
                }
            }
        return {}


class HardeningTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_thread_start_reads_effective_cwd_and_disables_all_mcp_servers(self):
        inner = CaptureTransport({
            "project_mcp": {"command": "PRIVATE"},
            "browser": {"url": "PRIVATE"},
        })
        transport = CodexGenerationHardeningTransport(inner)
        await transport.request("thread/start", {
            "model": "gpt-5.6-sol",
            "cwd": "/var/data/codex-workspace/sessions/api-1/attempt-1",
            "config": {
                "features.shell_tool": True,
                "mcp_servers.local_tools.enabled": False,
                "unreviewed": "must disappear",
            },
            "environments": ["unsafe"],
            "dynamicTools": ["unsafe"],
            "runtimeWorkspaceRoots": ["/unsafe"],
            "selectedCapabilityRoots": ["unsafe"],
            "experimentalRawEvents": True,
        })
        self.assertEqual(inner.calls[0], (
            "config/read",
            {
                "includeLayers": False,
                "cwd": "/var/data/codex-workspace/sessions/api-1/attempt-1",
            },
        ))
        method, params = inner.calls[-1]
        self.assertEqual(method, "thread/start")
        for key, value in OFFICIAL_0147_DENY_CONFIG.items():
            self.assertEqual(params["config"][key], value)
        self.assertNotIn("unreviewed", params["config"])
        self.assertEqual(params["config"]["mcp_servers"], {
            "browser": {"enabled": False},
            "local_tools": {"enabled": False},
            "project_mcp": {"enabled": False},
        })
        self.assertEqual(params["environments"], [])
        self.assertEqual(params["runtimeWorkspaceRoots"], [])
        self.assertEqual(params["dynamicTools"], [])
        self.assertEqual(params["selectedCapabilityRoots"], [])
        self.assertFalse(params["experimentalRawEvents"])
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertEqual(params["sandbox"], "read-only")

    async def test_resume_rechecks_effective_cwd_and_reasserts_read_only_contract(self):
        inner = CaptureTransport({"project_mcp": {"command": "PRIVATE"}})
        transport = CodexGenerationHardeningTransport(inner)
        await transport.request("thread/resume", {
            "threadId": "thr-1",
            "cwd": "/var/data/codex-workspace/sessions/api-1/attempt-1",
            "config": {"mcp_servers.a.enabled": False},
            "environments": ["unsafe"],
            "approvalPolicy": "on-request",
            "sandbox": "workspace-write",
        })
        self.assertEqual(inner.calls[0][0], "config/read")
        _, params = inner.calls[-1]
        self.assertEqual(params["environments"], [])
        self.assertEqual(params["runtimeWorkspaceRoots"], [])
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertEqual(params["sandbox"], "read-only")
        self.assertEqual(params["config"]["mcp_servers"], {
            "a": {"enabled": False},
            "project_mcp": {"enabled": False},
        })
        self.assertNotIn("dynamicTools", params)

    async def test_invalid_effective_config_fails_closed_before_thread_request(self):
        class BadConfigTransport(CaptureTransport):
            async def request(self, method, params):
                self.calls.append((method, params))
                if method == "config/read":
                    return {"config": {"additional": {"mcp_servers": ["not-a-map"]}}}
                return {}

        inner = BadConfigTransport()
        transport = CodexGenerationHardeningTransport(inner)
        with self.assertRaisesRegex(CodexTransportError, "protocol_error"):
            await transport.request("thread/start", {
                "cwd": "/var/data/codex-workspace/sessions/api-1/attempt-1",
            })
        self.assertEqual([method for method, _ in inner.calls], ["config/read"])

    async def test_missing_or_relative_cwd_fails_closed_before_config_read(self):
        for cwd in (None, "relative/path"):
            inner = CaptureTransport()
            transport = CodexGenerationHardeningTransport(inner)
            payload = {} if cwd is None else {"cwd": cwd}
            with self.subTest(cwd=cwd), self.assertRaisesRegex(
                CodexTransportError, "protocol_error"
            ):
                await transport.request("thread/start", payload)
            self.assertEqual(inner.calls, [])

    async def test_non_thread_methods_pass_through_unchanged(self):
        inner = CaptureTransport()
        transport = CodexGenerationHardeningTransport(inner)
        payload = {"refreshToken": False}
        await transport.request("account/read", payload)
        self.assertEqual(inner.calls[-1], ("account/read", payload))


if __name__ == "__main__":
    unittest.main()
