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
                "extra-v9",
                """INSERT INTO schema_migrations
                   VALUES(9,'unknown','applied','x','x')""",
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
            conn.execute(
                """INSERT INTO memory_action_requests
                   (request_id,action_kind,origin,request_binding_digest,
                    status,result_category,created_at,updated_at)
                   VALUES(?,'remember','operator_cli',?,
                          'failed','storage_unavailable',?,?)""",
                (
                    "A" * 32,
                    b"d" * 32,
                    channel_store.now_iso(),
                    channel_store.now_iso(),
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
