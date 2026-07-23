from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from backend.tests._support import NoNetworkMixin, load_app, request


TEST_HMAC_SECRET = "synthetic-memory-hmac-secret-000000000001"


class _TaskState:
    def done(self) -> bool:
        return False


class MemoryIntegrationTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def _ready(self, module):
        module.app.state.telegram_worker_task = _TaskState()
        with mock.patch.object(
            module, "_api_loop_ready", new=mock.AsyncMock(return_value=True)
        ):
            return await request(module, "GET", "/readyz")

    async def test_default_disabled_reports_false_without_blocking_readiness(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(root)
            response = await self._ready(module)
            with closing(sqlite3.connect(module.DB_PATH)) as conn:
                memory_count = conn.execute(
                    "SELECT count(*) FROM memory_items"
                ).fetchone()[0]
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ready"])
        self.assertFalse(response.json()["checks"]["memory_core"])
        self.assertFalse(module.DEPLOYMENT.memory.enabled)
        self.assertFalse(module.DEPLOYMENT.heartbeat.enabled)
        self.assertNotIn("errors", response.json())
        self.assertEqual(memory_count, 0)

    async def test_disabled_memory_runtime_failure_does_not_block_chat_readiness(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(root)
            module.app.state.telegram_worker_task = _TaskState()
            with (
                mock.patch.object(
                    module.MEMORY_SERVICE,
                    "readiness",
                    return_value=(False, "memory_schema_invalid"),
                ),
                mock.patch.object(
                    module, "_api_loop_ready", new=mock.AsyncMock(return_value=True)
                ),
            ):
                response = await request(module, "GET", "/readyz")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ready"])
        self.assertFalse(response.json()["checks"]["memory_core"])
        self.assertNotIn("errors", response.json())

    async def test_enabled_read_only_requires_no_hmac_and_is_ready(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(root, memory=True, memory_writes=False, memory_secret="")
            response = await self._ready(module)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["checks"]["memory_core"])
        self.assertTrue(module.DEPLOYMENT.memory.configuration_valid)

    async def test_enabled_writes_with_valid_hmac_is_ready(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(
                root,
                memory=True,
                memory_writes=True,
                memory_secret=TEST_HMAC_SECRET,
            )
            response = await self._ready(module)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["checks"]["memory_core"])

    async def test_enabled_writes_missing_hmac_fail_closed_in_readiness(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(root, memory=True, memory_writes=True, memory_secret="")
            response = await self._ready(module)
        payload = response.json()
        self.assertEqual(response.status_code, 503)
        self.assertFalse(payload["ready"])
        self.assertFalse(payload["checks"]["memory_core"])
        self.assertEqual(
            payload["errors"]["memory_core"], "memory_fingerprint_hmac_secret_missing"
        )
        self.assertNotIn("secret", str(payload).lower().replace(
            "memory_fingerprint_hmac_secret_missing", ""
        ))

    async def test_corrupt_memory_schema_fails_database_and_memory_checks(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(root, memory=True)
            with closing(sqlite3.connect(module.DB_PATH)) as conn:
                conn.execute("DROP INDEX idx_memory_items_live_fingerprint")
                conn.commit()
            response = await self._ready(module)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["checks"]["database"])
        self.assertFalse(response.json()["checks"]["memory_core"])
        self.assertEqual(
            response.json()["errors"]["memory_core"], "memory_schema_invalid"
        )

    async def test_phase1_exposes_no_memory_http_route(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(root, memory=True)
        paths = {route.path for route in module.app.routes}
        self.assertFalse(any("memory" in path.lower() for path in paths))


if __name__ == "__main__":
    unittest.main()
