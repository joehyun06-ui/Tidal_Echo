"""Isolated HTTP tests of session-write DRY-RUN preflight only.

No write routes, no user's database, no provider calls or sockets.
Import-heavy app modules deliberately run in a separate CI process.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from backend.tests._support import NoNetworkMixin, load_app, request

LOOP_TOKEN = "invalid-preflight-test-internal-token-12345"
PUBLIC = "/app/memory/session-write/preflight"
INTERNAL = "/loop/memory/session-write/preflight"
EXPECTED = {
    "schema": "guitting-session-write-preflight-v1",
    "mode": "dry_run",
    "active_session_verified": True,
    "would_write": False,
}


class LoopWritePreflightTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        patch = mock.patch.dict(os.environ, {
            "LOOP_CONFIG": str(root / "loop.config.json"),
            "RELAY_DB": str(root / "synthetic.db"),
            "RELAY_SECRET": "invalid-test-relay-secret",
            "RELAY_URL": "http://invalid.test",
            "LLM_API_BASE": "https://provider.invalid/v1",
            "LLM_API_KEY": "invalid-key",
            "LLM_MODEL": "gpt-test",
            "API_LOOP_INTERNAL_TOKEN": LOOP_TOKEN,
            "CODEX_CONTROL_ENABLED": "false",
            "RENDER_TELEGRAM_MVP": "false",
            "MEMORY_CORE_ENABLED": "true",
            "MEMORY_SESSION_BOUND_WRITE_ENABLED": "false",
            "MEMORY_SESSION_WRITE_PREFLIGHT_ENABLED": "true",
        }, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        sys.modules.pop("examples.api_loop", None)
        self.addCleanup(lambda: sys.modules.pop("examples.api_loop", None))
        self.module = importlib.import_module("examples.api_loop")

    async def get(self, *, token=LOOP_TOKEN, path=INTERNAL):
        transport = httpx.ASGITransport(app=self.module.app)
        headers = {} if token is None else {"X-API-Loop-Internal-Token": token}
        async with httpx.AsyncClient(transport=transport, base_url="http://loop.invalid") as client:
            return await client.get(path, headers=headers)

    async def test_internal_auth_first(self):
        with mock.patch.object(self.module, "session_rows", side_effect=AssertionError("leak")):
            r = await self.get(token=None)
        self.assertEqual(r.status_code, 401)

    async def test_unknown_internal_token_rejected(self):
        r = await self.get(token="invalid")
        self.assertEqual(r.status_code, 401)

    async def test_default_disabled_no_session_read(self):
        del os.environ["MEMORY_SESSION_WRITE_PREFLIGHT_ENABLED"]
        with mock.patch.object(self.module, "session_rows", side_effect=AssertionError("called")):
            r = await self.get()
        self.assertEqual(r.status_code, 404)

    async def test_server_active_session_only(self):
        with (
            mock.patch.object(self.module, "load_config", return_value={"active_session":"s1"}),
            mock.patch.object(self.module, "session_rows", return_value=[{"id":"s1"},{"id":"s2"}]),
        ):
            r = await self.get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), EXPECTED)
        self.assertEqual(r.headers.get("cache-control"), "private, no-store")
        self.assertNotIn("s1", str(r.json()))
        self.assertNotIn("token", str(r.json()).lower())

    async def test_active_session_not_in_own_rows_denied(self):
        with (
            mock.patch.object(self.module, "load_config", return_value={"active_session":"s3"}),
            mock.patch.object(self.module, "session_rows", return_value=[{"id":"s1"}]),
        ):
            r = await self.get()
        self.assertEqual(r.status_code, 409)

    async def test_missing_active_session_cannot_choose_last_session(self):
        with (
            mock.patch.object(self.module, "load_config", return_value={"sessions":[{"id":"s1"}]}),
            mock.patch.object(self.module, "session_rows", return_value=[{"id":"s1"}]),
        ):
            r = await self.get()
        self.assertEqual(r.status_code, 409)

    async def test_client_supplied_session_rejected(self):
        r = await self.get(path=INTERNAL+"?session_id=s2")
        self.assertEqual(r.status_code, 400)

    async def test_preflight_never_touches_memory_storage(self):
        with (
            mock.patch.object(self.module, "load_config", return_value={"active_session":"s1"}),
            mock.patch.object(self.module, "session_rows", return_value=[{"id":"s1"}]),
            mock.patch.object(self.module.channel_store, "connect", side_effect=AssertionError("write_db")),
            mock.patch.object(self.module.channel_store, "connect_read_only", side_effect=AssertionError("read_db")),
        ):
            r = await self.get()
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["would_write"])


class RelayWritePreflightTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        load_app(self.temp.name, telegram=False)
        os.environ.update({
            "LEGACY_CHAT_BRIDGE_TOKEN": "test-legacy-bridge-token-1234567890",
            "LEGACY_CHAT_BRIDGE_SESSION": "legacy-test",
            "MEMORY_CORE_ENABLED": "true",
            "MEMORY_SESSION_WRITE_PREFLIGHT_ENABLED": "true",
            "MEMORY_SESSION_BOUND_WRITE_ENABLED": "false",
            "CODEX_CONTROL_ENABLED": "false",
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

    async def get(self, url=PUBLIC, token="Bearer test-relay-secret"):
        headers = {"Authorization":token} if token is not None else {}
        return await request(self.module, "GET", url, headers=headers)

    async def test_requires_header_bearer_before_loop(self):
        with mock.patch.object(self.module.relay_app, "loop_json", side_effect=AssertionError("proxied")):
            r = await self.get(token=None)
        self.assertEqual(r.status_code, 401)

    async def test_query_token_cannot_authenticate_new_preflight(self):
        with mock.patch.object(self.module.relay_app, "loop_json", side_effect=AssertionError("proxied")):
            r = await self.get(PUBLIC+"?token=test-relay-secret", token=None)
        self.assertEqual(r.status_code, 401)

    async def test_query_session_selection_rejected(self):
        with mock.patch.object(self.module.relay_app, "loop_json", side_effect=AssertionError("proxied")):
            r = await self.get(PUBLIC+"?session_id=s2")
        self.assertEqual(r.status_code, 401)

    async def test_feature_gate_default_off(self):
        del os.environ["MEMORY_SESSION_WRITE_PREFLIGHT_ENABLED"]
        with mock.patch.object(self.module.relay_app, "loop_json", side_effect=AssertionError("proxied")):
            r = await self.get()
        self.assertEqual(r.status_code, 404)

    async def test_calls_only_internal_dry_run_route(self):
        with mock.patch.object(self.module.relay_app, "loop_json", return_value=EXPECTED) as proxy:
            r = await self.get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), EXPECTED)
        self.assertEqual(r.headers.get("cache-control"), "private, no-store")
        proxy.assert_called_once_with(INTERNAL)

    async def test_post_method_cannot_be_used_to_write(self):
        with mock.patch.object(
            self.module.relay_app, "loop_json",
            side_effect=AssertionError("unexpected_write_forward"),
        ):
            response = await request(
                self.module, "POST", PUBLIC,
                headers={"Authorization": "Bearer test-relay-secret"},
            )
        self.assertEqual(response.status_code, 405)

    async def test_upstream_cannot_return_writable_capability(self):
        with mock.patch.object(
            self.module.relay_app, "loop_json",
            return_value={**EXPECTED, "would_write":True},
        ):
            r = await self.get()
        self.assertEqual(r.status_code, 502)

    async def test_upstream_cannot_leak_session_id(self):
        with mock.patch.object(
            self.module.relay_app, "loop_json",
            return_value={**EXPECTED, "api_session":"s1"},
        ):
            r = await self.get()
        self.assertEqual(r.status_code, 502)


if __name__ == "__main__":
    unittest.main()
