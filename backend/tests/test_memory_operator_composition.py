from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend import (
    channel_store,
    deployment_config,
    memory_explicit_actions,
    memory_operator_composition,
    memory_policy,
    memory_runtime,
    memory_service,
    memory_store,
)
from backend.tests._support import NoNetworkMixin


TEST_KEY_ID = "operator-preflight-test-key"
TEST_SECRET = "Synthetic-Operator-Preflight-HMAC-Key-2026!Z9q7"
TELEGRAM_DISABLED = SimpleNamespace(requested=False, enabled=False)
BUSINESS_TABLES = (
    "memory_items",
    "memory_fingerprint_profile",
    "memory_evidence_events",
    "memory_sources",
    "memory_suppressions",
    "memory_action_requests",
)


def operator_environment(path: Path, **overrides: str) -> dict[str, str]:
    values = {
        "TELEGRAM_ENABLED": "false",
        "MEMORY_CORE_ENABLED": "true",
        "MEMORY_EXPLICIT_WRITES_ENABLED": "true",
        "MEMORY_EXPLICIT_ENTRY_ENABLED": "true",
        "MEMORY_SENSITIVE_STORAGE_ENABLED": "false",
        "MEMORY_MAX_ITEM_CHARS": "1000",
        "MEMORY_FORGET_RETENTION_POLICY": "tombstone_without_content",
        "MEMORY_FINGERPRINT_KEY_ID": TEST_KEY_ID,
        "MEMORY_FINGERPRINT_HMAC_SECRET": TEST_SECRET,
        "RELAY_DB": str(path),
        "SQLITE_BUSY_TIMEOUT_SECONDS": "5",
    }
    values.update(overrides)
    return values


def initialize_v8(path: Path) -> None:
    with channel_store.connect(str(path)) as conn:
        for statement in channel_store.RELAY_TABLE_DDL.values():
            conn.execute(statement)
    channel_store.run_migrations(str(path))


def insert_matching_profile(path: Path) -> None:
    stamp = channel_store.now_iso()
    with channel_store.connect(str(path)) as conn:
        conn.execute(
            """INSERT INTO memory_fingerprint_profile
               (singleton,key_id,key_check,normalization_version,
                fingerprint_version,created_at,updated_at)
               VALUES(1,?,?,?,?,?,?)""",
            (
                TEST_KEY_ID,
                memory_policy.fingerprint_profile_check(TEST_SECRET),
                memory_policy.NORMALIZATION_VERSION,
                memory_policy.FINGERPRINT_VERSION,
                stamp,
                stamp,
            ),
        )


def database_snapshot(path: Path) -> tuple[str, tuple[tuple[str, int], ...]]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with channel_store.connect_read_only(
        path,
        timeout_seconds=5,
    ) as conn:
        counts = tuple(
            (table, int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]))
            for table in BUSINESS_TABLES
        )
    return digest, counts


class MemoryOperatorCompositionTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "base.sqlite3"
        initialize_v8(self.base)

    def copy_database(self, name: str) -> Path:
        path = self.root / f"{name}.sqlite3"
        shutil.copyfile(self.base, path)
        return path

    def preflight(
        self,
        path: Path,
        **overrides: str,
    ) -> memory_operator_composition.MemoryOperatorPreflightV1:
        return memory_operator_composition.preflight_operator_memory_from_environment(
            TELEGRAM_DISABLED,
            operator_environment(path, **overrides),
        )

    def assert_failure(
        self,
        path: Path,
        category: str,
        *,
        forbidden_values: tuple[str, ...] = (),
        **overrides: str,
    ) -> None:
        before = path.read_bytes() if path.exists() else None
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
            mock.patch.object(
                memory_runtime,
                "bootstrap_memory_runtime",
                side_effect=AssertionError("runtime bootstrap forbidden"),
            ) as bootstrap,
            mock.patch.object(
                memory_store.MemoryStore,
                "__init__",
                side_effect=AssertionError("store construction forbidden"),
            ) as store,
            mock.patch.object(
                memory_store.MemoryReader,
                "__init__",
                side_effect=AssertionError("reader construction forbidden"),
            ) as reader,
            mock.patch.object(
                memory_service.PrivilegedMemoryActions,
                "__init__",
                side_effect=AssertionError("writer construction forbidden"),
            ) as actions,
            mock.patch.object(
                memory_explicit_actions,
                "create_entry_backend",
                side_effect=AssertionError("backend construction forbidden"),
            ) as backend,
            mock.patch.object(
                memory_explicit_actions,
                "bind_operator_cli",
                side_effect=AssertionError("service binding forbidden"),
            ) as binding,
        ):
            result = self.preflight(path, **overrides)
        self.assertFalse(result.ready)
        self.assertEqual(result.category, category)
        self.assertEqual(repr(result), "<MemoryOperatorPreflightV1>")
        self.assertEqual(output.getvalue(), "")
        public_output = result.category + repr(result) + output.getvalue()
        self.assertNotIn(str(path), public_output)
        for value in forbidden_values:
            self.assertNotIn(value, public_output)
        self.assertEqual(path.read_bytes() if path.exists() else None, before)
        bootstrap.assert_not_called()
        store.assert_not_called()
        reader.assert_not_called()
        actions.assert_not_called()
        backend.assert_not_called()
        binding.assert_not_called()

    def test_import_isolated_from_app_fastapi_and_network_modules(self):
        code = textwrap.dedent(
            """
            import json
            import sys
            import backend.memory_operator_composition
            print(json.dumps({
                "app": "backend.app" in sys.modules,
                "fastapi": "fastapi" in sys.modules,
                "kelivo": "backend.kelivo_service" in sys.modules,
                "telegram": "backend.telegram_integration" in sys.modules,
            }, sort_keys=True))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[2],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "app": False,
                "fastapi": False,
                "kelivo": False,
                "telegram": False,
            },
        )
        self.assertEqual(completed.stderr, "")

    def test_contracts_are_frozen_slotted_and_repr_safe(self):
        result = memory_operator_composition.MemoryOperatorPreflightV1(
            ready=False,
            category="memory_storage_missing",
        )
        error = memory_operator_composition.MemoryOperatorCompositionError(
            "memory_storage_missing"
        )
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(result)),
            ("ready", "category"),
        )
        self.assertFalse(hasattr(result, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.ready = True
        self.assertEqual(repr(result), "<MemoryOperatorPreflightV1>")
        self.assertEqual(repr(error), "<MemoryOperatorCompositionError>")
        self.assertEqual(str(error), "memory_storage_missing")
        combined = repr(result) + repr(error) + str(error)
        self.assertNotIn(str(self.root), combined)
        self.assertNotIn(TEST_SECRET, combined)
        self.assertNotIn(TEST_KEY_ID, combined)

    def test_read_only_connection_enforces_query_only_without_file_changes(self):
        path = self.copy_database("readonly")
        before = database_snapshot(path)
        with channel_store.connect_read_only(path, timeout_seconds=5) as conn:
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute(
                    """INSERT INTO messages(ts,direction,kind,text)
                       VALUES('x','in','user','forbidden')"""
                )
        self.assertEqual(database_snapshot(path), before)

    def test_missing_database_is_not_created(self):
        path = self.root / "missing.sqlite3"
        self.assert_failure(path, "memory_storage_missing")
        self.assertFalse(path.exists())

    def test_success_is_repeatable_and_byte_for_byte_read_only(self):
        path = self.copy_database("repeatable")
        before = database_snapshot(path)
        dangerous = (
            (channel_store, "run_migrations"),
            (channel_store, "recover_inflight_generations"),
            (channel_store, "recover_inflight_deliveries"),
            (deployment_config, "prepare_persistent_paths"),
            (deployment_config, "initialize_brain_target"),
        )
        patchers = [
            mock.patch.object(
                module,
                name,
                side_effect=AssertionError(f"{name} forbidden"),
            )
            for module, name in dangerous
        ]
        started = [patcher.start() for patcher in patchers]
        self.addCleanup(lambda: [patcher.stop() for patcher in reversed(patchers)])
        with (
            mock.patch.object(
                memory_runtime,
                "bootstrap_memory_runtime",
                side_effect=AssertionError("runtime bootstrap forbidden"),
            ) as bootstrap,
            mock.patch.object(
                memory_store.MemoryStore,
                "__init__",
                side_effect=AssertionError("store construction forbidden"),
            ) as store,
            mock.patch.object(
                memory_store.MemoryReader,
                "__init__",
                side_effect=AssertionError("reader construction forbidden"),
            ) as reader,
            mock.patch.object(
                memory_service.PrivilegedMemoryActions,
                "__init__",
                side_effect=AssertionError("writer construction forbidden"),
            ) as actions,
            mock.patch.object(
                memory_explicit_actions,
                "create_entry_backend",
                side_effect=AssertionError("backend forbidden"),
            ) as backend,
        ):
            first = self.preflight(path)
            second = self.preflight(path)
        self.assertEqual(first, second)
        self.assertTrue(first.ready)
        self.assertEqual(first.category, "ready")
        self.assertEqual(database_snapshot(path), before)
        for patched in started:
            patched.assert_not_called()
        bootstrap.assert_not_called()
        store.assert_not_called()
        reader.assert_not_called()
        actions.assert_not_called()
        backend.assert_not_called()

    def test_configuration_failure_matrix(self):
        cases = (
            (
                "core-disabled",
                "memory_core_disabled",
                {
                    "MEMORY_CORE_ENABLED": "false",
                    "MEMORY_EXPLICIT_WRITES_ENABLED": "false",
                    "MEMORY_EXPLICIT_ENTRY_ENABLED": "false",
                },
            ),
            (
                "writes-disabled",
                "memory_explicit_writes_disabled",
                {
                    "MEMORY_EXPLICIT_WRITES_ENABLED": "false",
                    "MEMORY_EXPLICIT_ENTRY_ENABLED": "false",
                },
            ),
            (
                "entry-disabled",
                "memory_explicit_entry_disabled",
                {"MEMORY_EXPLICIT_ENTRY_ENABLED": "false"},
            ),
            (
                "missing-secret",
                "memory_fingerprint_hmac_secret_missing",
                {"MEMORY_FINGERPRINT_HMAC_SECRET": ""},
            ),
            (
                "weak-secret",
                "memory_fingerprint_hmac_secret_invalid",
                {"MEMORY_FINGERPRINT_HMAC_SECRET": "weak"},
            ),
            (
                "reused-secret",
                "memory_fingerprint_hmac_secret_must_be_distinct",
                {"RELAY_SECRET": TEST_SECRET},
            ),
            (
                "invalid-key",
                "memory_fingerprint_key_id_invalid",
                {"MEMORY_FINGERPRINT_KEY_ID": "invalid key id"},
            ),
        )
        for name, category, overrides in cases:
            with self.subTest(name=name):
                self.assert_failure(
                    self.copy_database(name),
                    category,
                    **overrides,
                )

    def test_frozen_configuration_invalid_flags_fail_before_storage(self):
        deployment = deployment_config.load_deployment_config(
            TELEGRAM_DISABLED,
            operator_environment(self.root / "not-accessed.sqlite3"),
        )
        cases = (
            (
                "configuration",
                dataclasses.replace(
                    deployment.memory,
                    configuration_valid=False,
                    error_category="memory_configuration_invalid",
                ),
                "memory_configuration_invalid",
            ),
            (
                "entry-configuration",
                dataclasses.replace(
                    deployment.memory,
                    entry_configuration_valid=False,
                    entry_error_category=(
                        "memory_explicit_entry_configuration_invalid"
                    ),
                ),
                "memory_explicit_entry_configuration_invalid",
            ),
        )
        for name, config, category in cases:
            with self.subTest(name=name):
                frozen = dataclasses.replace(deployment, memory=config)
                result = memory_operator_composition._preflight_operator_memory(
                    frozen
                )
                self.assertFalse(result.ready)
                self.assertEqual(result.category, category)
                self.assertFalse((self.root / "not-accessed.sqlite3").exists())

    def test_storage_schema_and_marker_failure_matrix(self):
        corruptions = (
            ("missing-v1", "DELETE FROM schema_migrations WHERE version=1"),
            (
                "wrong-v1-name",
                "UPDATE schema_migrations SET name='wrong' WHERE version=1",
            ),
            (
                "wrong-v6-status",
                "UPDATE schema_migrations SET status='pending' WHERE version=6",
            ),
            ("missing-v7", "DELETE FROM schema_migrations WHERE version=7"),
            (
                "wrong-v7-name",
                "UPDATE schema_migrations SET name='wrong' WHERE version=7",
            ),
            (
                "wrong-v7-status",
                "UPDATE schema_migrations SET status='pending' WHERE version=7",
            ),
            ("missing-v8", "DELETE FROM schema_migrations WHERE version=8"),
            (
                "wrong-v8-name",
                "UPDATE schema_migrations SET name='wrong' WHERE version=8",
            ),
            (
                "wrong-v8-status",
                "UPDATE schema_migrations SET status='pending' WHERE version=8",
            ),
            (
                "missing-v9",
                "DELETE FROM schema_migrations WHERE version=9",
            ),
            (
                "wrong-v9-name",
                "UPDATE schema_migrations SET name='wrong' WHERE version=9",
            ),
            (
                "wrong-v9-status",
                "UPDATE schema_migrations SET status='pending' WHERE version=9",
            ),
            (
                "extra-v10",
                """INSERT INTO schema_migrations
                   VALUES(10,'unknown','applied','x','x')""",
            ),
            ("core-table", "DROP TABLE channel_accounts"),
            ("relay-table", "DROP TABLE push_subscriptions"),
            ("v7-table", "DROP TABLE memory_suppressions"),
            ("v7-index", "DROP INDEX idx_memory_items_live_fingerprint"),
            (
                "v7-fk",
                """PRAGMA writable_schema=ON;
                   UPDATE sqlite_master
                   SET sql=replace(sql,
                       'FOREIGN KEY(memory_id) REFERENCES memory_items(id) ON DELETE RESTRICT',
                       'FOREIGN KEY(memory_id) REFERENCES memory_items(id) ON DELETE CASCADE')
                   WHERE type='table' AND name='memory_sources';
                   PRAGMA writable_schema=OFF""",
            ),
            (
                "v7-trigger",
                "DROP TRIGGER memory_evidence_events_immutable_update",
            ),
            ("v8-table", "DROP TABLE memory_action_requests"),
            (
                "v8-index",
                "DROP INDEX idx_memory_action_requests_status_created",
            ),
            (
                "v8-fk",
                """PRAGMA writable_schema=ON;
                   UPDATE sqlite_master
                   SET sql=replace(sql,
                       'ON DELETE RESTRICT',
                       'ON DELETE CASCADE')
                   WHERE type='table' AND name='memory_action_requests';
                   PRAGMA writable_schema=OFF""",
            ),
            (
                "v8-trigger",
                "DROP TRIGGER memory_action_requests_immutable_update",
            ),
            ("v9-source-table", "DROP TABLE memory_candidate_sources"),
            ("v9-run-table", "DROP TABLE memory_auto_formation_runs"),
            (
                "v9-index",
                "DROP INDEX idx_memory_candidate_sources_canonical",
            ),
            (
                "v9-fk",
                """PRAGMA writable_schema=ON;
                   UPDATE sqlite_master
                   SET sql=replace(sql,
                       'ON DELETE RESTRICT',
                       'ON DELETE CASCADE')
                   WHERE type='table' AND name='memory_auto_formation_runs';
                   PRAGMA writable_schema=OFF""",
            ),
            (
                "v9-trigger",
                "DROP TRIGGER memory_auto_formation_runs_immutable_update",
            ),
        )
        for name, script in corruptions:
            with self.subTest(name=name):
                path = self.copy_database(name)
                with channel_store.connect(str(path)) as conn:
                    conn.executescript(script)
                self.assert_failure(
                    path,
                    "memory_operator_schema_invalid",
                )

        empty = self.root / "empty.sqlite3"
        empty.touch()
        self.assert_failure(empty, "memory_operator_schema_invalid")

        no_markers = self.root / "no-markers.sqlite3"
        with channel_store.connect(str(no_markers)) as conn:
            for statement in channel_store.RELAY_TABLE_DDL.values():
                conn.execute(statement)
        self.assert_failure(no_markers, "memory_operator_schema_invalid")

        duplicate = self.copy_database("duplicate-marker")
        with channel_store.connect(str(duplicate)) as conn:
            conn.executescript(
                """PRAGMA foreign_keys=OFF;
                   ALTER TABLE schema_migrations RENAME TO old_schema_migrations;
                   CREATE TABLE schema_migrations (
                       version INTEGER NOT NULL,
                       name TEXT NOT NULL,
                       status TEXT NOT NULL,
                       created_at TEXT NOT NULL,
                       updated_at TEXT NOT NULL);
                   INSERT INTO schema_migrations
                       SELECT * FROM old_schema_migrations;
                   INSERT INTO schema_migrations
                       SELECT * FROM old_schema_migrations WHERE version=7;
                   DROP TABLE old_schema_migrations;
                   PRAGMA foreign_keys=ON"""
            )
        self.assert_failure(duplicate, "memory_operator_schema_invalid")

    def test_unreviewed_main_schema_object_matrix_is_rejected(self):
        critical_trigger = """
            CREATE TABLE operator_plaintext_copy(value TEXT);
            CREATE TRIGGER copy_operator_message
            AFTER INSERT ON messages
            BEGIN
                INSERT INTO operator_plaintext_copy(value) VALUES(NEW.text);
            END;
        """
        cases = (
            (
                "extra-table",
                "CREATE TABLE operator_extra_table(value TEXT)",
                "DROP TABLE operator_extra_table",
            ),
            (
                "extra-view",
                "CREATE VIEW operator_extra_view AS SELECT id FROM messages",
                "DROP VIEW operator_extra_view",
            ),
            (
                "extra-index",
                "CREATE INDEX operator_extra_index ON messages(ts)",
                "DROP INDEX operator_extra_index",
            ),
            (
                "messages-trigger",
                critical_trigger,
                """DROP TRIGGER copy_operator_message;
                   DROP TABLE operator_plaintext_copy""",
            ),
            (
                "memory-items-trigger",
                """CREATE TRIGGER operator_memory_items_trigger
                   AFTER INSERT ON memory_items BEGIN SELECT 1; END""",
                "DROP TRIGGER operator_memory_items_trigger",
            ),
            (
                "memory-sources-trigger",
                """CREATE TRIGGER operator_memory_sources_trigger
                   AFTER INSERT ON memory_sources BEGIN SELECT 1; END""",
                "DROP TRIGGER operator_memory_sources_trigger",
            ),
            (
                "memory-suppressions-trigger",
                """CREATE TRIGGER operator_memory_suppressions_trigger
                   AFTER INSERT ON memory_suppressions BEGIN SELECT 1; END""",
                "DROP TRIGGER operator_memory_suppressions_trigger",
            ),
            (
                "memory-evidence-third-trigger",
                """CREATE TRIGGER operator_memory_evidence_trigger
                   AFTER INSERT ON memory_evidence_events
                   BEGIN SELECT 1; END""",
                "DROP TRIGGER operator_memory_evidence_trigger",
            ),
            (
                "memory-action-third-trigger",
                """CREATE TRIGGER operator_memory_action_trigger
                   AFTER INSERT ON memory_action_requests
                   BEGIN SELECT 1; END""",
                "DROP TRIGGER operator_memory_action_trigger",
            ),
            (
                "memory-candidate-third-trigger",
                """CREATE TRIGGER operator_memory_candidate_trigger
                   AFTER INSERT ON memory_candidate_sources
                   BEGIN SELECT 1; END""",
                "DROP TRIGGER operator_memory_candidate_trigger",
            ),
            (
                "memory-formation-run-trigger",
                """CREATE TRIGGER operator_memory_formation_run_trigger
                   AFTER INSERT ON memory_auto_formation_runs
                   BEGIN SELECT 1; END""",
                "DROP TRIGGER operator_memory_formation_run_trigger",
            ),
        )
        for name, create_sql, remove_sql in cases:
            with self.subTest(name=name):
                path = self.copy_database(name)
                with channel_store.connect(str(path)) as conn:
                    conn.executescript(create_sql)
                self.assert_failure(
                    path,
                    "memory_operator_schema_invalid",
                    forbidden_values=(
                        "operator_plaintext_copy",
                        "copy_operator_message",
                        "VALUES(NEW.text)",
                    ),
                )
                with channel_store.connect(str(path)) as conn:
                    conn.executescript(remove_sql)
                recovered = self.preflight(path)
                self.assertTrue(recovered.ready)
                self.assertEqual(recovered.category, "ready")

    def test_attached_database_is_rejected_without_leak_and_retry_succeeds(self):
        path = self.copy_database("attached-database")
        attached = self.root / "operator-attachment.sqlite3"
        sqlite3.connect(attached).close()
        real_connect = channel_store.connect_read_only

        @contextlib.contextmanager
        def connect_with_attachment(database, *, timeout_seconds):
            with real_connect(
                database,
                timeout_seconds=timeout_seconds,
            ) as conn:
                conn.execute(
                    "ATTACH DATABASE ? AS operator_attachment",
                    (f"{attached.as_uri()}?mode=ro",),
                )
                yield conn

        with mock.patch.object(
            channel_store,
            "connect_read_only",
            new=connect_with_attachment,
        ):
            self.assert_failure(
                path,
                "memory_operator_schema_invalid",
                forbidden_values=(
                    "operator_attachment",
                    str(attached),
                    attached.as_uri(),
                ),
            )
        recovered = self.preflight(path)
        self.assertTrue(recovered.ready)
        self.assertEqual(recovered.category, "ready")

    def test_profile_failure_matrix_and_empty_profile_success(self):
        empty = self.copy_database("profile-empty")
        self.assertTrue(self.preflight(empty).ready)

        matching = self.copy_database("profile-matching")
        insert_matching_profile(matching)
        self.assertTrue(self.preflight(matching).ready)

        state_without_profile = self.copy_database("profile-state")
        with channel_store.connect(str(state_without_profile)) as conn:
            conn.execute(
                """INSERT INTO memory_suppressions
                   (scope_type,scope_ref,kind,normalized_fingerprint,
                    fingerprint_version,reason_category,created_at)
                   VALUES('global_user','','project',?,1,'user_forget',?)""",
                (b"s" * 32, channel_store.now_iso()),
            )
        self.assert_failure(
            state_without_profile,
            "memory_fingerprint_profile_mismatch",
        )

        ledger_without_profile = self.copy_database("profile-ledger-state")
        with channel_store.connect(str(ledger_without_profile)) as conn:
            stamp = channel_store.now_iso()
            conn.execute(
                """INSERT INTO memory_action_requests
                   (request_id,action_kind,origin,request_binding_digest,
                    status,result_category,created_at,updated_at)
                   VALUES(?,'remember','operator_cli',?,
                          'failed','storage_unavailable',?,?)""",
                (
                    "A" * 32,
                    b"d" * 32,
                    stamp,
                    stamp,
                ),
            )
        self.assert_failure(
            ledger_without_profile,
            "memory_fingerprint_profile_mismatch",
        )

        profile_mutations = (
            (
                "profile-key-id",
                "UPDATE memory_fingerprint_profile SET key_id='other-key'",
            ),
            (
                "profile-key-check",
                "UPDATE memory_fingerprint_profile SET key_check=zeroblob(32)",
            ),
            (
                "profile-normalization",
                "UPDATE memory_fingerprint_profile SET normalization_version=99",
            ),
            (
                "profile-fingerprint",
                "UPDATE memory_fingerprint_profile SET fingerprint_version=99",
            ),
        )
        for name, statement in profile_mutations:
            with self.subTest(name=name):
                path = self.copy_database(name)
                insert_matching_profile(path)
                with channel_store.connect(str(path)) as conn:
                    conn.execute(statement)
                self.assert_failure(
                    path,
                    "memory_fingerprint_profile_mismatch",
                )

        multiple = self.copy_database("profile-multiple")
        insert_matching_profile(multiple)
        with channel_store.connect(str(multiple)) as conn:
            conn.execute("PRAGMA ignore_check_constraints=ON")
            conn.execute(
                """INSERT INTO memory_fingerprint_profile
                   (singleton,key_id,key_check,normalization_version,
                    fingerprint_version,created_at,updated_at)
                   VALUES(2,?,?,?,?,?,?)""",
                (
                    TEST_KEY_ID,
                    memory_policy.fingerprint_profile_check(TEST_SECRET),
                    memory_policy.NORMALIZATION_VERSION,
                    memory_policy.FINGERPRINT_VERSION,
                    channel_store.now_iso(),
                    channel_store.now_iso(),
                ),
            )
        self.assert_failure(
            multiple,
            "memory_fingerprint_profile_mismatch",
        )

    def test_loads_environment_once_and_frozen_bootstraps_are_exact_type(self):
        path = self.copy_database("load-once")
        real_loader = deployment_config.load_deployment_config
        with mock.patch.object(
            deployment_config,
            "load_deployment_config",
            wraps=real_loader,
        ) as loader:
            result = self.preflight(path)
        self.assertTrue(result.ready)
        loader.assert_called_once()
        with self.assertRaisesRegex(
            memory_runtime.MemoryRuntimeError,
            "deployment_config_invalid",
        ):
            memory_runtime.bootstrap_memory_read_service(SimpleNamespace())
        with self.assertRaisesRegex(
            memory_runtime.MemoryRuntimeError,
            "deployment_config_invalid",
        ):
            memory_runtime.bootstrap_memory_runtime(SimpleNamespace())

    def test_compose_in_subprocess_binds_only_operator_after_preflight(self):
        path = self.copy_database("compose")
        before = database_snapshot(path)
        code = textwrap.dedent(
            """
            import json
            import os
            import socket
            from types import SimpleNamespace
            from unittest import mock
            from backend import (
                deployment_config,
                memory_explicit_actions,
                memory_operator_composition,
                memory_runtime,
            )

            def forbidden(*args, **kwargs):
                raise AssertionError("network forbidden")

            socket.socket.connect = forbidden
            counts = {"operator": 0, "mcp": 0, "telegram": 0, "operit": 0}
            real_operator = memory_explicit_actions.bind_operator_cli

            def operator(backend):
                counts["operator"] += 1
                return real_operator(backend)

            def unexpected(name):
                def call(*args, **kwargs):
                    counts[name] += 1
                    raise AssertionError(name + " binding forbidden")
                return call

            telegram = SimpleNamespace(requested=False, enabled=False)
            with (
                mock.patch.object(
                    deployment_config,
                    "load_deployment_config",
                    wraps=deployment_config.load_deployment_config,
                ) as loader,
                mock.patch.object(
                    memory_explicit_actions,
                    "bind_operator_cli",
                    side_effect=operator,
                ),
                mock.patch.object(
                    memory_explicit_actions,
                    "bind_mcp",
                    side_effect=unexpected("mcp"),
                ),
                mock.patch.object(
                    memory_explicit_actions,
                    "bind_telegram",
                    side_effect=unexpected("telegram"),
                ),
                mock.patch.object(
                    memory_explicit_actions,
                    "bind_operit",
                    side_effect=unexpected("operit"),
                ),
            ):
                service = (
                    memory_operator_composition
                    .compose_operator_memory_service_from_environment(
                        telegram,
                        os.environ,
                    )
                )
            print(json.dumps({
                "counts": counts,
                "loader_calls": loader.call_count,
                "service_type": type(service).__name__,
                "service_repr": repr(service),
                "bootstrapped": memory_runtime._PROCESS_BOOTSTRAPPED,
                "app_imported": "backend.app" in __import__("sys").modules,
                "fastapi_imported": "fastapi" in __import__("sys").modules,
            }, sort_keys=True))
            """
        )
        environment = os.environ.copy()
        environment.update(operator_environment(path))
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(
            payload,
            {
                "app_imported": False,
                "bootstrapped": True,
                "counts": {
                    "mcp": 0,
                    "operit": 0,
                    "operator": 1,
                    "telegram": 0,
                },
                "fastapi_imported": False,
                "loader_calls": 1,
                "service_repr": "<ExplicitMemoryActionService>",
                "service_type": "ExplicitMemoryActionService",
            },
        )
        self.assertEqual(completed.stderr, "")
        self.assertEqual(database_snapshot(path), before)
        combined = completed.stdout + completed.stderr
        self.assertNotIn(TEST_SECRET, combined)
        self.assertNotIn(str(path), combined)

    def test_composition_stage_failures_rollback_and_retry_in_subprocess(self):
        code = textwrap.dedent(
            """
            import json
            import os
            from types import SimpleNamespace
            from unittest import mock
            from backend import (
                memory_explicit_actions,
                memory_operator_composition,
                memory_runtime,
                memory_service,
                memory_store,
            )

            stage = os.environ["INJECT_STAGE"]
            injected = {"done": False}
            captured = {"stores": [], "actions": [], "backends": [], "services": []}
            counts = {"operator": 0, "mcp": 0, "telegram": 0, "operit": 0}
            real_token_bytes = memory_runtime.secrets.token_bytes
            real_store_init = memory_store.MemoryStore.__init__
            real_reader_init = memory_store.MemoryReader.__init__
            real_actions_init = memory_service.PrivilegedMemoryActions.__init__
            real_backend = memory_explicit_actions.create_entry_backend
            real_operator = memory_explicit_actions.bind_operator_cli

            def should_fail(name):
                if stage == name and not injected["done"]:
                    injected["done"] = True
                    return True
                return False

            def token_bytes(size):
                if should_fail("action-secret"):
                    raise RuntimeError("raw-action-secret-stage-sentinel")
                return real_token_bytes(size)

            def store_init(self, *args, **kwargs):
                real_store_init(self, *args, **kwargs)
                captured["stores"].append(self)
                if should_fail("store"):
                    raise memory_store.MemoryStoreError(
                        "injected_store_failure"
                    )

            def reader_init(self, *args, **kwargs):
                real_reader_init(self, *args, **kwargs)
                if should_fail("reader"):
                    raise memory_store.MemoryStoreError(
                        "injected_reader_failure"
                    )

            def actions_init(self, *args, **kwargs):
                real_actions_init(self, *args, **kwargs)
                captured["actions"].append(self)
                if should_fail("writer"):
                    raise memory_service.MemoryServiceError(
                        "injected_writer_failure"
                    )

            def backend(actions):
                result = real_backend(actions)
                captured["backends"].append(result)
                if should_fail("backend"):
                    raise memory_explicit_actions.ExplicitMemoryActionError(
                        "injected_backend_failure"
                    )
                return result

            def operator(entry_backend):
                counts["operator"] += 1
                result = real_operator(entry_backend)
                captured["services"].append(result)
                if should_fail("bind"):
                    raise memory_explicit_actions.ExplicitMemoryActionError(
                        "injected_bind_failure"
                    )
                return result

            def unexpected(name):
                def call(*args, **kwargs):
                    counts[name] += 1
                    raise AssertionError(name + " binding forbidden")
                return call

            telegram = SimpleNamespace(requested=False, enabled=False)
            with (
                mock.patch.object(
                    memory_runtime.secrets,
                    "token_bytes",
                    side_effect=token_bytes,
                ),
                mock.patch.object(
                    memory_store.MemoryStore,
                    "__init__",
                    new=store_init,
                ),
                mock.patch.object(
                    memory_store.MemoryReader,
                    "__init__",
                    new=reader_init,
                ),
                mock.patch.object(
                    memory_service.PrivilegedMemoryActions,
                    "__init__",
                    new=actions_init,
                ),
                mock.patch.object(
                    memory_explicit_actions,
                    "create_entry_backend",
                    side_effect=backend,
                ),
                mock.patch.object(
                    memory_explicit_actions,
                    "bind_operator_cli",
                    side_effect=operator,
                ),
                mock.patch.object(
                    memory_explicit_actions,
                    "bind_mcp",
                    side_effect=unexpected("mcp"),
                ),
                mock.patch.object(
                    memory_explicit_actions,
                    "bind_telegram",
                    side_effect=unexpected("telegram"),
                ),
                mock.patch.object(
                    memory_explicit_actions,
                    "bind_operit",
                    side_effect=unexpected("operit"),
                ),
            ):
                try:
                    memory_operator_composition.compose_operator_memory_service_from_environment(
                        telegram,
                        os.environ,
                    )
                except memory_operator_composition.MemoryOperatorCompositionError as error:
                    first_category = error.category
                    first_repr = repr(error)
                else:
                    raise AssertionError("first composition unexpectedly succeeded")

                after_failure = {
                    "authority_none": memory_runtime._PROCESS_AUTHORITY is None,
                    "bootstrapped": memory_runtime._PROCESS_BOOTSTRAPPED,
                }
                invalidated = []
                for store in captured["stores"]:
                    try:
                        store._require_write_runtime()
                    except memory_store.MemoryStoreError as error:
                        invalidated.append(error.category)
                    else:
                        invalidated.append("store_still_usable")
                for actions in captured["actions"]:
                    try:
                        actions._require_enabled()
                    except memory_service.MemoryServiceError as error:
                        invalidated.append(error.category)
                    else:
                        invalidated.append("writer_still_usable")

                service = memory_operator_composition.compose_operator_memory_service_from_environment(
                    telegram,
                    os.environ,
                )

            print(json.dumps({
                "after_failure": after_failure,
                "counts": counts,
                "first_category": first_category,
                "first_repr": first_repr,
                "invalidated": invalidated,
                "retry_repr": repr(service),
                "retry_type": type(service).__name__,
            }, sort_keys=True))
            """
        )
        stages = (
            "action-secret",
            "store",
            "reader",
            "writer",
            "backend",
            "bind",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                path = self.copy_database(f"rollback-{stage}")
                before = database_snapshot(path)
                environment = os.environ.copy()
                environment.update(operator_environment(path))
                environment["INJECT_STAGE"] = stage
                completed = subprocess.run(
                    [sys.executable, "-c", code],
                    cwd=Path(__file__).resolve().parents[2],
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                payload = json.loads(completed.stdout)
                self.assertEqual(
                    payload["after_failure"],
                    {"authority_none": True, "bootstrapped": False},
                )
                self.assertEqual(
                    payload["first_repr"],
                    "<MemoryOperatorCompositionError>",
                )
                self.assertEqual(
                    payload["first_category"],
                    (
                        "memory_operator_composition_failed"
                        if stage == "action-secret"
                        else f"injected_{stage}_failure"
                    ),
                )
                self.assertTrue(
                    all(
                        category == "runtime_authority_invalid"
                        for category in payload["invalidated"]
                    )
                )
                self.assertEqual(
                    payload["counts"]["operator"],
                    2 if stage == "bind" else 1,
                )
                self.assertEqual(payload["counts"]["mcp"], 0)
                self.assertEqual(payload["counts"]["telegram"], 0)
                self.assertEqual(payload["counts"]["operit"], 0)
                self.assertEqual(
                    payload["retry_repr"],
                    "<ExplicitMemoryActionService>",
                )
                self.assertEqual(
                    payload["retry_type"],
                    "ExplicitMemoryActionService",
                )
                self.assertEqual(completed.stderr, "")
                self.assertEqual(database_snapshot(path), before)
                public_output = completed.stdout + completed.stderr
                self.assertNotIn("raw-action-secret-stage-sentinel", public_output)
                self.assertNotIn(TEST_SECRET, public_output)
                self.assertNotIn(str(path), public_output)
                self.assertNotRegex(public_output, r"0x[0-9a-fA-F]+")

    def test_base_exception_cleans_pending_runtime_without_translation(self):
        path = self.copy_database("base-exception-cleanup")
        before = database_snapshot(path)
        code = textwrap.dedent(
            """
            import json
            import os
            from types import SimpleNamespace
            from unittest import mock
            from backend import (
                memory_explicit_actions,
                memory_operator_composition,
                memory_runtime,
            )

            real_operator = memory_explicit_actions.bind_operator_cli
            injected = {"done": False}

            def operator(backend):
                if not injected["done"]:
                    injected["done"] = True
                    raise KeyboardInterrupt("raw-base-exception-sentinel")
                return real_operator(backend)

            telegram = SimpleNamespace(requested=False, enabled=False)
            with mock.patch.object(
                memory_explicit_actions,
                "bind_operator_cli",
                side_effect=operator,
            ):
                try:
                    memory_operator_composition.compose_operator_memory_service_from_environment(
                        telegram,
                        os.environ,
                    )
                except KeyboardInterrupt:
                    after_failure = {
                        "authority_none": memory_runtime._PROCESS_AUTHORITY is None,
                        "bootstrapped": memory_runtime._PROCESS_BOOTSTRAPPED,
                    }
                else:
                    raise AssertionError("KeyboardInterrupt was translated")
                service = memory_operator_composition.compose_operator_memory_service_from_environment(
                    telegram,
                    os.environ,
                )
            print(json.dumps({
                "after_failure": after_failure,
                "retry_repr": repr(service),
            }, sort_keys=True))
            """
        )
        environment = os.environ.copy()
        environment.update(operator_environment(path))
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "after_failure": {
                    "authority_none": True,
                    "bootstrapped": False,
                },
                "retry_repr": "<ExplicitMemoryActionService>",
            },
        )
        self.assertEqual(completed.stderr, "")
        self.assertEqual(database_snapshot(path), before)
        self.assertNotIn("raw-base-exception-sentinel", completed.stdout)

    def test_concurrent_composition_publishes_only_one_runtime(self):
        path = self.copy_database("concurrent-publish")
        before = database_snapshot(path)
        code = textwrap.dedent(
            """
            import json
            import os
            import threading
            from types import SimpleNamespace
            from backend import memory_operator_composition

            barrier = threading.Barrier(2)
            results = []
            result_lock = threading.Lock()
            telegram = SimpleNamespace(requested=False, enabled=False)

            def worker():
                barrier.wait(timeout=10)
                try:
                    service = memory_operator_composition.compose_operator_memory_service_from_environment(
                        telegram,
                        os.environ,
                    )
                except memory_operator_composition.MemoryOperatorCompositionError as error:
                    result = error.category
                else:
                    result = repr(service)
                with result_lock:
                    results.append(result)

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            print(json.dumps({
                "alive": [thread.is_alive() for thread in threads],
                "results": sorted(results),
            }, sort_keys=True))
            """
        )
        environment = os.environ.copy()
        environment.update(operator_environment(path))
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "alive": [False, False],
                "results": [
                    "<ExplicitMemoryActionService>",
                    "memory_runtime_already_initialized",
                ],
            },
        )
        self.assertEqual(completed.stderr, "")
        self.assertEqual(database_snapshot(path), before)

    def test_waiter_succeeds_after_pending_backend_failure(self):
        path = self.copy_database("concurrent-rollback")
        before = database_snapshot(path)
        code = textwrap.dedent(
            """
            import json
            import os
            import threading
            from types import SimpleNamespace
            from unittest import mock
            from backend import (
                memory_explicit_actions,
                memory_operator_composition,
            )

            entered = threading.Event()
            release = threading.Event()
            call_lock = threading.Lock()
            call_count = 0
            results = []
            result_lock = threading.Lock()
            real_backend = memory_explicit_actions.create_entry_backend
            telegram = SimpleNamespace(requested=False, enabled=False)

            def backend(actions):
                global call_count
                with call_lock:
                    call_count += 1
                    current = call_count
                if current == 1:
                    entered.set()
                    if not release.wait(timeout=10):
                        raise AssertionError("release timeout")
                    raise memory_explicit_actions.ExplicitMemoryActionError(
                        "injected_backend_failure"
                    )
                return real_backend(actions)

            def worker():
                try:
                    service = memory_operator_composition.compose_operator_memory_service_from_environment(
                        telegram,
                        os.environ,
                    )
                except memory_operator_composition.MemoryOperatorCompositionError as error:
                    result = error.category
                else:
                    result = repr(service)
                with result_lock:
                    results.append(result)

            with mock.patch.object(
                memory_explicit_actions,
                "create_entry_backend",
                side_effect=backend,
            ):
                first = threading.Thread(target=worker)
                first.start()
                if not entered.wait(timeout=10):
                    raise AssertionError("backend was not reached")
                second = threading.Thread(target=worker)
                second.start()
                release.set()
                first.join(timeout=10)
                second.join(timeout=10)
            print(json.dumps({
                "alive": [first.is_alive(), second.is_alive()],
                "calls": call_count,
                "results": sorted(results),
            }, sort_keys=True))
            """
        )
        environment = os.environ.copy()
        environment.update(operator_environment(path))
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "alive": [False, False],
                "calls": 2,
                "results": [
                    "<ExplicitMemoryActionService>",
                    "injected_backend_failure",
                ],
            },
        )
        self.assertEqual(completed.stderr, "")
        self.assertEqual(database_snapshot(path), before)

    def test_failed_compose_never_bootstraps_runtime_in_subprocess(self):
        missing = self.root / "compose-missing.sqlite3"
        code = textwrap.dedent(
            """
            import json
            import os
            from types import SimpleNamespace
            from unittest import mock
            from backend import memory_operator_composition, memory_runtime

            with mock.patch.object(
                memory_runtime,
                "bootstrap_memory_runtime",
                side_effect=AssertionError("runtime bootstrap forbidden"),
            ) as bootstrap:
                try:
                    (
                        memory_operator_composition
                        .compose_operator_memory_service_from_environment(
                            SimpleNamespace(requested=False, enabled=False),
                            os.environ,
                        )
                    )
                except memory_operator_composition.MemoryOperatorCompositionError as error:
                    payload = {
                        "category": error.category,
                        "repr": repr(error),
                        "bootstrap_calls": bootstrap.call_count,
                        "bootstrapped": memory_runtime._PROCESS_BOOTSTRAPPED,
                    }
                else:
                    raise AssertionError("composition unexpectedly succeeded")
            print(json.dumps(payload, sort_keys=True))
            """
        )
        environment = os.environ.copy()
        environment.update(operator_environment(missing))
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "bootstrap_calls": 0,
                "bootstrapped": False,
                "category": "memory_storage_missing",
                "repr": "<MemoryOperatorCompositionError>",
            },
        )
        self.assertFalse(missing.exists())
        self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main()
