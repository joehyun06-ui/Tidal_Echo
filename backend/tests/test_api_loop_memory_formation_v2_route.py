from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from backend import memory_formation_extractor_v2
from backend.tests._support import NoNetworkMixin


TOKEN = "test-internal-loop-token-1234567890"
SOURCE = "Project Atlas uses PostgreSQL 16."


class ApiLoopMemoryFormationV2RouteTests(
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
        self.loopback = importlib.import_module("backend.memory_formation_v2_loopback")

    async def _post(self, *, token: str | None = TOKEN, payload=None):
        headers = {}
        if token is not None:
            headers["X-API-Loop-Internal-Token"] = token
        transport = httpx.ASGITransport(app=self.module.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://loop.test",
        ) as client:
            return await client.post(
                self.loopback.ENDPOINT,
                headers=headers,
                json={"source_text": SOURCE} if payload is None else payload,
            )

    def test_exact_post_route_is_registered_once(self):
        matches = [
            route
            for route in self.module.app.routes
            if getattr(route, "path", None) == self.loopback.ENDPOINT
        ]
        self.assertEqual(len(matches), 1)
        self.assertIn("POST", getattr(matches[0], "methods", set()))

    async def test_route_rejects_missing_internal_auth_before_extraction(self):
        extraction = mock.AsyncMock()
        with mock.patch.object(
            self.loopback,
            "run_server_extraction",
            new=extraction,
        ):
            response = await self._post(token=None)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "unauthorized"})
        extraction.assert_not_awaited()

    async def test_route_delegates_to_existing_handler_and_returns_ranges_only(self):
        extraction = mock.AsyncMock(
            return_value=memory_formation_extractor_v2.AutoMemoryExtractionV2(
                proposals=(),
            )
        )
        with mock.patch.object(
            self.loopback,
            "run_server_extraction",
            new=extraction,
        ):
            response = await self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "ok": True,
                "version": memory_formation_extractor_v2.EXTRACTOR_CONTRACT_VERSION,
                "proposals": [],
            },
        )
        extraction.assert_awaited_once_with(self.module, SOURCE)

    async def test_route_keeps_strict_source_text_only_body(self):
        extraction = mock.AsyncMock()
        with mock.patch.object(
            self.loopback,
            "run_server_extraction",
            new=extraction,
        ):
            response = await self._post(
                payload={"source_text": SOURCE, "extra": True},
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json(),
            {"ok": False, "error": "invalid_source_text"},
        )
        extraction.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
