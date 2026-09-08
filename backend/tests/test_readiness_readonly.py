from __future__ import annotations

import contextlib
import importlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.tests._support import NoNetworkMixin, load_app, request


TEST_SECRET = "Synthetic-Readiness-HMAC-Key-2026-Alpha!Z9q7"


class ReadinessReadOnlyTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="readiness #? ")
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(
            self.temp.name,
            kelivo=True,
            operit_share=True,
            memory=True,
            memory_writes=True,
            memory_entry=True,
            memory_secret=TEST_SECRET,
        )
        self.path = Path(self.module.DB_PATH)
        self.reader = self.module.MEMORY_SERVICE._reader
        self.store = self.module.MEMORY_PRIVILEGED_RUNTIME.privileged_actions._store
        self.memory_store = importlib.import_module("backend.memory_store")
        self.module.app.state.telegram_worker_task = mock.Mock(
            done=mock.Mock(return_value=False)
        )

    def mapping_ready(self, path=None):
        config = self.module.DEPLOYMENT.kelivo
        return self.module.kelivo_service.client_mapping_ready(
            str(self.path) if path is None else path,
            config.client_id,
            config.api_session,
        )

    def local_checks(self):
        return (
            self.reader.validate_schema,
            self.store.validate_schema,
            self.reader.validate_runtime_profile_state,
            self.store.validate_runtime_profile_state,
            self.mapping_ready,
        )

    @contextlib.contextmanager
    def prove_sqlite_read_only(self):
        original = sqlite3.connect
        opened = []
        target_names = {str(self.path), self.path.as_uri() + "?mode=ro"}

        def connect(database, *args, **kwargs):
            conn = original(database, *args, **kwargs)
            if str(database) in target_names:
                opened.append(conn)
                try:
                    # Disabling the advisory guard must not remove SQLite's
                    # underlying read-only restriction on the authority DB.
                    conn.execute("PRAGMA query_only = OFF")
                    with self.assertRaises(sqlite3.OperationalError):
                        conn.execute("UPDATE kelivo_clients SET enabled=enabled")
                except BaseException:
                    conn.close()
                    raise
            return conn

        with mock.patch.object(sqlite3, "connect", side_effect=connect):
            yield opened
        self.assertTrue(opened)
        for conn in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    async def test_missing_database_checks_do_not_create_a_file(self):
        for target in (self.reader, self.store):
            with self.subTest(target=type(target).__name__):
                absent = self.path.parent / "missing-memory.db"
                with mock.patch.object(target, "path", str(absent)):
                    self.assertFalse(target.validate_schema())
                    self.assertFalse(absent.exists())
                    with self.assertRaisesRegex(
                        self.memory_store.MemoryStoreError, "^storage_unavailable$"
                    ):
                        target.validate_runtime_profile_state()
                    self.assertFalse(absent.exists())
        absent = self.path.parent / "missing-mapping.db"
        self.assertFalse(self.mapping_ready(str(absent)))
        self.assertFalse(absent.exists())

    async def test_healthy_checks_leave_schema_profile_and_data_unchanged(self):
        before = self.path.read_bytes()
        for check in self.local_checks():
            with self.subTest(check=check.__qualname__):
                self.assertTrue(check())
        self.assertEqual(self.path.read_bytes(), before)
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM memory_fingerprint_profile").fetchone()[0],
                0,
            )

    async def test_each_check_uses_a_read_only_connection_and_closes_it(self):
        for check in self.local_checks():
            with self.subTest(check=check.__qualname__), self.prove_sqlite_read_only():
                self.assertTrue(check())

    async def test_relative_paths_and_uri_characters_remain_supported(self):
        before = self.path.read_bytes()
        with contextlib.chdir(self.path.parent):
            for target in (self.reader, self.store):
                with mock.patch.object(target, "path", self.path.name):
                    self.assertTrue(target.validate_schema())
                    self.assertTrue(target.validate_runtime_profile_state())
            self.assertTrue(self.mapping_ready(self.path.name))
        self.assertEqual(self.path.read_bytes(), before)

    async def test_readiness_busy_timeout_honors_configuration_with_safe_bounds(self):
        for configured, expected_ms in (
            ("0.01", 10),
            ("2.5", 2500),
            ("invalid", 30000),
            ("0", 30000),
            ("-1", 30000),
            ("nan", 30000),
            ("inf", 30000),
            ("301", 300000),
        ):
            with (
                self.subTest(configured=configured),
                mock.patch.dict(os.environ, {"SQLITE_BUSY_TIMEOUT_SECONDS": configured}),
                self.module.channel_store.connect_readiness(self.path) as conn,
            ):
                self.assertEqual(
                    conn.execute("PRAGMA busy_timeout").fetchone()[0], expected_ms
                )

    async def test_corrupt_database_fails_closed_without_repair(self):
        corrupt = self.path.parent / "corrupt.db"
        original = b"synthetic invalid sqlite file"
        corrupt.write_bytes(original)
        for target in (self.reader, self.store):
            with self.subTest(target=type(target).__name__):
                with mock.patch.object(target, "path", str(corrupt)):
                    self.assertFalse(target.validate_schema())
                    with self.assertRaisesRegex(
                        self.memory_store.MemoryStoreError, "^storage_unavailable$"
                    ):
                        target.validate_runtime_profile_state()
        self.assertFalse(self.mapping_ready(str(corrupt)))
        self.assertEqual(corrupt.read_bytes(), original)

    async def test_mismatched_profile_is_rejected_without_repinning(self):
        key_id, key_check, normalization, fingerprint = self.reader._expected_profile
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                """INSERT INTO memory_fingerprint_profile
                   (singleton,key_id,key_check,normalization_version,
                    fingerprint_version,created_at,updated_at)
                   VALUES(1,?,?,?,?,?,?)""",
                (key_id + "-wrong", key_check, normalization, fingerprint, "test", "test"),
            )
        before = self.path.read_bytes()
        for target in (self.reader, self.store):
            with self.subTest(target=type(target).__name__):
                with self.assertRaisesRegex(
                    self.memory_store.MemoryStoreError,
                    "^memory_fingerprint_profile_mismatch$",
                ):
                    target.validate_runtime_profile_state()
        self.assertEqual(self.path.read_bytes(), before)

    async def test_readyz_healthy_preserves_contract_and_uses_read_only_sqlite(self):
        before = self.path.read_bytes()
        with (
            mock.patch.object(
                self.module, "_api_loop_ready", new=mock.AsyncMock(return_value=True)
            ),
            self.prove_sqlite_read_only(),
        ):
            response = await request(self.module, "GET", "/readyz")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {"ready", "checks", "status"})
        self.assertIs(payload["ready"], True)
        self.assertEqual(payload["status"], "ready")
        self.assertTrue(all(payload["checks"].values()))
        for name in ("database", "memory_core", "kelivo", "operit_share"):
            self.assertIs(payload["checks"][name], True)
        self.assertEqual(self.path.read_bytes(), before)

    async def test_readyz_missing_database_returns_503_without_creating_it(self):
        absent = self.path.parent / "missing-relay.db"
        with (
            mock.patch.object(self.module, "DB_PATH", str(absent)),
            mock.patch.object(
                self.module, "_api_loop_ready", new=mock.AsyncMock(return_value=True)
            ),
        ):
            response = await request(self.module, "GET", "/readyz")
        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertIs(payload["ready"], False)
        for name in ("database", "memory_core", "kelivo", "operit_share"):
            self.assertIs(payload["checks"][name], False)
        self.assertEqual(payload["errors"]["database"], "core_schema_invalid")
        self.assertFalse(absent.exists())


if __name__ == "__main__":
    unittest.main()
