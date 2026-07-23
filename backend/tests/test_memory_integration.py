from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from backend.tests._support import NoNetworkMixin, load_app, request


TEST_HMAC_SECRET = "Synthetic-Memory-HMAC-Key-2026-Alpha!Z9q7"


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
                memory_tables = conn.execute(
                    """SELECT count(*) FROM sqlite_master
                       WHERE type='table' AND name LIKE 'memory_%'"""
                ).fetchone()[0]
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ready"])
        self.assertFalse(response.json()["checks"]["memory_core"])
        self.assertFalse(module.DEPLOYMENT.memory.enabled)
        self.assertFalse(module.DEPLOYMENT.heartbeat.enabled)
        self.assertNotIn("errors", response.json())
        self.assertEqual(memory_tables, 0)

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

    async def test_corrupt_enabled_memory_schema_fails_only_optional_memory_check(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(root, memory=True)
            with closing(sqlite3.connect(module.DB_PATH)) as conn:
                conn.execute("DROP INDEX idx_memory_items_live_fingerprint")
                conn.commit()
            response = await self._ready(module)
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.json()["checks"]["database"])
        self.assertFalse(response.json()["checks"]["memory_core"])
        self.assertEqual(
            response.json()["errors"]["memory_core"], "memory_schema_invalid"
        )

    async def test_corrupt_disabled_v7_does_not_block_core_startup_or_readiness(self):
        with tempfile.TemporaryDirectory() as root:
            enabled = load_app(root, memory=True)
            with closing(sqlite3.connect(enabled.DB_PATH)) as conn:
                conn.execute("DROP INDEX idx_memory_items_live_fingerprint")
                conn.commit()
            disabled = load_app(root, memory=False)
            response = await self._ready(disabled)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ready"])
        self.assertTrue(response.json()["checks"]["database"])
        self.assertFalse(response.json()["checks"]["memory_core"])
        self.assertNotIn("errors", response.json())

    async def test_disabled_memory_schema_corruption_matrix_is_isolated(self):
        cases = (
            ("items_missing", "DROP TABLE memory_items"),
            ("sources_missing", "DROP TABLE memory_sources"),
            ("suppressions_missing", "DROP TABLE memory_suppressions"),
            ("profile_missing", "DROP TABLE memory_fingerprint_profile"),
            ("evidence_missing", "DROP TABLE memory_evidence_events"),
            ("partial_index_missing", "DROP INDEX idx_memory_items_live_fingerprint"),
            (
                "sources_shape_corrupt",
                """ALTER TABLE memory_sources RENAME TO memory_sources_original;
                   CREATE TABLE memory_sources(id INTEGER PRIMARY KEY)""",
            ),
        )
        for name, sql in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                enabled = load_app(root, memory=True)
                with closing(sqlite3.connect(enabled.DB_PATH)) as conn:
                    conn.executescript(sql)
                    conn.commit()
                disabled = load_app(root, memory=False)
                response = await self._ready(disabled)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["checks"]["database"])
                self.assertFalse(response.json()["checks"]["memory_core"])
                self.assertNotIn("errors", response.json())

    async def test_enabled_memory_schema_corruption_matrix_fails_closed(self):
        cases = (
            ("items_missing", "DROP TABLE memory_items"),
            ("sources_missing", "DROP TABLE memory_sources"),
            ("suppressions_missing", "DROP TABLE memory_suppressions"),
            ("profile_missing", "DROP TABLE memory_fingerprint_profile"),
            ("evidence_missing", "DROP TABLE memory_evidence_events"),
            ("partial_index_missing", "DROP INDEX idx_memory_items_live_fingerprint"),
            (
                "sources_shape_corrupt",
                """ALTER TABLE memory_sources RENAME TO memory_sources_original;
                   CREATE TABLE memory_sources(id INTEGER PRIMARY KEY)""",
            ),
        )
        for name, sql in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                initial = load_app(root, memory=True)
                with closing(sqlite3.connect(initial.DB_PATH)) as conn:
                    conn.executescript(sql)
                    conn.commit()
                enabled = load_app(root, memory=True)
                response = await self._ready(enabled)
                payload = response.json()
                self.assertEqual(response.status_code, 503)
                self.assertTrue(payload["checks"]["database"])
                self.assertFalse(payload["checks"]["memory_core"])
                self.assertEqual(
                    payload["errors"]["memory_core"], "memory_schema_invalid"
                )
                self.assertNotIn("CREATE TABLE", str(payload))
                self.assertNotIn(str(root), str(payload))

    async def test_core_corruption_fails_startup_and_readiness_with_memory_disabled_or_enabled(self):
        cases = (
            ("rate_limits", "DROP TABLE channel_rate_limits"),
            ("audit_events", "DROP TABLE channel_audit_events"),
            (
                "core_index",
                "DROP INDEX idx_generation_jobs_status_lease",
            ),
        )
        for memory_enabled in (False, True):
            for name, sql in cases:
                with (
                    self.subTest(memory=memory_enabled, case=name),
                    tempfile.TemporaryDirectory() as root,
                ):
                    initial = load_app(root, memory=memory_enabled)
                    with closing(sqlite3.connect(initial.DB_PATH)) as conn:
                        conn.execute(sql)
                        conn.commit()
                    restarted = load_app(root, memory=memory_enabled)
                    response = await self._ready(restarted)
                    payload = response.json()
                    self.assertEqual(
                        restarted.CORE_STARTUP_ERROR,
                        "core_schema_invalid",
                    )
                    self.assertEqual(response.status_code, 503)
                    self.assertFalse(payload["ready"])
                    self.assertFalse(payload["checks"]["database"])
                    self.assertEqual(
                        payload["errors"]["database"],
                        "core_schema_invalid",
                    )
                    self.assertNotIn("channel_", str(payload))
                    self.assertNotIn("CREATE TABLE", str(payload))
                    self.assertNotIn(str(root), str(payload))

    async def test_phase1_exposes_no_memory_http_route(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(root, memory=True)
        paths = {route.path for route in module.app.routes}
        self.assertFalse(any("memory" in path.lower() for path in paths))


if __name__ == "__main__":
    unittest.main()
