from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi import HTTPException

from backend.tests._support import NoNetworkMixin, load_app, request


INTERNAL_TOKEN = "test-internal-loop-token-1234567890"


class _FakeControl:
    def __init__(self):
        self.calls: list[str] = []

    async def status(self):
        self.calls.append("status")
        return {
            "connected": True, "account_type": "chatgpt", "plan_type": "plus",
            "requires_openai_auth": False, "rate_limits": [],
        }

    async def usage(self):
        self.calls.append("usage")
        return {"lifetime_tokens": 12, "daily_usage_buckets": []}

    async def login_start(self):
        self.calls.append("login_start")
        return {"verification_url": "https://example.invalid/device", "user_code": "ABCD", "status": "pending"}

    async def login_cancel(self):
        self.calls.append("login_cancel")
        return {"cancelled": True}

    async def logout(self):
        self.calls.append("logout")
        return {"logged_out": True}


class InternalProviderControlTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        env = {
            "API_LOOP_INTERNAL_TOKEN": INTERNAL_TOKEN,
            "LOOP_CONFIG": str(root / "loop.json"),
            "RELAY_DB": str(root / "relay.db"),
            "LLM_MODEL": "current-api-provider",
            "LLM_API_BASE": "https://provider.invalid/v1",
            "LLM_API_KEY": "test-key",
            "CODEX_CONTROL_ENABLED": "false",
            "CODEX_HOME": str(root / "codex-home"),
            "CODEX_WORKSPACE": str(root / "codex-workspace"),
        }
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop("examples.api_loop", None)
        self.addCleanup(sys.modules.pop, "examples.api_loop", None)
        self.module = importlib.import_module("examples.api_loop")
        self.disabled_control = self.module.CODEX_CONTROL
        self.control = _FakeControl()
        self.module.CODEX_CONTROL = self.control

        async def forbidden(*args, **kwargs):
            raise AssertionError("generation path invoked by provider control")

        self.module.run_model = forbidden
        self.module.complete_chat = forbidden
        self.module.stream_chat = forbidden
        self.module.run_kelivo_provider_contract = forbidden
        self.module.handle_ingest = forbidden

    async def call(self, method: str, path: str, *, authenticated: bool = True):
        headers = {"X-API-Loop-Internal-Token": INTERNAL_TOKEN} if authenticated else {}
        transport = httpx.ASGITransport(app=self.module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, headers=headers)

    async def test_every_internal_route_requires_existing_loop_token(self):
        for method, path in (
            ("GET", "/loop/provider/status"),
            ("GET", "/loop/provider/usage"),
            ("POST", "/loop/provider/login/start"),
            ("POST", "/loop/provider/login/cancel"),
            ("POST", "/loop/provider/logout"),
        ):
            with self.subTest(path=path):
                response = await self.call(method, path, authenticated=False)
                self.assertEqual(response.status_code, 401)
        self.assertEqual(self.control.calls, [])

    async def test_status_is_explicitly_api_and_never_generates(self):
        response = await self.call("GET", "/loop/provider/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["generation_provider"], "api")
        self.assertEqual(self.control.calls, ["status"])

    async def test_source_default_disabled_is_a_fixed_failure_without_launch(self):
        self.module.CODEX_CONTROL = self.disabled_control
        response = await self.call("GET", "/loop/provider/status")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "codex_control_disabled"})

    async def test_usage_login_cancel_and_logout_call_only_control_plane(self):
        cases = (
            ("GET", "/loop/provider/usage", "usage"),
            ("POST", "/loop/provider/login/start", "login_start"),
            ("POST", "/loop/provider/login/cancel", "login_cancel"),
            ("POST", "/loop/provider/logout", "logout"),
        )
        for method, path, expected in cases:
            with self.subTest(path=path):
                self.control.calls.clear()
                response = await self.call(method, path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.control.calls, [expected])

    async def test_no_provider_selection_or_generation_route_was_added(self):
        response = await self.call("POST", "/loop/provider/select")
        self.assertEqual(response.status_code, 404)
        route_paths = {route.path for route in self.module.app.routes}
        self.assertFalse(any("thread" in path or "turn" in path for path in route_paths))


class ExternalProviderControlTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(self.temp.name, telegram=False)
        self.proxied: list[tuple[str, str]] = []
        self.original_provider_proxy = self.module.provider_loop_json

        def proxy(path: str, method: str = "GET"):
            self.proxied.append((path, method))
            if path.endswith("status"):
                return {"generation_provider": "codex", "connected": True}
            return {"ok": True}

        self.module.provider_loop_json = proxy
        async def forbidden(*args, **kwargs):
            raise AssertionError("Kelivo generation invoked")
        self.module.KELIVO_GENERATOR = forbidden

    @property
    def auth(self):
        return {"Authorization": "Bearer test-relay-secret"}

    async def test_every_external_route_requires_existing_relay_auth(self):
        for method, path in (
            ("GET", "/provider/status"),
            ("GET", "/provider/usage"),
            ("POST", "/provider/login/start"),
            ("POST", "/provider/login/cancel"),
            ("POST", "/provider/logout"),
        ):
            with self.subTest(path=path):
                response = await request(self.module, method, path)
                self.assertEqual(response.status_code, 401)
        self.assertEqual(self.proxied, [])

    async def test_facade_uses_exact_internal_paths_and_forces_api_authority(self):
        cases = (
            ("GET", "/provider/status", "/loop/provider/status", "GET"),
            ("GET", "/provider/usage", "/loop/provider/usage", "GET"),
            ("POST", "/provider/login/start", "/loop/provider/login/start", "POST"),
            ("POST", "/provider/login/cancel", "/loop/provider/login/cancel", "POST"),
            ("POST", "/provider/logout", "/loop/provider/logout", "POST"),
        )
        for method, external, internal, proxied_method in cases:
            with self.subTest(path=external):
                self.proxied.clear()
                response = await request(self.module, method, external, headers=self.auth)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.proxied, [(internal, proxied_method)])
                if external.endswith("status"):
                    self.assertEqual(response.json()["generation_provider"], "api")

    async def test_proxy_collapses_raw_stderr_or_upstream_errors(self):
        def unsafe(*args, **kwargs):
            raise HTTPException(status_code=502, detail="token stderr account path")
        self.module.loop_json = unsafe
        self.module.provider_loop_json = self.original_provider_proxy
        with self.assertRaises(HTTPException) as raised:
            self.module.provider_loop_json("/loop/provider/status")
        self.assertEqual(raised.exception.detail, "codex_app_server_unavailable")
        self.assertNotIn("token", str(raised.exception))

    async def test_proxy_preserves_only_a_fixed_loop_error_category(self):
        def disabled(*args, **kwargs):
            raise HTTPException(
                status_code=503, detail='{"detail":"codex_control_disabled"}'
            )
        self.module.loop_json = disabled
        with self.assertRaises(HTTPException) as raised:
            self.original_provider_proxy("/loop/provider/status")
        self.assertEqual(raised.exception.detail, "codex_control_disabled")

    async def test_no_external_provider_select_route_exists(self):
        response = await request(
            self.module, "POST", "/provider/select", headers=self.auth, json={"provider": "codex"}
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
