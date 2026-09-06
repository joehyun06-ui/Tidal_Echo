from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from backend.tests._support import NoNetworkMixin


TOKEN = "test-internal-loop-token-1234567890"


class ApiLoopMemoryHierarchyRouteTests(
    NoNetworkMixin,
    unittest.IsolatedAsyncioTestCase,
):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        env = {
            "LOOP_CONFIG": str(root / "loop.json"),
            "RELAY_DB": str(root / "relay.sqlite3"),
            "RELAY_SECRET": "invalid-test-relay-secret",
            "RELAY_URL": "http://invalid.test",
            "LLM_API_BASE": "https://provider.invalid/v1",
            "LLM_API_KEY": "invalid-key",
            "LLM_MODEL": "[Pro按量]gpt-5.6-sol",
            "LLM_MAX_TOKENS": "2000",
            "LLM_TEMPERATURE": "0.7",
            "LOOP_STREAM": "1",
            "API_LOOP_INTERNAL_TOKEN": TOKEN,
            "CODEX_CONTROL_ENABLED": "false",
            "RENDER_TELEGRAM_MVP": "false",
        }
        self.env_patch = mock.patch.dict(os.environ, env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        sys.modules.pop("examples.api_loop", None)
        self.addCleanup(lambda: sys.modules.pop("examples.api_loop", None))
        self.module = importlib.import_module("examples.api_loop")
        self.loopbacks = (
            importlib.import_module(
                "backend.memory_hierarchy_refinement_loopback"
            ),
            importlib.import_module(
                "backend.memory_hierarchy_summary_loopback_v2"
            ),
        )

    async def _post(
        self,
        endpoint: str,
        *,
        token: str | None = TOKEN,
        payload=None,
    ):
        headers = {}
        if token is not None:
            headers["X-API-Loop-Internal-Token"] = token
        transport = httpx.ASGITransport(app=self.module.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://loop.test",
        ) as client:
            return await client.post(
                endpoint,
                headers=headers,
                json={} if payload is None else payload,
            )

    def test_exact_post_routes_are_registered_once(self):
        for loopback in self.loopbacks:
            with self.subTest(endpoint=loopback.ENDPOINT):
                matches = [
                    route
                    for route in self.module.app.routes
                    if getattr(route, "path", None) == loopback.ENDPOINT
                ]
                self.assertEqual(len(matches), 1)
                self.assertEqual(getattr(matches[0], "methods", set()), {"POST"})

    async def test_routes_reject_missing_auth_before_provider_dispatch(self):
        provider = mock.AsyncMock()
        with mock.patch.object(
            self.module,
            "run_kelivo_provider_contract",
            new=provider,
        ):
            for loopback in self.loopbacks:
                with self.subTest(endpoint=loopback.ENDPOINT):
                    response = await self._post(loopback.ENDPOINT, token=None)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(
                        response.json(),
                        {"detail": "unauthorized"},
                    )

        provider.assert_not_awaited()

    async def test_routes_delegate_to_existing_handlers(self):
        for loopback in self.loopbacks:
            expected = {"ok": True, "endpoint": loopback.ENDPOINT}
            handler = mock.AsyncMock(return_value=expected)
            with self.subTest(endpoint=loopback.ENDPOINT):
                with mock.patch.object(
                    loopback,
                    "handle_request",
                    new=handler,
                ):
                    response = await self._post(loopback.ENDPOINT)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), expected)
                handler.assert_awaited_once()
                call = handler.await_args
                self.assertEqual(len(call.args), 2)
                self.assertIs(call.args[0], self.module)
                self.assertEqual(call.args[1].method, "POST")
                self.assertEqual(call.args[1].url.path, loopback.ENDPOINT)
                self.assertEqual(call.kwargs, {})


if __name__ == "__main__":
    unittest.main()
