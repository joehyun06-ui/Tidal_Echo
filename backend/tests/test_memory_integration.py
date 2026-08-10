from __future__ import annotations

import dataclasses
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

    def _assert_no_entry_writer(self, module):
        self.assertIsNone(module.MEMORY_EXPLICIT_ENTRY_SERVICES)
        self.assertIsNone(module.MEMORY_PRIVILEGED_RUNTIME)
        self.assertIsNone(module.MEMORY_CANDIDATE_PERSISTENCE)
        self.assertIsNone(module.memory_runtime._PROCESS_AUTHORITY)
        self.assertFalse(hasattr(module.app.state, "memory_runtime"))
        self.assertFalse(hasattr(module.app.state, "privileged_memory_actions"))
        self.assertFalse(any(
            "memory" in route.path.lower() for route in module.app.routes
        ))

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
        self.assertNotIn(
            "memory_auto_candidate_persistence",
            response.json()["checks"],
        )
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
        self.assertNotIn(
            "memory_auto_candidate_persistence",
            response.json()["checks"],
        )
        self._assert_no_entry_writer(module)

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

    async def test_entry_false_retains_read_only_object_graph(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(
                root,
                memory=True,
                memory_writes=True,
                memory_secret=TEST_HMAC_SECRET,
                memory_entry=False,
            )
            response = await self._ready(module)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("memory_explicit_entry", response.json()["checks"])
        self._assert_no_entry_writer(module)

    async def test_entry_requires_core_and_writes_without_harming_core_check(self):
        cases = (
            (
                "core_disabled",
                False,
                False,
                False,
                "memory_explicit_entry_requires_core",
            ),
            (
                "writes_disabled",
                True,
                False,
                True,
                "memory_explicit_entry_requires_writes",
            ),
        )
        for name, core, writes, core_ready, category in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                module = load_app(
                    root,
                    memory=core,
                    memory_writes=writes,
                    memory_entry=True,
                )
                response = await self._ready(module)
                payload = response.json()
                self.assertEqual(response.status_code, 503)
                self.assertEqual(
                    payload["checks"]["memory_core"],
                    core_ready,
                )
                self.assertFalse(
                    payload["checks"]["memory_explicit_entry"]
                )
                self.assertEqual(
                    payload["errors"]["memory_explicit_entry"],
                    category,
                )
                if core_ready:
                    self.assertNotIn("memory_core", payload["errors"])
                self._assert_no_entry_writer(module)

    async def test_entry_missing_secret_has_no_writer(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(
                root,
                memory=True,
                memory_writes=True,
                memory_secret="",
                memory_entry=True,
            )
            response = await self._ready(module)
        payload = response.json()
        self.assertEqual(response.status_code, 503)
        self.assertFalse(payload["checks"]["memory_explicit_entry"])
        self.assertEqual(
            payload["errors"]["memory_explicit_entry"],
            "memory_fingerprint_hmac_secret_missing",
        )
        self._assert_no_entry_writer(module)

    async def test_entry_invalid_schema_and_profile_have_no_writer(self):
        cases = (
            (
                "schema",
                "DROP INDEX idx_memory_items_live_fingerprint",
                "memory_schema_invalid",
            ),
            (
                "profile",
                """INSERT INTO memory_fingerprint_profile(
                       singleton,key_id,key_check,normalization_version,
                       fingerprint_version,created_at,updated_at
                   ) VALUES(1,'wrong-key',zeroblob(32),1,1,'now','now')""",
                "memory_fingerprint_profile_mismatch",
            ),
        )
        for name, sql, category in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                initial = load_app(
                    root,
                    memory=True,
                    memory_writes=True,
                    memory_secret=TEST_HMAC_SECRET,
                    memory_entry=False,
                )
                with closing(sqlite3.connect(initial.DB_PATH)) as connection:
                    connection.execute(sql)
                    connection.commit()
                module = load_app(
                    root,
                    memory=True,
                    memory_writes=True,
                    memory_secret=TEST_HMAC_SECRET,
                    memory_entry=True,
                )
                response = await self._ready(module)
                payload = response.json()
                self.assertEqual(response.status_code, 503)
                self.assertFalse(
                    payload["checks"]["memory_explicit_entry"]
                )
                self.assertEqual(
                    payload["errors"]["memory_explicit_entry"],
                    category,
                )
                self._assert_no_entry_writer(module)

    async def test_fully_valid_entry_constructs_only_internal_bound_services(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(
                root,
                memory=True,
                memory_writes=True,
                memory_secret=TEST_HMAC_SECRET,
                memory_entry=True,
            )
            response = await self._ready(module)
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["checks"]["memory_core"])
        self.assertTrue(payload["checks"]["memory_explicit_entry"])
        self.assertNotIn("memory_auto_candidate_persistence", payload["checks"])
        services = module.MEMORY_EXPLICIT_ENTRY_SERVICES
        self.assertIsInstance(services, tuple)
        self.assertEqual(
            tuple(service._origin for service in services),
            ("operator_cli", "mcp", "telegram", "operit"),
        )
        self.assertTrue(all(
            type(service)
            is module.memory_explicit_actions.ExplicitMemoryActionService
            for service in services
        ))
        runtime = module.MEMORY_PRIVILEGED_RUNTIME
        self.assertIsNotNone(runtime)
        self.assertIsNone(module.MEMORY_CANDIDATE_PERSISTENCE)
        policy = module.memory_runtime.require_runtime_authority(
            runtime.privileged_actions._authority
        )
        self.assertTrue(policy.explicit_writes_enabled)
        self.assertFalse(policy.auto_candidate_persistence_enabled)
        self.assertFalse(hasattr(module.app.state, "memory_runtime"))
        self.assertFalse(hasattr(module.app.state, "privileged_memory_actions"))
        self.assertFalse(any(
            "memory" in route.path.lower() for route in module.app.routes
        ))

    async def test_candidate_only_bootstraps_shared_runtime_without_explicit_entry(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(
                root,
                kelivo=True,
                memory=True,
                memory_secret=TEST_HMAC_SECRET,
                memory_auto_formation=True,
                memory_candidate_persistence=True,
            )
            response = await self._ready(module)
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["checks"]["memory_core"])
        self.assertTrue(
            payload["checks"]["memory_auto_candidate_persistence"]
        )
        self.assertNotIn("memory_explicit_entry", payload["checks"])
        runtime = module.MEMORY_PRIVILEGED_RUNTIME
        persistence = module.MEMORY_CANDIDATE_PERSISTENCE
        self.assertIsNotNone(runtime)
        self.assertIs(persistence, runtime.candidate_persistence)
        self.assertIsNone(module.MEMORY_EXPLICIT_ENTRY_SERVICES)
        self.assertIs(persistence._store, runtime.privileged_actions._store)
        self.assertIs(persistence._authority, runtime.privileged_actions._authority)
        policy = module.memory_runtime.require_runtime_authority(
            persistence._authority
        )
        self.assertFalse(policy.explicit_writes_enabled)
        self.assertTrue(policy.auto_candidate_persistence_enabled)

    async def test_explicit_and_candidate_share_one_runtime_and_bootstrap_once(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(
                root,
                kelivo=True,
                memory=True,
                memory_writes=True,
                memory_secret=TEST_HMAC_SECRET,
                memory_entry=True,
                memory_auto_formation=True,
                memory_candidate_persistence=True,
            )
            with mock.patch.object(
                module.memory_runtime,
                "bootstrap_memory_runtime_from_environment",
                side_effect=AssertionError("runtime bootstrap repeated"),
            ) as bootstrap:
                module.init_db()
            response = await self._ready(module)
        bootstrap.assert_not_called()
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["checks"]["memory_explicit_entry"])
        self.assertTrue(
            payload["checks"]["memory_auto_candidate_persistence"]
        )
        runtime = module.MEMORY_PRIVILEGED_RUNTIME
        persistence = module.MEMORY_CANDIDATE_PERSISTENCE
        services = module.MEMORY_EXPLICIT_ENTRY_SERVICES
        self.assertIs(persistence, runtime.candidate_persistence)
        self.assertTrue(all(
            service._backend._store is persistence._store
            for service in services
        ))
        self.assertTrue(all(
            service._backend._actions._authority is persistence._authority
            for service in services
        ))
        policy = module.memory_runtime.require_runtime_authority(
            persistence._authority
        )
        self.assertTrue(policy.explicit_writes_enabled)
        self.assertTrue(policy.auto_candidate_persistence_enabled)

    async def test_candidate_bootstrap_failure_is_fail_closed_and_readyz_gates(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(
                root,
                kelivo=True,
                memory=True,
                memory_secret=TEST_HMAC_SECRET,
                memory_auto_formation=True,
            )
            module.DEPLOYMENT = dataclasses.replace(
                module.DEPLOYMENT,
                memory=dataclasses.replace(
                    module.DEPLOYMENT.memory,
                    auto_candidate_persistence_enabled=True,
                ),
            )
            with mock.patch.object(
                module.memory_runtime,
                "bootstrap_memory_runtime_from_environment",
                side_effect=module.memory_runtime.MemoryRuntimeError(
                    "memory_schema_invalid"
                ),
            ):
                module._compose_memory_privileged_runtime()
            response = await self._ready(module)
        payload = response.json()
        self.assertEqual(response.status_code, 503)
        self.assertFalse(payload["ready"])
        self.assertTrue(payload["checks"]["memory_core"])
        self.assertFalse(
            payload["checks"]["memory_auto_candidate_persistence"]
        )
        self.assertEqual(
            payload["errors"]["memory_auto_candidate_persistence"],
            "memory_schema_invalid",
        )
        self.assertIsNone(module.MEMORY_PRIVILEGED_RUNTIME)
        self.assertIsNone(module.MEMORY_CANDIDATE_PERSISTENCE)
        self.assertIsNone(module.memory_runtime._PROCESS_AUTHORITY)

    async def test_candidate_enabled_missing_handle_fails_readiness_closed(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(
                root,
                kelivo=True,
                memory=True,
                memory_secret=TEST_HMAC_SECRET,
                memory_auto_formation=True,
                memory_candidate_persistence=True,
            )
            module.MEMORY_CANDIDATE_PERSISTENCE = None
            module.MEMORY_CANDIDATE_PERSISTENCE_ERROR = (
                "memory_auto_candidate_persistence_unavailable"
            )
            response = await self._ready(module)
        payload = response.json()
        self.assertEqual(response.status_code, 503)
        self.assertFalse(
            payload["checks"]["memory_auto_candidate_persistence"]
        )
        self.assertEqual(
            payload["errors"]["memory_auto_candidate_persistence"],
            "memory_auto_candidate_persistence_unavailable",
        )

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
        for name in (
            "create_explicit_memory",
            "correct_memory",
            "forget_memory",
            "remember_explicit_user_message",
        ):
            self.assertFalse(hasattr(module.MEMORY_SERVICE, name))
        self.assertFalse(hasattr(module.MEMORY_SERVICE, "_authority"))
        self.assertFalse(hasattr(module.MEMORY_SERVICE, "_store"))
        self.assertFalse(hasattr(module.app.state, "memory_runtime"))
        self.assertFalse(hasattr(module.app.state, "privileged_memory_actions"))


if __name__ == "__main__":
    unittest.main()
