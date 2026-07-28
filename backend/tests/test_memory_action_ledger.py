from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, dataclass, fields, replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend import (
    channel_store,
    memory_action_ledger,
    memory_store,
)
from backend.tests.test_memory_service import (
    TEST_HMAC_SECRET,
    bootstrap_runtime,
    memory_config,
)


class MemoryActionLedgerSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "ledger.sqlite3")
        with channel_store.connect(self.path) as conn:
            conn.execute(channel_store.RELAY_TABLE_DDL["messages"])
        channel_store.run_migrations(self.path)

    @staticmethod
    def request_id(marker: str = "A") -> str:
        return marker * 32

    @staticmethod
    def memory_key(marker: str = "M") -> str:
        return marker * 32

    def canonical(self) -> int:
        stamp = channel_store.now_iso()
        with channel_store.connect(self.path) as conn:
            cursor = conn.execute(
                """INSERT INTO messages(ts,direction,kind,text,meta)
                   VALUES(?,'in','user','synthetic ledger action',
                          '{"channel":"web","source":"relay"}')""",
                (stamp,),
            )
            return int(cursor.lastrowid)

    def insert_row(
        self,
        *,
        request_id: str,
        action_kind: str,
        target_memory_key: str | None,
        canonical_message_id: int | None,
        result_memory_key: str | None,
        status: str,
        result_category: str,
        digest: object = b"d" * 32,
        origin: str = "operator_cli",
    ) -> None:
        stamp = channel_store.now_iso()
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO memory_action_requests
                   (request_id,action_kind,origin,request_binding_digest,
                    target_memory_key,canonical_message_id,result_memory_key,
                    status,result_category,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    request_id,
                    action_kind,
                    origin,
                    digest,
                    target_memory_key,
                    canonical_message_id,
                    result_memory_key,
                    status,
                    result_category,
                    stamp,
                    stamp,
                ),
            )

    def test_valid_terminal_combinations(self):
        cases = (
            ("remember", None, "created", self.memory_key("A")),
            ("remember", None, "idempotent_existing", self.memory_key("B")),
            ("remember", None, "suppressed", None),
            ("correct", self.memory_key("C"), "corrected", self.memory_key("D")),
            ("correct", self.memory_key("E"), "unchanged", self.memory_key("E")),
            ("correct", self.memory_key("F"), "suppressed", None),
            ("forget", self.memory_key("G"), "forgotten", self.memory_key("G")),
            (
                "forget",
                self.memory_key("H"),
                "already_forgotten",
                self.memory_key("H"),
            ),
        )
        for index, (kind, target, category, result_key) in enumerate(cases):
            with self.subTest(kind=kind, category=category):
                self.insert_row(
                    request_id=self.request_id(chr(65 + index)),
                    action_kind=kind,
                    target_memory_key=target,
                    canonical_message_id=self.canonical(),
                    result_memory_key=result_key,
                    status="completed",
                    result_category=category,
                )
        self.insert_row(
            request_id=self.request_id("Z"),
            action_kind="remember",
            target_memory_key=None,
            canonical_message_id=None,
            result_memory_key=None,
            status="failed",
            result_category="invalid_content",
        )
        with channel_store.connect(self.path) as conn:
            channel_store.validate_memory_action_schema(conn)
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM memory_action_requests"
                ).fetchone()[0],
                len(cases) + 1,
            )

    def test_invalid_action_target_status_and_result_combinations(self):
        cases = (
            {
                "action_kind": "unknown",
                "target_memory_key": None,
                "canonical_message_id": self.canonical(),
                "result_memory_key": self.memory_key(),
                "status": "completed",
                "result_category": "created",
            },
            {
                "action_kind": "remember",
                "target_memory_key": self.memory_key(),
                "canonical_message_id": self.canonical(),
                "result_memory_key": self.memory_key(),
                "status": "completed",
                "result_category": "created",
            },
            {
                "action_kind": "correct",
                "target_memory_key": None,
                "canonical_message_id": self.canonical(),
                "result_memory_key": self.memory_key(),
                "status": "completed",
                "result_category": "corrected",
            },
            {
                "action_kind": "forget",
                "target_memory_key": self.memory_key("A"),
                "canonical_message_id": self.canonical(),
                "result_memory_key": self.memory_key("B"),
                "status": "completed",
                "result_category": "forgotten",
            },
            {
                "action_kind": "remember",
                "target_memory_key": None,
                "canonical_message_id": None,
                "result_memory_key": self.memory_key(),
                "status": "completed",
                "result_category": "created",
            },
            {
                "action_kind": "remember",
                "target_memory_key": None,
                "canonical_message_id": self.canonical(),
                "result_memory_key": None,
                "status": "failed",
                "result_category": "invalid_content",
            },
            {
                "action_kind": "remember",
                "target_memory_key": None,
                "canonical_message_id": None,
                "result_memory_key": None,
                "status": "pending",
                "result_category": "invalid_content",
            },
        )
        for index, values in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(
                sqlite3.IntegrityError
            ):
                self.insert_row(
                    request_id=self.request_id(chr(65 + index)),
                    **values,
                )

    def test_duplicate_ids_canonical_ids_and_malformed_digest_are_rejected(self):
        canonical_id = self.canonical()
        self.insert_row(
            request_id=self.request_id("A"),
            action_kind="remember",
            target_memory_key=None,
            canonical_message_id=canonical_id,
            result_memory_key=self.memory_key("A"),
            status="completed",
            result_category="created",
        )
        for request_id, other_canonical in (
            (self.request_id("A"), self.canonical()),
            (self.request_id("B"), canonical_id),
        ):
            with self.subTest(request_id=request_id), self.assertRaises(
                sqlite3.IntegrityError
            ):
                self.insert_row(
                    request_id=request_id,
                    action_kind="remember",
                    target_memory_key=None,
                    canonical_message_id=other_canonical,
                    result_memory_key=self.memory_key("B"),
                    status="completed",
                    result_category="created",
                )
        for index, digest in enumerate((b"", b"x" * 31, b"x" * 33, "x" * 32)):
            with self.subTest(digest=index), self.assertRaises(
                sqlite3.IntegrityError
            ):
                self.insert_row(
                    request_id=self.request_id(chr(70 + index)),
                    action_kind="remember",
                    target_memory_key=None,
                    canonical_message_id=self.canonical(),
                    result_memory_key=self.memory_key(chr(70 + index)),
                    status="completed",
                    result_category="created",
                    digest=digest,
                )

    def test_terminal_rows_are_insert_once_and_immutable(self):
        request_id = self.request_id("I")
        self.insert_row(
            request_id=request_id,
            action_kind="remember",
            target_memory_key=None,
            canonical_message_id=self.canonical(),
            result_memory_key=self.memory_key("I"),
            status="completed",
            result_category="created",
        )
        with channel_store.connect(self.path) as conn:
            for statement in (
                """UPDATE memory_action_requests SET result_category='suppressed'
                   WHERE request_id=?""",
                "DELETE FROM memory_action_requests WHERE request_id=?",
            ):
                with self.subTest(statement=statement), self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "memory_action_request_immutable",
                ):
                    conn.execute(statement, (request_id,))
            row = conn.execute(
                """SELECT status,result_category
                   FROM memory_action_requests WHERE request_id=?""",
                (request_id,),
            ).fetchone()
        self.assertEqual(tuple(row), ("completed", "created"))

    def test_schema_has_no_content_identity_secret_or_capability_fields(self):
        with channel_store.connect(self.path) as conn:
            names = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_xinfo(memory_action_requests)"
                )
            }
        forbidden = {
            "content",
            "normalized_content",
            "canonical_text",
            "fingerprint",
            "external_user_id",
            "device_id",
            "session_id",
            "metadata",
            "capability",
            "signature",
            "authority",
            "secret",
            "key_id",
            "sql",
            "error_body",
        }
        self.assertTrue(names.isdisjoint(forbidden))


class MemoryActionMigrationValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def path(self, name: str) -> str:
        return str(Path(self.temp.name) / f"{name}.sqlite3")

    def prepare_v7(self, name: str) -> str:
        path = self.path(name)
        with channel_store.connect(path) as conn:
            conn.execute(channel_store.RELAY_TABLE_DDL["messages"])
        channel_store.run_migrations(path, channel_store.MIGRATIONS[:7])
        return path

    def install_v8_objects(
        self,
        path: str,
        *,
        table_sql: str | None = None,
        index_sql: str | None = None,
        trigger_sql: tuple[str, ...] | None = None,
        marker_name: str = "explicit_memory_action_request_ledger",
        extra_sql: str | None = None,
    ) -> None:
        with channel_store.connect(path) as conn:
            conn.execute(table_sql or channel_store.MEMORY_ACTION_REQUEST_TABLE_DDL)
            conn.execute(
                index_sql
                or channel_store.MEMORY_ACTION_REQUEST_INDEX_DDL[
                    "idx_memory_action_requests_status_created"
                ]
            )
            for statement in (
                trigger_sql
                if trigger_sql is not None
                else tuple(
                    channel_store.MEMORY_ACTION_REQUEST_TRIGGER_DDL.values()
                )
            ):
                conn.execute(statement)
            if extra_sql:
                conn.execute(extra_sql)
            stamp = channel_store.now_iso()
            conn.execute(
                """INSERT INTO schema_migrations
                   (version,name,status,created_at,updated_at)
                   VALUES(8,?,'applied',?,?)""",
                (marker_name, stamp, stamp),
            )

    def test_clean_repeated_concurrent_v7_to_v8_preserves_v1_v7_data(self):
        path = self.prepare_v7("preserve")
        stamp = channel_store.now_iso()
        with channel_store.connect(path) as conn:
            conn.execute(
                """INSERT INTO channel_accounts
                   (channel,external_account_id,status,created_at,updated_at)
                   VALUES('telegram','v8-preserved','active',?,?)""",
                (stamp, stamp),
            )
            message = conn.execute(
                """INSERT INTO messages(ts,direction,kind,text,meta)
                   VALUES(?,'in','user','preserved synthetic',
                          '{"channel":"web","source":"relay"}')""",
                (stamp,),
            )
            conn.execute(
                """INSERT INTO memory_fingerprint_profile
                   (singleton,key_id,key_check,normalization_version,
                    fingerprint_version,created_at,updated_at)
                   VALUES(1,'v8-preserved-key',?,1,1,?,?)""",
                (b"k" * 32, stamp, stamp),
            )
            item = conn.execute(
                """INSERT INTO memory_items
                   (memory_key,kind,scope_type,scope_ref,normalized_content,
                    normalized_fingerprint,fingerprint_version,status,
                    explicitness,confidence,sensitivity,first_observed_at,
                    last_confirmed_at,superseded_by_id,created_at,updated_at)
                   VALUES(?,'project','global_user','','preserved memory',
                          ?,1,'active','explicit',1.0,'normal',?,?,NULL,?,?)""",
                ("M" * 32, b"f" * 32, stamp, stamp, stamp, stamp),
            )
            evidence = conn.execute(
                """INSERT INTO memory_evidence_events
                   (canonical_message_id,action_id,action_type,
                    action_binding_version,evidence_type,reality_scope,
                    subject_scope,created_by_component,created_at)
                   VALUES(? ,?,'remember_explicit_user',1,
                          'explicit_user_memory','real','user',
                          'memory_admin',?)""",
                (int(message.lastrowid), "A" * 32, stamp),
            )
            conn.execute(
                """INSERT INTO memory_sources
                   (memory_id,evidence_event_id,canonical_message_id,channel,
                    source,evidence_role,evidence_type,created_at)
                   VALUES(?,?,?,'web','relay','user',
                          'explicit_user_memory',?)""",
                (
                    int(item.lastrowid),
                    int(evidence.lastrowid),
                    int(message.lastrowid),
                    stamp,
                ),
            )
            conn.execute(
                """INSERT INTO memory_suppressions
                   (scope_type,scope_ref,kind,normalized_fingerprint,
                    fingerprint_version,reason_category,created_at)
                   VALUES('global_user','','project',?,1,
                          'privacy_policy',?)""",
                (b"s" * 32, stamp),
            )
            before = {
                table: [
                    tuple(row)
                    for row in conn.execute(f"SELECT * FROM {table}")
                ]
                for table in (
                    "channel_accounts",
                    "messages",
                    "memory_items",
                    "memory_fingerprint_profile",
                    "memory_evidence_events",
                    "memory_sources",
                    "memory_suppressions",
                )
            }
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(
                lambda _index: channel_store.run_migrations(path),
                range(16),
            ))
        channel_store.run_migrations(path)
        with channel_store.connect(path) as conn:
            after = {
                table: [
                    tuple(row)
                    for row in conn.execute(f"SELECT * FROM {table}")
                ]
                for table in before
            }
            channel_store.validate_memory_action_schema(conn)
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM schema_migrations WHERE version=8"
                ).fetchone()[0],
                1,
            )
        self.assertEqual(after, before)

    def test_validator_rejects_column_check_fk_index_marker_and_extra_object(self):
        ddl = channel_store.MEMORY_ACTION_REQUEST_TABLE_DDL
        cases = (
            (
                "missing-column",
                ddl.replace(
                    """        origin TEXT NOT NULL
            CHECK(origin IN ('operator_cli','mcp','telegram','operit')),
""",
                    "",
                ),
                None,
                None,
                None,
            ),
            (
                "extra-column",
                ddl.replace(
                    "updated_at TEXT NOT NULL,",
                    "updated_at TEXT NOT NULL, extra_column TEXT,",
                ),
                None,
                None,
                None,
            ),
            (
                "changed-check",
                ddl.replace(
                    "('remember','correct','forget')",
                    "('remember','correct','forget','generic')",
                ),
                None,
                None,
                None,
            ),
            (
                "bad-fk",
                ddl.replace(
                    "REFERENCES messages(id) ON DELETE RESTRICT",
                    "REFERENCES messages(id) ON DELETE CASCADE",
                ),
                None,
                None,
                None,
            ),
            (
                "bad-index",
                None,
                """CREATE INDEX idx_memory_action_requests_status_created
                   ON memory_action_requests(created_at,status,request_id)""",
                None,
                None,
            ),
            (
                "bad-unique",
                ddl.replace(
                    "canonical_message_id INTEGER UNIQUE,",
                    "canonical_message_id INTEGER,",
                ),
                None,
                None,
                None,
            ),
            (
                "bad-marker",
                None,
                None,
                "wrong_name",
                None,
            ),
            (
                "extra-object",
                None,
                None,
                None,
                "CREATE TABLE memory_action_extra(id INTEGER)",
            ),
        )
        for name, table_sql, index_sql, marker, extra_sql in cases:
            with self.subTest(name=name):
                path = self.prepare_v7(name)
                self.install_v8_objects(
                    path,
                    table_sql=table_sql,
                    index_sql=index_sql,
                    marker_name=marker or "explicit_memory_action_request_ledger",
                    extra_sql=extra_sql,
                )
                with channel_store.connect(path) as conn, self.assertRaisesRegex(
                    sqlite3.DatabaseError,
                    "memory action",
                ):
                    channel_store.validate_memory_action_schema(conn)

    def test_validator_requires_exact_immutable_trigger_set(self):
        update_sql = channel_store.MEMORY_ACTION_REQUEST_TRIGGER_DDL[
            "memory_action_requests_immutable_update"
        ]
        delete_sql = channel_store.MEMORY_ACTION_REQUEST_TRIGGER_DDL[
            "memory_action_requests_immutable_delete"
        ]
        cases = (
            ("missing", (update_sql,), None),
            (
                "modified",
                (
                    update_sql,
                    delete_sql.replace(
                        "memory_action_request_immutable",
                        "changed_error",
                    ),
                ),
                None,
            ),
            (
                "extra",
                (update_sql, delete_sql),
                """CREATE TRIGGER memory_action_requests_extra
                   BEFORE INSERT ON memory_action_requests
                   BEGIN
                     SELECT RAISE(ABORT,'extra');
                   END""",
            ),
        )
        for name, triggers, extra in cases:
            with self.subTest(name=name):
                path = self.prepare_v7(f"trigger-{name}")
                self.install_v8_objects(
                    path,
                    trigger_sql=triggers,
                    extra_sql=extra,
                )
                with channel_store.connect(path) as conn, self.assertRaisesRegex(
                    sqlite3.DatabaseError,
                    "memory action",
                ):
                    channel_store.validate_memory_action_schema(conn)

    def test_failed_v8_rolls_back_object_and_marker(self):
        path = self.prepare_v7("rollback")

        def broken(conn):
            channel_store._migration_008(conn)
            raise RuntimeError("injected")

        migrations = (
            *channel_store.MIGRATIONS[:7],
            (8, "explicit_memory_action_request_ledger", broken),
        )
        with self.assertRaisesRegex(RuntimeError, "injected"):
            channel_store.run_migrations(path, migrations)
        with channel_store.connect(path) as conn:
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=8"
                ).fetchone()
            )
            self.assertIsNone(
                conn.execute(
                    """SELECT 1 FROM sqlite_master
                       WHERE name='memory_action_requests'"""
                ).fetchone()
            )

    def test_v7_code_path_ignores_additive_v8_table(self):
        path = self.prepare_v7("old-code")
        channel_store.run_migrations(path)
        channel_store.run_migrations(path, channel_store.MIGRATIONS[:7])
        with channel_store.connect(path) as conn:
            channel_store.validate_memory_schema(conn)
            self.assertIsNotNone(
                conn.execute(
                    """SELECT 1 FROM sqlite_master
                       WHERE type='table' AND name='memory_action_requests'"""
                ).fetchone()
            )


class MemoryActionBindingTests(unittest.TestCase):
    def binding(self, **updates):
        values = {
            "request_id": "R" * 32,
            "action_kind": "remember",
            "origin": "operator_cli",
            "target_memory_key": None,
            "scope_type": "global_user",
            "scope_ref": "",
            "kind": "project",
            "sensitivity": "normal",
            "normalized_content": "Synthetic request binding",
        }
        values.update(updates)
        return memory_action_ledger.MemoryActionRequestBinding(**values)

    def test_digest_is_domain_separated_hmac_and_repr_is_data_free(self):
        binding = self.binding()
        digest = memory_action_ledger.request_binding_digest(
            TEST_HMAC_SECRET, binding,
        )
        self.assertEqual(len(digest), 32)
        self.assertNotEqual(
            digest,
            hashlib.sha256(binding.normalized_content.encode("utf-8")).digest(),
        )
        self.assertEqual(repr(binding), "<MemoryActionRequestBinding>")
        self.assertNotIn(binding.request_id, repr(binding))
        self.assertNotIn(binding.normalized_content, repr(binding))

    def test_every_business_field_changes_digest(self):
        baseline = memory_action_ledger.request_binding_digest(
            TEST_HMAC_SECRET, self.binding(),
        )
        cases = (
            {"request_id": "S" * 32},
            {"origin": "mcp"},
            {"scope_type": "project", "scope_ref": "synthetic"},
            {"kind": "decision"},
            {"sensitivity": "sensitive"},
            {"normalized_content": "Different synthetic binding"},
            {
                "action_kind": "correct",
                "target_memory_key": "M" * 32,
            },
        )
        for updates in cases:
            with self.subTest(updates=tuple(updates)):
                self.assertNotEqual(
                    memory_action_ledger.request_binding_digest(
                        TEST_HMAC_SECRET,
                        self.binding(**updates),
                    ),
                    baseline,
                )

    def test_invalid_types_shapes_and_plain_or_weak_secrets_fail_closed(self):
        cases = (
            self.binding(request_id="short"),
            self.binding(action_kind="generic"),
            self.binding(origin="http"),
            self.binding(target_memory_key="M" * 32),
            self.binding(scope_type="session", scope_ref=""),
            self.binding(kind="assistant_experience"),
            self.binding(normalized_content=" not normalized "),
            self.binding(normalization_version=True),
            self.binding(canonical_action_contract_version=2),
        )
        for binding in cases:
            with self.subTest(binding=binding), self.assertRaisesRegex(
                memory_action_ledger.MemoryActionLedgerError,
                "invalid_request",
            ):
                memory_action_ledger.request_binding_digest(
                    TEST_HMAC_SECRET,
                    binding,
                )
        for secret in (
            "",
            "short",
            "x" * 31,
            "x" * 32,
            "replace-with-example-secret-123!ABC",
            "é" * 32,
        ):
            with self.subTest(secret_length=len(secret)), self.assertRaisesRegex(
                memory_action_ledger.MemoryActionLedgerError,
                "memory_configuration_invalid",
            ):
                memory_action_ledger.request_binding_digest(
                    secret,
                    self.binding(),
                )


class _CommitFailureConnection:
    def __init__(self, connection, *, after_commit: bool):
        self._connection = connection
        self._after_commit = after_commit

    @property
    def in_transaction(self):
        return self._connection.in_transaction

    def execute(self, sql, parameters=()):
        if " ".join(str(sql).strip().upper().split()) == "COMMIT":
            if self._after_commit:
                self._connection.execute(sql, parameters)
            raise sqlite3.OperationalError("synthetic_commit_failure")
        return self._connection.execute(sql, parameters)

    def close(self):
        self._connection.close()

    def __getattr__(self, name):
        return getattr(self._connection, name)


class MemoryActionUnitOfWorkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "uow.sqlite3")
        self._prepare_path(self.path)
        self.runtime = bootstrap_runtime(self.path, memory_config())
        self.actions = self.runtime.privileged_actions
        self.store = self.actions._store
        self.authority = self.actions._authority

    @staticmethod
    def _prepare_path(path: str):
        with channel_store.connect(path) as conn:
            conn.execute(channel_store.RELAY_TABLE_DDL["messages"])
        channel_store.run_migrations(path)

    @staticmethod
    def binding(request_id: str = "R" * 32, *, content: str = "Synthetic UoW memory"):
        return memory_action_ledger.MemoryActionRequestBinding(
            request_id=request_id,
            action_kind="remember",
            origin="operator_cli",
            scope_type="global_user",
            scope_ref="",
            kind="project",
            sensitivity="normal",
            normalized_content=content,
        )

    def counts(self, path: str | None = None) -> dict[str, int]:
        with channel_store.connect(path or self.path) as conn:
            return {
                table: int(
                    conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                )
                for table in (
                    "messages",
                    "memory_action_requests",
                    "memory_fingerprint_profile",
                    "memory_evidence_events",
                    "memory_items",
                    "memory_sources",
                    "memory_suppressions",
                )
            }

    def execute_remember(
        self,
        binding: memory_action_ledger.MemoryActionRequestBinding,
        *,
        spoof_outcome: str | None = None,
    ):
        with self.store._action_unit_of_work() as uow:
            replay = uow.claim_request(binding)
            if replay is not None:
                return uow.commit(), True
            terminal = self.stage_remember(
                uow,
                binding,
                store=self.store,
                authority=self.authority,
                spoof_outcome=spoof_outcome,
            )
            self.assertEqual(terminal.status, "completed")
            return uow.commit(), False

    @staticmethod
    def stage_remember(
        uow,
        binding,
        *,
        store,
        authority,
        spoof_outcome: str | None = None,
    ):
        MemoryActionUnitOfWorkTests.stage_remember_store_action(
            uow,
            binding,
            store=store,
            authority=authority,
        )
        if spoof_outcome is not None:
            spoofed_semantics = replace(
                uow._store_outcome.semantics,
                store_outcome=spoof_outcome,
            )
            uow._store_outcome_semantics = spoofed_semantics
            uow._store_outcome = replace(
                uow._store_outcome,
                semantics=spoofed_semantics,
            )
        return uow.complete_request()

    @staticmethod
    def stage_remember_store_action(
        uow,
        binding,
        *,
        store,
        authority,
    ):
        runtime_module = importlib.import_module(type(authority).__module__)
        store_module = importlib.import_module(type(store).__module__)
        canonical_id = uow._insert_canonical_action(
            text=binding.normalized_content,
            metadata={"channel": "web", "source": "relay"},
        )
        envelope = runtime_module.issue_action_envelope(
            authority,
            runtime_module.MemoryActionBinding(
                action_type=runtime_module.ACTION_REMEMBER_USER,
                canonical_message_id=canonical_id,
                kind=binding.kind,
                scope_type=binding.scope_type,
                scope_ref=binding.scope_ref,
                normalized_content=binding.normalized_content,
                sensitivity=binding.sensitivity,
            ),
        )
        result = store.create_explicit_memory_from_user_action(
            kind=binding.kind,
            scope_type=binding.scope_type,
            scope_ref=binding.scope_ref,
            content=binding.normalized_content,
            sensitivity=binding.sensitivity,
            sources=[
                store_module.memory_policy.ProvenanceInput(
                    canonical_message_id=canonical_id
                )
            ],
            authorization=envelope,
            _transaction=uow,
        )
        return result, envelope

    def completed_remember_case(self, marker: str):
        path = str(Path(self.temp.name) / f"tamper-{marker}.sqlite3")
        self._prepare_path(path)
        runtime = bootstrap_runtime(path, memory_config())
        actions = runtime.privileged_actions
        store = actions._store
        authority = actions._authority
        binding = self.binding(
            request_id=marker[0].upper() * 32,
            content=f"Synthetic {marker} memory",
        )
        with store._action_unit_of_work() as uow:
            self.assertIsNone(uow.claim_request(binding))
            terminal = self.stage_remember(
                uow,
                binding,
                store=store,
                authority=authority,
            )
            result = uow.commit()
        self.assertEqual(result, terminal)
        return path, store, authority, binding, result

    @staticmethod
    def execute_remember_for_store(
        store,
        authority,
        binding: memory_action_ledger.MemoryActionRequestBinding,
        *,
        spoof_outcome: str | None = None,
    ):
        with store._action_unit_of_work() as uow:
            replay = uow.claim_request(binding)
            if replay is not None:
                return uow.commit(), True
            MemoryActionUnitOfWorkTests.stage_remember(
                uow,
                binding,
                store=store,
                authority=authority,
                spoof_outcome=spoof_outcome,
            )
            return uow.commit(), False

    @staticmethod
    def execute_correct(
        *,
        store,
        authority,
        binding: memory_action_ledger.MemoryActionRequestBinding,
        spoof_outcome: str | None = None,
    ):
        runtime_module = importlib.import_module(type(authority).__module__)
        store_module = importlib.import_module(type(store).__module__)
        with store._action_unit_of_work() as uow:
            replay = uow.claim_request(binding)
            if replay is not None:
                return uow.commit(), True
            canonical_id = uow._insert_canonical_action(
                text=binding.normalized_content,
                metadata={"channel": "web", "source": "relay"},
            )
            envelope = runtime_module.issue_action_envelope(
                authority,
                runtime_module.MemoryActionBinding(
                    action_type=runtime_module.ACTION_CORRECT_USER,
                    canonical_message_id=canonical_id,
                    kind=binding.kind,
                    scope_type=binding.scope_type,
                    scope_ref=binding.scope_ref,
                    normalized_content=binding.normalized_content,
                    sensitivity=binding.sensitivity,
                    memory_key=binding.target_memory_key,
                ),
            )
            result = store.correct_memory_from_user_action(
                memory_key=binding.target_memory_key,
                content=binding.normalized_content,
                sensitivity=binding.sensitivity,
                sources=[
                    store_module.memory_policy.ProvenanceInput(
                        canonical_message_id=canonical_id
                    )
                ],
                authorization=envelope,
                _transaction=uow,
            )
            if spoof_outcome is not None:
                spoofed_semantics = replace(
                    uow._store_outcome.semantics,
                    store_outcome=spoof_outcome,
                )
                uow._store_outcome_semantics = spoofed_semantics
                uow._store_outcome = replace(
                    uow._store_outcome,
                    semantics=spoofed_semantics,
                )
            uow.complete_request()
            return uow.commit(), False

    @staticmethod
    def execute_forget(
        *,
        store,
        authority,
        binding: memory_action_ledger.MemoryActionRequestBinding,
        spoof_outcome: str | None = None,
        uow_sink: list[object] | None = None,
    ):
        binding = replace(binding, normalized_content=None)
        runtime_module = importlib.import_module(type(authority).__module__)
        store_module = importlib.import_module(type(store).__module__)
        with store._action_unit_of_work() as uow:
            if uow_sink is not None:
                uow_sink.append(uow)
            store._get_forget_target_metadata(
                binding.target_memory_key,
                _transaction=uow,
            )
            replay = uow.claim_request(binding)
            if replay is not None:
                return uow.commit(), True
            canonical_id = uow._insert_canonical_action(
                text=f"Forget explicit memory: {binding.target_memory_key}",
                metadata={"channel": "web", "source": "relay"},
            )
            envelope = runtime_module.issue_action_envelope(
                authority,
                runtime_module.MemoryActionBinding(
                    action_type=runtime_module.ACTION_FORGET_USER,
                    canonical_message_id=canonical_id,
                    kind=binding.kind,
                    scope_type=binding.scope_type,
                    scope_ref=binding.scope_ref,
                    normalized_content=None,
                    sensitivity=binding.sensitivity,
                    memory_key=binding.target_memory_key,
                ),
            )
            result = store.forget_memory_atomic(
                memory_key=binding.target_memory_key,
                sources=[
                    store_module.memory_policy.ProvenanceInput(
                        canonical_message_id=canonical_id
                    )
                ],
                authorization=envelope,
                _transaction=uow,
            )
            if spoof_outcome is not None:
                spoofed_semantics = replace(
                    uow._store_outcome.semantics,
                    store_outcome=spoof_outcome,
                )
                uow._store_outcome_semantics = spoofed_semantics
                uow._store_outcome = replace(
                    uow._store_outcome,
                    semantics=spoofed_semantics,
                )
            uow.complete_request()
            return uow.commit(), False

    def test_forget_target_registration_is_owner_and_request_bound(self):
        (
            path,
            store,
            _authority,
            remember_binding,
            remember_result,
        ) = self.completed_remember_case("forget-registration-owner")
        binding = memory_action_ledger.MemoryActionRequestBinding(
            request_id="Z" * 32,
            action_kind="forget",
            origin="operator_cli",
            target_memory_key=remember_result.result_memory_key,
            scope_type=remember_binding.scope_type,
            scope_ref=remember_binding.scope_ref,
            kind=remember_binding.kind,
            sensitivity=remember_binding.sensitivity,
            normalized_content=None,
        )
        other_store = object.__new__(type(store))
        before = self.counts(path)

        with store._action_unit_of_work() as owner:
            target = store._get_forget_target_metadata(
                binding.target_memory_key,
                _transaction=owner,
            )
            registration = owner._forget_target_registration
            self.assertIsNotNone(registration)
            self.assertEqual(repr(registration), "<RegisteredForgetTargetV1>")
            self.assertNotIn(binding.target_memory_key, repr(registration))
            self.assertFalse(hasattr(registration, "__dict__"))

            with self.assertRaisesRegex(
                RuntimeError,
                "invalid_state",
            ):
                store._get_forget_target_metadata(
                    binding.target_memory_key,
                    _transaction=owner,
                )

            with self.assertRaisesRegex(
                RuntimeError,
                "request_binding_conflict",
            ):
                store.forget_memory_atomic(
                    memory_key=binding.target_memory_key,
                    sources=(),
                    authorization=None,
                    _transaction=owner,
                )

            self.assertIsNone(owner.claim_request(binding))
            self.assertIs(
                owner._require_registered_forget_target(store=store),
                target,
            )
            sealed = owner._forget_target_registration
            self.assertEqual(sealed.request_id, binding.request_id)
            self.assertEqual(sealed.origin, binding.origin)
            self.assertEqual(
                sealed.binding_digest,
                memory_action_ledger.request_binding_digest(
                    TEST_HMAC_SECRET,
                    binding,
                ),
            )
            owner._forget_target_registration = replace(
                sealed,
                _metadata=replace(target),
            )
            with self.assertRaisesRegex(
                memory_action_ledger.MemoryActionLedgerError,
                "request_binding_conflict",
            ):
                owner._require_registered_forget_target(store=store)
            owner._forget_target_registration = sealed

            with self.assertRaisesRegex(
                memory_action_ledger.MemoryActionLedgerError,
                "request_binding_conflict",
            ):
                owner._require_registered_forget_target(store=other_store)

            original_binding = owner._binding
            for changed in (
                replace(original_binding, request_id="Y" * 32),
                replace(original_binding, origin="mcp"),
            ):
                with self.subTest(
                    request=changed.request_id,
                    origin=changed.origin,
                ):
                    owner._binding = changed
                    with self.assertRaisesRegex(
                        memory_action_ledger.MemoryActionLedgerError,
                        "request_binding_conflict",
                    ):
                        owner._require_registered_forget_target(store=store)
            owner._binding = original_binding

        self.assertIsNone(owner._forget_target_registration)
        with self.assertRaisesRegex(
            memory_action_ledger.MemoryActionLedgerError,
            "invalid_state",
        ):
            owner._require_registered_forget_target(store=store)
        with store._action_unit_of_work() as foreign_uow:
            foreign_uow._forget_target_registration = registration
            with self.assertRaisesRegex(
                memory_action_ledger.MemoryActionLedgerError,
                "request_binding_conflict",
            ):
                foreign_uow.claim_request(binding)
        self.assertEqual(self.counts(path), before)

    def test_forget_target_registration_rejects_unissued_metadata_shapes(self):
        (
            path,
            store,
            _authority,
            _remember_binding,
            remember_result,
        ) = self.completed_remember_case("forget-registration-fakes")
        native = store._get_forget_target_metadata(
            remember_result.result_memory_key,
        )
        native_type = type(native)

        class MetadataSubclass(native_type):
            pass

        values = {
            field.name: getattr(native, field.name)
            for field in fields(native)
        }
        candidates = (
            native,
            dict(values),
            SimpleNamespace(**values),
            MetadataSubclass(**values),
            replace(native),
        )
        before = self.counts(path)
        for candidate in candidates:
            with (
                self.subTest(candidate=type(candidate).__name__),
                store._action_unit_of_work() as uow,
                self.assertRaisesRegex(
                    memory_action_ledger.MemoryActionLedgerError,
                    "invalid_state",
                ),
            ):
                uow._register_forget_target(
                    store=store,
                    metadata=candidate,
                    issuance=object(),
                )
        self.assertEqual(self.counts(path), before)

    def test_forget_target_registration_clears_on_commit_and_uncertain_close(self):
        (
            _path,
            store,
            authority,
            remember_binding,
            remember_result,
        ) = self.completed_remember_case("forget-registration-commit")
        binding = memory_action_ledger.MemoryActionRequestBinding(
            request_id="Z" * 32,
            action_kind="forget",
            origin="operator_cli",
            target_memory_key=remember_result.result_memory_key,
            scope_type=remember_binding.scope_type,
            scope_ref=remember_binding.scope_ref,
            kind=remember_binding.kind,
            sensitivity=remember_binding.sensitivity,
            normalized_content=None,
        )
        committed_uow: list[object] = []
        result, replay = self.execute_forget(
            store=store,
            authority=authority,
            binding=binding,
            uow_sink=committed_uow,
        )
        self.assertFalse(replay)
        self.assertEqual(result.result_category, "forgotten")
        self.assertIsNone(committed_uow[0]._forget_target_registration)
        with self.assertRaisesRegex(
            memory_action_ledger.MemoryActionLedgerError,
            "invalid_state",
        ):
            committed_uow[0]._require_registered_forget_target(store=store)

        (
            _path,
            store,
            authority,
            remember_binding,
            remember_result,
        ) = self.completed_remember_case("forget-registration-uncertain")
        binding = memory_action_ledger.MemoryActionRequestBinding(
            request_id="Y" * 32,
            action_kind="forget",
            origin="operator_cli",
            target_memory_key=remember_result.result_memory_key,
            scope_type=remember_binding.scope_type,
            scope_ref=remember_binding.scope_ref,
            kind=remember_binding.kind,
            sensitivity=remember_binding.sensitivity,
            normalized_content=None,
        )
        uncertain_uow: list[object] = []
        original_commit = memory_action_ledger._MemoryActionUnitOfWork.commit

        def fail_after_commit(uow):
            uow._connection = _CommitFailureConnection(
                uow._connection,
                after_commit=True,
            )
            return original_commit(uow)

        with (
            mock.patch.object(
                memory_action_ledger._MemoryActionUnitOfWork,
                "commit",
                new=fail_after_commit,
            ),
            self.assertRaisesRegex(
                memory_action_ledger.MemoryActionLedgerError,
                "transaction_outcome_uncertain",
            ),
        ):
            self.execute_forget(
                store=store,
                authority=authority,
                binding=binding,
                uow_sink=uncertain_uow,
            )
        self.assertIsNone(uncertain_uow[0]._forget_target_registration)
        with self.assertRaisesRegex(
            memory_action_ledger.MemoryActionLedgerError,
            "invalid_state",
        ):
            uncertain_uow[0]._require_registered_forget_target(store=store)

    def test_completed_forget_claim_replays_without_prepared_registration(self):
        (
            path,
            store,
            authority,
            remember_binding,
            remember_result,
        ) = self.completed_remember_case("forget-claim-no-registration")
        binding = memory_action_ledger.MemoryActionRequestBinding(
            request_id="X" * 32,
            action_kind="forget",
            origin="operator_cli",
            target_memory_key=remember_result.result_memory_key,
            scope_type=remember_binding.scope_type,
            scope_ref=remember_binding.scope_ref,
            kind=remember_binding.kind,
            sensitivity=remember_binding.sensitivity,
            normalized_content=None,
        )
        first, replayed = self.execute_forget(
            store=store,
            authority=authority,
            binding=binding,
        )
        self.assertFalse(replayed)
        self.assertEqual(first.result_category, "forgotten")
        before = self.counts(path)
        with (
            mock.patch.object(
                memory_action_ledger._MemoryActionUnitOfWork,
                "_seal_registered_forget_target",
                side_effect=AssertionError("replay must not seal registration"),
            ),
            store._action_unit_of_work() as uow,
        ):
            self.assertIsNone(uow._forget_target_metadata_identity)
            self.assertIsNone(uow._forget_target_registration)
            replay = uow.claim_request(binding)
            self.assertEqual(replay, first)
            self.assertIsNone(uow._forget_target_metadata_identity)
            self.assertIsNone(uow._forget_target_registration)
            committed = uow.commit()
        self.assertEqual(committed, first)
        self.assertEqual(self.counts(path), before)

    def assert_replay_rejected_without_growth(
        self,
        *,
        path: str,
        store,
        binding: memory_action_ledger.MemoryActionRequestBinding,
    ) -> None:
        before = self.counts(path)
        with self.assertRaisesRegex(
            RuntimeError,
            "invalid_state|request_binding_conflict|terminal_semantics_invalid",
        ):
            with store._action_unit_of_work() as uow:
                uow.claim_request(binding)
        self.assertEqual(self.counts(path), before)
        self.assertGreaterEqual(before["memory_action_requests"], 1)

    @staticmethod
    def insert_other_item(conn, *, marker: str) -> tuple[int, str]:
        content = f"Other {marker} memory"
        memory_key = marker.upper() * 32
        fingerprint = memory_action_ledger.memory_policy.fingerprint_content(
            TEST_HMAC_SECRET,
            scope_type="global_user",
            scope_ref="",
            kind="project",
            normalized_content=content,
        )
        stamp = channel_store.now_iso()
        cursor = conn.execute(
            """INSERT INTO memory_items
               (memory_key,kind,scope_type,scope_ref,normalized_content,
                normalized_fingerprint,fingerprint_version,status,explicitness,
                confidence,sensitivity,first_observed_at,last_confirmed_at,
                superseded_by_id,created_at,updated_at)
               VALUES(?,'project','global_user','',?,? ,1,'active','explicit',
                      1.0,'normal',?,?,NULL,?,?)""",
            (
                memory_key,
                content,
                fingerprint,
                stamp,
                stamp,
                stamp,
                stamp,
            ),
        )
        return int(cursor.lastrowid), memory_key

    def prepare_suppressed_store(
        self,
        *,
        path: str,
        content: str,
    ):
        self._prepare_path(path)
        runtime = bootstrap_runtime(path, memory_config())
        actions = runtime.privileged_actions
        store = actions._store
        authority = actions._authority
        remember_binding = self.binding(
            request_id="A" * 32,
            content=content,
        )
        created, replay = self.execute_remember_for_store(
            store,
            authority,
            remember_binding,
        )
        self.assertFalse(replay)
        self.assertEqual(created.result_category, "created")
        forget_binding = memory_action_ledger.MemoryActionRequestBinding(
            request_id="B" * 32,
            action_kind="forget",
            origin="operator_cli",
            target_memory_key=created.result_memory_key,
            scope_type=remember_binding.scope_type,
            scope_ref=remember_binding.scope_ref,
            kind=remember_binding.kind,
            sensitivity=remember_binding.sensitivity,
            normalized_content=remember_binding.normalized_content,
        )
        forgotten, replay = self.execute_forget(
            store=store,
            authority=authority,
            binding=forget_binding,
        )
        self.assertFalse(replay)
        self.assertEqual(forgotten.result_category, "forgotten")
        return store, authority

    @staticmethod
    def committed_suppressed_outcome(
        *,
        store,
        authority,
        binding,
    ):
        with store._action_unit_of_work() as uow:
            if uow.claim_request(binding) is not None:
                raise AssertionError("suppressed owner test unexpectedly replayed")
            MemoryActionUnitOfWorkTests.stage_remember_store_action(
                uow,
                binding,
                store=store,
                authority=authority,
            )
            outcome = uow._store_outcome
            terminal = uow.complete_request()
            committed = uow.commit()
        if terminal != committed or terminal.result_category != "suppressed":
            raise AssertionError("suppressed owner fixture did not commit")
        return outcome

    def test_success_commits_ledger_canonical_and_memory_together(self):
        result, replay = self.execute_remember(self.binding())
        self.assertFalse(replay)
        self.assertEqual(result.result_category, "created")
        self.assertEqual(
            self.counts(),
            {
                "messages": 1,
                "memory_action_requests": 1,
                "memory_fingerprint_profile": 1,
                "memory_evidence_events": 1,
                "memory_items": 1,
                "memory_sources": 1,
                "memory_suppressions": 0,
            },
        )

    def test_complete_request_has_no_caller_selected_terminal_parameters(self):
        signature = inspect.signature(
            memory_action_ledger._MemoryActionUnitOfWork.complete_request
        )
        self.assertEqual(tuple(signature.parameters), ("self",))
        record_signature = inspect.signature(
            memory_action_ledger._MemoryActionUnitOfWork._record_store_outcome
        )
        self.assertEqual(
            tuple(record_signature.parameters),
            (
                "self",
                "store",
                "action_id",
                "store_result",
                "suppression_ids",
            ),
        )
        self.assertEqual(
            tuple(
                item.name
                for item in fields(
                    memory_action_ledger.StoreOutcomeSemanticsV1
                )
            ),
            (
                "version",
                "action_kind",
                "store_outcome",
                "result_memory_key",
                "target_memory_key",
                "result_item_id",
                "target_item_id",
                "created_item_ids",
                "evidence_event_ids",
                "source_ids",
                "suppression_ids",
                "created_suppression_ids",
            ),
        )
        self.assertEqual(
            tuple(
                item.name
                for item in fields(
                    memory_action_ledger.TrustedStoreOutcomeV1
                )
            ),
            (
                "_seal",
                "_owner_uow_token",
                "_owner_store",
                "request_id",
                "canonical_message_id",
                "action_id",
                "semantics",
            ),
        )
        with self.store._action_unit_of_work() as uow:
            with self.assertRaises(TypeError):
                uow.complete_request(
                    result_category="created",
                    result_memory_key="X" * 32,
                )

    def test_store_outcome_to_terminal_category_attack_matrix_fails_closed(self):
        def fresh(marker: str):
            path = str(Path(self.temp.name) / f"outcome-{marker}.sqlite3")
            self._prepare_path(path)
            runtime = bootstrap_runtime(path, memory_config())
            actions = runtime.privileged_actions
            return path, actions._store, actions._authority

        def remember_binding(request_char: str, content: str):
            return self.binding(
                request_id=request_char * 32,
                content=content,
            )

        def correction_binding(
            request_char: str,
            *,
            target_key: str,
            content: str,
        ):
            return memory_action_ledger.MemoryActionRequestBinding(
                request_id=request_char * 32,
                action_kind="correct",
                origin="operator_cli",
                target_memory_key=target_key,
                scope_type="global_user",
                scope_ref="",
                kind="project",
                sensitivity="normal",
                normalized_content=content,
            )

        def forget_binding(
            request_char: str,
            *,
            target_key: str,
            content: str | None,
        ):
            return memory_action_ledger.MemoryActionRequestBinding(
                request_id=request_char * 32,
                action_kind="forget",
                origin="operator_cli",
                target_memory_key=target_key,
                scope_type="global_user",
                scope_ref="",
                kind="project",
                sensitivity="normal",
                normalized_content=content,
            )

        for spoofed in ("idempotent_existing", "suppressed"):
            with self.subTest(actual="created", spoofed=spoofed):
                path, store, authority = fresh(f"created-{spoofed}")
                binding = remember_binding("A", "Created matrix memory")
                before = self.counts(path)
                with self.assertRaisesRegex(
                    memory_action_ledger.MemoryActionLedgerError,
                    "terminal_semantics_invalid",
                ):
                    with store._action_unit_of_work() as uow:
                        uow.claim_request(binding)
                        self.stage_remember(
                            uow,
                            binding,
                            store=store,
                            authority=authority,
                            spoof_outcome=spoofed,
                        )
                self.assertEqual(self.counts(path), before)

        for spoofed in ("created", "suppressed"):
            with self.subTest(
                actual="idempotent_existing",
                spoofed=spoofed,
            ):
                path, store, authority = fresh(f"existing-{spoofed}")
                content = "Existing matrix memory"
                seed, _ = self.execute_remember_for_store(
                    store,
                    authority,
                    remember_binding("B", content),
                )
                before = self.counts(path)
                with self.assertRaisesRegex(
                    memory_action_ledger.MemoryActionLedgerError,
                    "terminal_semantics_invalid",
                ):
                    self.execute_remember_for_store(
                        store,
                        authority,
                        remember_binding("C", content),
                        spoof_outcome=spoofed,
                    )
                self.assertEqual(self.counts(path), before)
                self.assertIsNotNone(seed.result_memory_key)

        for spoofed in ("created", "idempotent_existing"):
            with self.subTest(actual="suppressed", spoofed=spoofed):
                path, store, authority = fresh(f"suppressed-{spoofed}")
                content = "Suppressed matrix memory"
                seed, _ = self.execute_remember_for_store(
                    store,
                    authority,
                    remember_binding("D", content),
                )
                self.execute_forget(
                    store=store,
                    authority=authority,
                    binding=forget_binding(
                        "E",
                        target_key=seed.result_memory_key,
                        content=content,
                    ),
                )
                before = self.counts(path)
                with self.assertRaisesRegex(
                    memory_action_ledger.MemoryActionLedgerError,
                    "terminal_semantics_invalid",
                ):
                    self.execute_remember_for_store(
                        store,
                        authority,
                        remember_binding("F", content),
                        spoof_outcome=spoofed,
                    )
                self.assertEqual(self.counts(path), before)

        for actual, content, spoofed_values in (
            ("corrected", "Corrected matrix replacement", ("idempotent_noop", "suppressed")),
            ("idempotent_noop", "Correct matrix seed", ("corrected", "suppressed")),
        ):
            for spoofed in spoofed_values:
                with self.subTest(actual=actual, spoofed=spoofed):
                    path, store, authority = fresh(f"{actual}-{spoofed}")
                    seed_content = "Correct matrix seed"
                    seed, _ = self.execute_remember_for_store(
                        store,
                        authority,
                        remember_binding("G", seed_content),
                    )
                    before = self.counts(path)
                    with self.assertRaisesRegex(
                        memory_action_ledger.MemoryActionLedgerError,
                        "terminal_semantics_invalid",
                    ):
                        self.execute_correct(
                            store=store,
                            authority=authority,
                            binding=correction_binding(
                                "H",
                                target_key=seed.result_memory_key,
                                content=content,
                            ),
                            spoof_outcome=spoofed,
                        )
                    self.assertEqual(self.counts(path), before)

        for spoofed in ("corrected", "idempotent_noop"):
            with self.subTest(actual="correct_suppressed", spoofed=spoofed):
                path, store, authority = fresh(f"correct-suppressed-{spoofed}")
                target, _ = self.execute_remember_for_store(
                    store,
                    authority,
                    remember_binding("I", "Correction target"),
                )
                blocked_content = "Blocked correction replacement"
                blocked, _ = self.execute_remember_for_store(
                    store,
                    authority,
                    remember_binding("J", blocked_content),
                )
                self.execute_forget(
                    store=store,
                    authority=authority,
                    binding=forget_binding(
                        "K",
                        target_key=blocked.result_memory_key,
                        content=blocked_content,
                    ),
                )
                before = self.counts(path)
                with self.assertRaisesRegex(
                    memory_action_ledger.MemoryActionLedgerError,
                    "terminal_semantics_invalid",
                ):
                    self.execute_correct(
                        store=store,
                        authority=authority,
                        binding=correction_binding(
                            "L",
                            target_key=target.result_memory_key,
                            content=blocked_content,
                        ),
                        spoof_outcome=spoofed,
                    )
                self.assertEqual(self.counts(path), before)

        with self.subTest(actual="forgotten", spoofed="already_forgotten"):
            path, store, authority = fresh("forgotten")
            content = "Forget matrix memory"
            seed, _ = self.execute_remember_for_store(
                store,
                authority,
                remember_binding("M", content),
            )
            before = self.counts(path)
            with self.assertRaisesRegex(
                memory_action_ledger.MemoryActionLedgerError,
                "terminal_semantics_invalid",
            ):
                self.execute_forget(
                    store=store,
                    authority=authority,
                    binding=forget_binding(
                        "N",
                        target_key=seed.result_memory_key,
                        content=content,
                    ),
                    spoof_outcome="already_forgotten",
                )
            self.assertEqual(self.counts(path), before)

        with self.subTest(actual="already_forgotten", spoofed="forgotten"):
            path, store, authority = fresh("already-forgotten")
            content = "Already forgotten matrix memory"
            seed, _ = self.execute_remember_for_store(
                store,
                authority,
                remember_binding("O", content),
            )
            self.execute_forget(
                store=store,
                authority=authority,
                binding=forget_binding(
                    "P",
                    target_key=seed.result_memory_key,
                    content=content,
                ),
            )
            before = self.counts(path)
            with self.assertRaisesRegex(
                memory_action_ledger.MemoryActionLedgerError,
                "terminal_semantics_invalid",
            ):
                self.execute_forget(
                    store=store,
                    authority=authority,
                    binding=forget_binding(
                        "Q",
                        target_key=seed.result_memory_key,
                        content=None,
                    ),
                    spoof_outcome="forgotten",
                )
            self.assertEqual(self.counts(path), before)

    def test_suppressed_outcome_rejects_cross_uow_before_and_after_defer(self):
        path = str(Path(self.temp.name) / "owner-cross-uow.sqlite3")
        content = "Owner-bound suppressed memory"
        store, authority = self.prepare_suppressed_store(
            path=path,
            content=content,
        )
        foreign_binding = self.binding(
            request_id="C" * 32,
            content=content,
        )
        foreign = self.committed_suppressed_outcome(
            store=store,
            authority=authority,
            binding=foreign_binding,
        )

        after_defer_binding = self.binding(
            request_id="D" * 32,
            content=content,
        )
        before = self.counts(path)
        with self.assertRaisesRegex(
            memory_action_ledger.MemoryActionLedgerError,
            "invalid_state",
        ):
            with store._action_unit_of_work() as uow:
                self.assertIsNone(uow.claim_request(after_defer_binding))
                self.stage_remember_store_action(
                    uow,
                    after_defer_binding,
                    store=store,
                    authority=authority,
                )
                self.assertEqual(
                    repr(uow._store_outcome),
                    "<TrustedStoreOutcomeV1>",
                )
                self.assertEqual(
                    repr(uow._store_outcome.semantics),
                    "<StoreOutcomeSemanticsV1>",
                )
                self.assertNotIn(
                    after_defer_binding.request_id,
                    repr(uow._store_outcome),
                )
                self.assertFalse(hasattr(uow._store_outcome, "__dict__"))
                self.assertFalse(
                    hasattr(uow._store_outcome.semantics, "__dict__")
                )
                with self.assertRaises(FrozenInstanceError):
                    uow._store_outcome.action_id = "Z" * 32
                with self.assertRaises(FrozenInstanceError):
                    uow._store_outcome.semantics.store_outcome = "created"
                current_action_id = uow._store_outcome.action_id
                uow._store_outcome = foreign
                uow.complete_request()
        self.assertEqual(self.counts(path), before)
        self.assertNotIn(current_action_id, authority._inflight_actions)
        self.assertNotIn(current_action_id, authority._consumed_actions)

        before_defer_binding = self.binding(
            request_id="E" * 32,
            content=content,
        )
        before = self.counts(path)
        captured_action_ids: list[str] = []
        with self.assertRaisesRegex(RuntimeError, "invalid_state"):
            with store._action_unit_of_work() as uow:
                self.assertIsNone(uow.claim_request(before_defer_binding))
                original_defer = type(uow)._defer_action

                def transplant_then_defer(inner, action_id):
                    captured_action_ids.append(action_id)
                    inner._store_outcome = foreign
                    return original_defer(inner, action_id)

                with mock.patch.object(
                    type(uow),
                    "_defer_action",
                    new=transplant_then_defer,
                ):
                    self.stage_remember_store_action(
                        uow,
                        before_defer_binding,
                        store=store,
                        authority=authority,
                    )
        self.assertEqual(self.counts(path), before)
        self.assertEqual(len(captured_action_ids), 1)
        self.assertNotIn(captured_action_ids[0], authority._inflight_actions)
        self.assertNotIn(captured_action_ids[0], authority._consumed_actions)

        replay, was_replay = self.execute_remember_for_store(
            store,
            authority,
            foreign_binding,
        )
        self.assertTrue(was_replay)
        self.assertEqual(replay.result_category, "suppressed")

    def test_suppressed_outcome_rejects_cross_store_transplant(self):
        fixed_stamp = "2030-01-02T03:04:05+00:00"
        content = "Cross-store owner-bound suppression"
        path_a = str(Path(self.temp.name) / "owner-store-a.sqlite3")
        path_b = str(Path(self.temp.name) / "owner-store-b.sqlite3")
        live_store_module = importlib.import_module("backend.memory_store")
        with mock.patch.object(
            live_store_module.channel_store,
            "now_iso",
            return_value=fixed_stamp,
        ):
            store_a, authority_a = self.prepare_suppressed_store(
                path=path_a,
                content=content,
            )
            foreign = self.committed_suppressed_outcome(
                store=store_a,
                authority=authority_a,
                binding=self.binding(
                    request_id="C" * 32,
                    content=content,
                ),
            )
            store_b, authority_b = self.prepare_suppressed_store(
                path=path_b,
                content=content,
            )

            with channel_store.connect(path_a) as conn_a:
                suppression_a = tuple(conn_a.execute(
                    """SELECT id,scope_type,scope_ref,kind,
                              hex(normalized_fingerprint),fingerprint_version,
                              reason_category,created_at
                       FROM memory_suppressions"""
                ).fetchone())
            with channel_store.connect(path_b) as conn_b:
                suppression_b = tuple(conn_b.execute(
                    """SELECT id,scope_type,scope_ref,kind,
                              hex(normalized_fingerprint),fingerprint_version,
                              reason_category,created_at
                       FROM memory_suppressions"""
                ).fetchone())
            self.assertEqual(suppression_a, suppression_b)

            binding_b = self.binding(
                request_id="D" * 32,
                content=content,
            )
            before = self.counts(path_b)
            with self.assertRaisesRegex(
                memory_action_ledger.MemoryActionLedgerError,
                "invalid_state",
            ):
                with store_b._action_unit_of_work() as uow:
                    self.assertIsNone(uow.claim_request(binding_b))
                    self.stage_remember_store_action(
                        uow,
                        binding_b,
                        store=store_b,
                        authority=authority_b,
                    )
                    native = uow._store_outcome
                    self.assertEqual(foreign.semantics, native.semantics)
                    cross_store = replace(
                        foreign,
                        _owner_uow_token=uow._store_outcome_owner_token,
                        request_id=binding_b.request_id,
                        canonical_message_id=uow._canonical_message_id,
                        action_id=native.action_id,
                        semantics=native.semantics,
                    )
                    uow._store_outcome = cross_store
                    uow.complete_request()
            self.assertEqual(self.counts(path_b), before)

    def test_live_outcome_revalidates_every_owner_and_identity_field(self):
        mutations = (
            (
                "owner-token",
                lambda native, uow: replace(
                    native,
                    _owner_uow_token=object(),
                ),
            ),
            (
                "owner-store",
                lambda native, _uow: replace(
                    native,
                    _owner_store=object(),
                ),
            ),
            (
                "request",
                lambda native, _uow: replace(
                    native,
                    request_id="Z" * 32,
                ),
            ),
            (
                "canonical",
                lambda native, _uow: replace(
                    native,
                    canonical_message_id=native.canonical_message_id + 100,
                ),
            ),
            (
                "action",
                lambda native, _uow: replace(
                    native,
                    action_id="Z" * 32,
                ),
            ),
            (
                "semantics-object",
                lambda native, _uow: replace(
                    native,
                    semantics=replace(native.semantics),
                ),
            ),
            (
                "seal",
                lambda native, _uow: replace(
                    native,
                    _seal=object(),
                ),
            ),
        )
        for index, (marker, mutate) in enumerate(mutations):
            with self.subTest(marker=marker):
                path = str(
                    Path(self.temp.name) / f"owner-field-{index}.sqlite3"
                )
                content = f"Owner field {index} suppressed memory"
                store, authority = self.prepare_suppressed_store(
                    path=path,
                    content=content,
                )
                binding = self.binding(
                    request_id="C" * 32,
                    content=content,
                )
                before = self.counts(path)
                with self.assertRaisesRegex(
                    memory_action_ledger.MemoryActionLedgerError,
                    "invalid_state",
                ):
                    with store._action_unit_of_work() as uow:
                        self.assertIsNone(uow.claim_request(binding))
                        self.stage_remember_store_action(
                            uow,
                            binding,
                            store=store,
                            authority=authority,
                        )
                        native = uow._store_outcome
                        uow._store_outcome = mutate(native, uow)
                        uow.complete_request()
                self.assertEqual(self.counts(path), before)

    def test_live_outcome_rejects_fake_dict_namespace_and_subclass(self):
        @dataclass(frozen=True)
        class FakeOutcome:
            semantics: object

        def subclass_outcome(native, _uow):
            class OutcomeSubclass(
                memory_action_ledger.TrustedStoreOutcomeV1
            ):
                pass

            return OutcomeSubclass(
                _seal=native._seal,
                _owner_uow_token=native._owner_uow_token,
                _owner_store=native._owner_store,
                request_id=native.request_id,
                canonical_message_id=native.canonical_message_id,
                action_id=native.action_id,
                semantics=native.semantics,
            )

        candidates = (
            ("dict", lambda native, _uow: {"semantics": native.semantics}),
            (
                "namespace",
                lambda native, _uow: SimpleNamespace(
                    semantics=native.semantics
                ),
            ),
            (
                "fake-dataclass",
                lambda native, _uow: FakeOutcome(native.semantics),
            ),
            ("subclass", subclass_outcome),
        )
        for index, (marker, candidate) in enumerate(candidates):
            with self.subTest(marker=marker):
                path = str(
                    Path(self.temp.name) / f"owner-type-{index}.sqlite3"
                )
                content = f"Owner type {index} suppressed memory"
                store, authority = self.prepare_suppressed_store(
                    path=path,
                    content=content,
                )
                binding = self.binding(
                    request_id="C" * 32,
                    content=content,
                )
                before = self.counts(path)
                with self.assertRaisesRegex(
                    memory_action_ledger.MemoryActionLedgerError,
                    "invalid_state",
                ):
                    with store._action_unit_of_work() as uow:
                        self.assertIsNone(uow.claim_request(binding))
                        self.stage_remember_store_action(
                            uow,
                            binding,
                            store=store,
                            authority=authority,
                        )
                        native = uow._store_outcome
                        uow._store_outcome = candidate(native, uow)
                        uow.complete_request()
                self.assertEqual(self.counts(path), before)

    def test_live_outcome_missing_and_duplicate_recording_are_rejected(self):
        missing_path = str(
            Path(self.temp.name) / "owner-missing-outcome.sqlite3"
        )
        missing_content = "Missing owner-bound suppressed outcome"
        missing_store, missing_authority = self.prepare_suppressed_store(
            path=missing_path,
            content=missing_content,
        )
        missing_binding = self.binding(
            request_id="C" * 32,
            content=missing_content,
        )
        before = self.counts(missing_path)
        with self.assertRaisesRegex(
            memory_action_ledger.MemoryActionLedgerError,
            "invalid_state",
        ):
            with missing_store._action_unit_of_work() as uow:
                self.assertIsNone(uow.claim_request(missing_binding))
                self.stage_remember_store_action(
                    uow,
                    missing_binding,
                    store=missing_store,
                    authority=missing_authority,
                )
                uow._store_outcome = None
                uow.complete_request()
        self.assertEqual(self.counts(missing_path), before)

        duplicate_path = str(
            Path(self.temp.name) / "owner-duplicate-outcome.sqlite3"
        )
        duplicate_content = "Duplicate owner-bound suppressed outcome"
        duplicate_store, duplicate_authority = self.prepare_suppressed_store(
            path=duplicate_path,
            content=duplicate_content,
        )
        duplicate_binding = self.binding(
            request_id="C" * 32,
            content=duplicate_content,
        )
        with duplicate_store._action_unit_of_work() as uow:
            self.assertIsNone(uow.claim_request(duplicate_binding))
            self.stage_remember_store_action(
                uow,
                duplicate_binding,
                store=duplicate_store,
                authority=duplicate_authority,
            )
            native = uow._store_outcome
            deferred = tuple(uow._deferred_actions)
            with self.assertRaisesRegex(
                memory_action_ledger.MemoryActionLedgerError,
                "invalid_state",
            ):
                uow._record_store_outcome(
                    store=duplicate_store,
                    action_id=native.action_id,
                    store_result=object(),
                    suppression_ids=(),
                )
            self.assertIs(uow._store_outcome, native)
            self.assertEqual(tuple(uow._deferred_actions), deferred)
            terminal = uow.complete_request()
            self.assertEqual(uow.commit(), terminal)

    def test_live_outcome_cannot_complete_twice_or_cross_restart(self):
        path = str(Path(self.temp.name) / "owner-reload.sqlite3")
        content = "Reload-bound suppressed memory"
        store, authority = self.prepare_suppressed_store(
            path=path,
            content=content,
        )
        first_binding = self.binding(
            request_id="C" * 32,
            content=content,
        )
        with store._action_unit_of_work() as uow:
            self.assertIsNone(uow.claim_request(first_binding))
            self.stage_remember_store_action(
                uow,
                first_binding,
                store=store,
                authority=authority,
            )
            old_outcome = uow._store_outcome
            first = uow.complete_request()
            with self.assertRaisesRegex(
                memory_action_ledger.MemoryActionLedgerError,
                "invalid_state",
            ):
                uow.complete_request()
            self.assertEqual(uow.commit(), first)

        importlib.reload(memory_action_ledger)
        second_binding = self.binding(
            request_id="D" * 32,
            content=content,
        )
        before = self.counts(path)
        with self.assertRaisesRegex(
            memory_action_ledger.MemoryActionLedgerError,
            "invalid_state",
        ):
            with store._action_unit_of_work() as uow:
                self.assertIsNone(uow.claim_request(second_binding))
                self.stage_remember_store_action(
                    uow,
                    second_binding,
                    store=store,
                    authority=authority,
                )
                uow._store_outcome = old_outcome
                uow.complete_request()
        self.assertEqual(self.counts(path), before)

    def test_correct_and_forget_terminal_semantics_commit_and_replay(self):
        (
            _correct_path,
            correct_store,
            correct_authority,
            remember_binding,
            remember_result,
        ) = self.completed_remember_case("correct-flow")
        correct_binding = memory_action_ledger.MemoryActionRequestBinding(
            request_id="Q" * 32,
            action_kind="correct",
            origin="operator_cli",
            target_memory_key=remember_result.result_memory_key,
            scope_type=remember_binding.scope_type,
            scope_ref=remember_binding.scope_ref,
            kind=remember_binding.kind,
            sensitivity=remember_binding.sensitivity,
            normalized_content="Corrected synthetic memory",
        )
        first, replay = self.execute_correct(
            store=correct_store,
            authority=correct_authority,
            binding=correct_binding,
        )
        self.assertFalse(replay)
        self.assertEqual(first.result_category, "corrected")
        second, replay = self.execute_correct(
            store=correct_store,
            authority=correct_authority,
            binding=correct_binding,
        )
        self.assertTrue(replay)
        self.assertEqual(first, second)

        (
            _forget_path,
            forget_store,
            forget_authority,
            forget_remember_binding,
            forget_remember_result,
        ) = self.completed_remember_case("forget-flow")
        forget_binding = memory_action_ledger.MemoryActionRequestBinding(
            request_id="G" * 32,
            action_kind="forget",
            origin="operator_cli",
            target_memory_key=forget_remember_result.result_memory_key,
            scope_type=forget_remember_binding.scope_type,
            scope_ref=forget_remember_binding.scope_ref,
            kind=forget_remember_binding.kind,
            sensitivity=forget_remember_binding.sensitivity,
            normalized_content=forget_remember_binding.normalized_content,
        )
        first, replay = self.execute_forget(
            store=forget_store,
            authority=forget_authority,
            binding=forget_binding,
        )
        self.assertFalse(replay)
        self.assertEqual(first.result_category, "forgotten")
        second, replay = self.execute_forget(
            store=forget_store,
            authority=forget_authority,
            binding=forget_binding,
        )
        self.assertTrue(replay)
        self.assertEqual(first, second)

    def test_replay_and_changed_payload_binding(self):
        binding = self.binding()
        first, replay = self.execute_remember(binding)
        self.runtime = bootstrap_runtime(self.path, memory_config())
        self.actions = self.runtime.privileged_actions
        self.store = self.actions._store
        self.authority = self.actions._authority
        second, replay = self.execute_remember(binding)
        self.assertFalse(first is second)
        self.assertTrue(replay)
        self.assertEqual(first, second)
        with self.store._action_unit_of_work() as uow:
            with self.assertRaisesRegex(
                memory_action_ledger.MemoryActionLedgerError,
                "request_binding_conflict",
            ):
                uow.claim_request(self.binding(content="Changed synthetic payload"))
        self.assertEqual(self.counts()["memory_action_requests"], 1)
        with channel_store.connect(self.path) as conn:
            for statement in (
                """UPDATE memory_action_requests
                   SET result_category='idempotent_existing'
                   WHERE request_id=?""",
                "DELETE FROM memory_action_requests WHERE request_id=?",
            ):
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "memory_action_request_immutable",
                ):
                    conn.execute(statement, (binding.request_id,))
        third, replay = self.execute_remember(binding)
        self.assertTrue(replay)
        self.assertEqual(first, third)

    def test_replay_revalidates_actual_terminal_request_columns_and_digest(self):
        cases = (
            ("action-kind", "action_kind", "correct", True),
            ("origin", "origin", "mcp", False),
            ("target-key", "target_memory_key", "T" * 32, True),
            ("status", "status", "failed", True),
            (
                "result-category",
                "result_category",
                "idempotent_existing",
                False,
            ),
            ("canonical-id", "canonical_message_id", "other_canonical", False),
            ("result-key", "result_memory_key", "other_item", False),
            (
                "created-at",
                "created_at",
                "2030-01-02T03:04:05+00:00",
                True,
            ),
            (
                "updated-at",
                "updated_at",
                "2030-01-02T03:04:05+00:00",
                True,
            ),
        )
        trigger = channel_store.MEMORY_ACTION_REQUEST_TRIGGER_DDL[
            "memory_action_requests_immutable_update"
        ]
        for marker, column, raw_value, ignore_checks in cases:
            with self.subTest(column=column):
                path, store, _authority, binding, _result = (
                    self.completed_remember_case(marker)
                )
                with channel_store.connect(path) as conn:
                    value = raw_value
                    if raw_value == "other_canonical":
                        cursor = conn.execute(
                            """INSERT INTO messages(ts,direction,kind,text,meta)
                               VALUES(?,'in','user','Other canonical',
                                      '{"channel":"web","source":"relay"}')""",
                            (channel_store.now_iso(),),
                        )
                        value = int(cursor.lastrowid)
                    elif raw_value == "other_item":
                        _item_id, value = self.insert_other_item(
                            conn,
                            marker="Z",
                        )
                    conn.execute(
                        "DROP TRIGGER memory_action_requests_immutable_update"
                    )
                    if ignore_checks:
                        conn.execute("PRAGMA ignore_check_constraints=ON")
                    conn.execute(
                        f"""UPDATE memory_action_requests SET {column}=?
                            WHERE request_id=?""",
                        (value, binding.request_id),
                    )
                    if ignore_checks:
                        conn.execute("PRAGMA ignore_check_constraints=OFF")
                    conn.execute(trigger)
                    channel_store.validate_memory_action_schema(conn)
                before = self.counts(path)
                with self.assertRaises(
                    memory_action_ledger.MemoryActionLedgerError
                ) as raised:
                    with store._action_unit_of_work() as uow:
                        uow.claim_request(binding)
                self.assertIn(
                    raised.exception.category,
                    {
                        "memory_schema_invalid",
                        "request_binding_conflict",
                        "terminal_semantics_invalid",
                    },
                )
                self.assertEqual(
                    str(raised.exception),
                    raised.exception.category,
                )
                self.assertEqual(self.counts(path), before)
                with channel_store.connect(path) as conn:
                    self.assertEqual(
                        conn.execute(
                            f"""SELECT {column}
                                FROM memory_action_requests
                                WHERE request_id=?""",
                            (binding.request_id,),
                        ).fetchone()[0],
                        value,
                    )

    def test_missing_terminal_trigger_blocks_validator_and_claim(self):
        binding = self.binding()
        self.execute_remember(binding)
        with channel_store.connect(self.path) as conn:
            conn.execute(
                "DROP TRIGGER memory_action_requests_immutable_delete"
            )
            with self.assertRaisesRegex(
                sqlite3.DatabaseError,
                "memory action",
            ):
                channel_store.validate_memory_action_schema(conn)
        with self.assertRaisesRegex(
            memory_action_ledger.MemoryActionLedgerError,
            "memory_schema_invalid",
        ):
            with self.store._action_unit_of_work():
                self.fail("invalid v8 schema must fail before claim")
        self.assertEqual(self.counts()["memory_action_requests"], 1)

    def test_replay_authenticates_canonical_evidence_source_item_and_result(self):
        canonical_cases = (
            ("canonical-text", "UPDATE messages SET text='Tampered text'"),
            (
                "canonical-meta",
                """UPDATE messages
                   SET meta='{"channel":"web","source":"relay","extra":"x"}'""",
            ),
            (
                "canonical-role",
                "UPDATE messages SET direction='out',kind='reply'",
            ),
            (
                "canonical-channel",
                """UPDATE messages
                   SET meta='{"channel":"telegram","source":"relay"}'""",
            ),
            (
                "canonical-source",
                """UPDATE messages
                   SET meta='{"channel":"web","source":"mcp"}'""",
            ),
        )
        for marker, statement in canonical_cases:
            with self.subTest(marker=marker):
                (
                    path,
                    store,
                    _authority,
                    binding,
                    _result,
                ) = self.completed_remember_case(marker)
                with channel_store.connect(path) as conn:
                    conn.execute(statement)
                self.assert_replay_rejected_without_growth(
                    path=path,
                    store=store,
                    binding=binding,
                )

        evidence_cases = (
            ("evidence-action-id", "action_id", "E" * 32, False),
            (
                "evidence-action-type",
                "action_type",
                "correct_explicit_user",
                False,
            ),
            (
                "evidence-binding-version",
                "action_binding_version",
                2,
                True,
            ),
            (
                "evidence-type",
                "evidence_type",
                "explicit_user_correction",
                False,
            ),
            ("evidence-reality", "reality_scope", "fiction", False),
            ("evidence-subject", "subject_scope", "project", False),
            (
                "evidence-component",
                "created_by_component",
                "web_adapter",
                False,
            ),
        )
        update_trigger = channel_store.MEMORY_TRIGGER_DDL[
            "memory_evidence_events_immutable_update"
        ]
        for marker, column, value, ignore_checks in evidence_cases:
            with self.subTest(marker=marker):
                (
                    path,
                    store,
                    _authority,
                    binding,
                    _result,
                ) = self.completed_remember_case(marker)
                with channel_store.connect(path) as conn:
                    conn.execute("PRAGMA foreign_keys=OFF")
                    conn.execute(
                        "DROP TRIGGER memory_evidence_events_immutable_update"
                    )
                    if ignore_checks:
                        conn.execute("PRAGMA ignore_check_constraints=ON")
                    conn.execute(
                        f"UPDATE memory_evidence_events SET {column}=?",
                        (value,),
                    )
                    if ignore_checks:
                        conn.execute("PRAGMA ignore_check_constraints=OFF")
                    conn.execute(update_trigger)
                    conn.execute("PRAGMA foreign_keys=ON")
                self.assert_replay_rejected_without_growth(
                    path=path,
                    store=store,
                    binding=binding,
                )

        source_cases = (
            ("source-channel", "channel", "telegram"),
            ("source-source", "source", "mcp"),
            ("source-role", "evidence_role", "assistant"),
        )
        for marker, column, value in source_cases:
            with self.subTest(marker=marker):
                (
                    path,
                    store,
                    _authority,
                    binding,
                    _result,
                ) = self.completed_remember_case(marker)
                with channel_store.connect(path) as conn:
                    conn.execute(
                        f"UPDATE memory_sources SET {column}=?",
                        (value,),
                    )
                self.assert_replay_rejected_without_growth(
                    path=path,
                    store=store,
                    binding=binding,
                )

        item_cases = (
            (
                "item-content",
                """UPDATE memory_items
                   SET normalized_content='Tampered item content'""",
            ),
            ("item-kind", "UPDATE memory_items SET kind='task_or_progress'"),
            (
                "item-scope",
                """UPDATE memory_items
                   SET scope_type='project',scope_ref='tampered'""",
            ),
            (
                "item-sensitivity",
                "UPDATE memory_items SET sensitivity='sensitive'",
            ),
            ("item-state", "UPDATE memory_items SET status='rejected'"),
        )
        for marker, statement in item_cases:
            with self.subTest(marker=marker):
                (
                    path,
                    store,
                    _authority,
                    binding,
                    _result,
                ) = self.completed_remember_case(marker)
                with channel_store.connect(path) as conn:
                    conn.execute(statement)
                self.assert_replay_rejected_without_growth(
                    path=path,
                    store=store,
                    binding=binding,
                )

        path, store, _authority, binding, _result = self.completed_remember_case(
            "result-key"
        )
        with channel_store.connect(path) as conn:
            _other_id, other_key = self.insert_other_item(
                conn,
                marker="Z",
            )
            conn.execute(
                "DROP TRIGGER memory_action_requests_immutable_update"
            )
            conn.execute(
                """UPDATE memory_action_requests SET result_memory_key=?
                   WHERE request_id=?""",
                (other_key, binding.request_id),
            )
            conn.execute(
                channel_store.MEMORY_ACTION_REQUEST_TRIGGER_DDL[
                    "memory_action_requests_immutable_update"
                ]
            )
        self.assert_replay_rejected_without_growth(
            path=path,
            store=store,
            binding=binding,
        )

        path, store, _authority, binding, result = self.completed_remember_case(
            "source-link"
        )
        with channel_store.connect(path) as conn:
            other_id, _other_key = self.insert_other_item(
                conn,
                marker="Y",
            )
            conn.execute(
                """UPDATE memory_sources SET memory_id=?
                   WHERE memory_id=(
                     SELECT id FROM memory_items WHERE memory_key=?
                   )""",
                (other_id, result.result_memory_key),
            )
        self.assert_replay_rejected_without_growth(
            path=path,
            store=store,
            binding=binding,
        )

    def test_replay_rejects_deleted_or_added_evidence_and_sources(self):
        path, store, _authority, binding, _result = self.completed_remember_case(
            "delete-source"
        )
        with channel_store.connect(path) as conn:
            conn.execute("DELETE FROM memory_sources")
        self.assert_replay_rejected_without_growth(
            path=path,
            store=store,
            binding=binding,
        )

        path, store, _authority, binding, _result = self.completed_remember_case(
            "delete-evidence"
        )
        with channel_store.connect(path) as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(
                "DROP TRIGGER memory_evidence_events_immutable_delete"
            )
            conn.execute("DELETE FROM memory_evidence_events")
            conn.execute(
                channel_store.MEMORY_TRIGGER_DDL[
                    "memory_evidence_events_immutable_delete"
                ]
            )
            conn.execute("PRAGMA foreign_keys=ON")
        self.assert_replay_rejected_without_growth(
            path=path,
            store=store,
            binding=binding,
        )

        path, store, _authority, binding, result = self.completed_remember_case(
            "add-provenance"
        )
        with channel_store.connect(path) as conn:
            stamp = channel_store.now_iso()
            canonical = conn.execute(
                """INSERT INTO messages(ts,direction,kind,text,meta)
                   VALUES(?,'in','user','Additional provenance',
                          '{"channel":"web","source":"relay"}')""",
                (stamp,),
            )
            evidence = conn.execute(
                """INSERT INTO memory_evidence_events
                   (canonical_message_id,action_id,action_type,
                    action_binding_version,evidence_type,reality_scope,
                    subject_scope,created_by_component,created_at)
                   VALUES(? ,?,'remember_explicit_user',1,
                          'explicit_user_memory','real','user',
                          'memory_admin',?)""",
                (int(canonical.lastrowid), "P" * 32, stamp),
            )
            memory_id = conn.execute(
                "SELECT id FROM memory_items WHERE memory_key=?",
                (result.result_memory_key,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO memory_sources
                   (memory_id,evidence_event_id,canonical_message_id,channel,
                    source,evidence_role,evidence_type,created_at)
                   VALUES(?,?,?,'web','relay','user',
                          'explicit_user_memory',?)""",
                (
                    memory_id,
                    int(evidence.lastrowid),
                    int(canonical.lastrowid),
                    stamp,
                ),
            )
        self.assert_replay_rejected_without_growth(
            path=path,
            store=store,
            binding=binding,
        )

    def test_forget_replay_authenticates_suppression_semantics(self):
        cases = (
            ("scope_type", "UPDATE memory_suppressions SET scope_type='project'"),
            ("scope_ref", "UPDATE memory_suppressions SET scope_ref='tampered'"),
            ("kind", "UPDATE memory_suppressions SET kind='task_or_progress'"),
            (
                "fingerprint_version",
                """UPDATE memory_suppressions
                   SET fingerprint_version=fingerprint_version+1""",
            ),
            (
                "normalized_fingerprint",
                """UPDATE memory_suppressions
                   SET normalized_fingerprint=zeroblob(32)""",
            ),
            (
                "reason_category",
                """UPDATE memory_suppressions
                   SET reason_category='privacy_policy'""",
            ),
            (
                "created_at",
                "UPDATE memory_suppressions SET created_at='tampered'",
            ),
            ("deleted", "DELETE FROM memory_suppressions"),
            ("replaced", None),
        )
        columns = (
            "id",
            "scope_type",
            "scope_ref",
            "kind",
            "fingerprint_version",
            "normalized_fingerprint",
            "reason_category",
            "created_at",
        )
        for index, (name, statement) in enumerate(cases):
            with self.subTest(name=name):
                (
                    path,
                    store,
                    authority,
                    remember_binding,
                    remember_result,
                ) = self.completed_remember_case(
                    f"suppression-{index}"
                )
                forget_binding = memory_action_ledger.MemoryActionRequestBinding(
                    request_id=str(index) * 32,
                    action_kind="forget",
                    origin="operator_cli",
                    target_memory_key=remember_result.result_memory_key,
                    scope_type=remember_binding.scope_type,
                    scope_ref=remember_binding.scope_ref,
                    kind=remember_binding.kind,
                    sensitivity=remember_binding.sensitivity,
                    normalized_content=None,
                )
                result, replay = self.execute_forget(
                    store=store,
                    authority=authority,
                    binding=forget_binding,
                )
                self.assertFalse(replay)
                self.assertEqual(result.result_category, "forgotten")
                if name == "replaced":
                    second_binding = self.binding(
                        request_id="X" * 32,
                        content="Synthetic replacement suppression memory",
                    )
                    second_result, second_replay = (
                        self.execute_remember_for_store(
                            store,
                            authority,
                            second_binding,
                        )
                    )
                    self.assertFalse(second_replay)
                    second_forget = memory_action_ledger.MemoryActionRequestBinding(
                        request_id="Y" * 32,
                        action_kind="forget",
                        origin="operator_cli",
                        target_memory_key=second_result.result_memory_key,
                        scope_type=second_binding.scope_type,
                        scope_ref=second_binding.scope_ref,
                        kind=second_binding.kind,
                        sensitivity=second_binding.sensitivity,
                        normalized_content=None,
                    )
                    self.execute_forget(
                        store=store,
                        authority=authority,
                        binding=second_forget,
                    )
                with channel_store.connect(path) as conn:
                    conn.execute("PRAGMA foreign_keys=OFF")
                    conn.execute("PRAGMA ignore_check_constraints=ON")
                    if statement is not None:
                        conn.execute(statement)
                    else:
                        rows = conn.execute(
                            f"""SELECT {','.join(columns)}
                                FROM memory_suppressions ORDER BY id"""
                        ).fetchall()
                        self.assertEqual(len(rows), 2)
                        conn.execute("DELETE FROM memory_suppressions")
                        placeholders = ",".join("?" for _ in columns)
                        conn.execute(
                            f"""INSERT INTO memory_suppressions
                                ({','.join(columns)}) VALUES({placeholders})""",
                            (
                                rows[1]["id"],
                                *(
                                    rows[0][column]
                                    for column in columns[1:]
                                ),
                            ),
                        )
                        conn.execute(
                            f"""INSERT INTO memory_suppressions
                                ({','.join(columns)}) VALUES({placeholders})""",
                            (
                                rows[0]["id"],
                                *(
                                    rows[1][column]
                                    for column in columns[1:]
                                ),
                            ),
                        )
                    conn.execute("PRAGMA ignore_check_constraints=OFF")
                    conn.execute("PRAGMA foreign_keys=ON")
                self.assert_replay_rejected_without_growth(
                    path=path,
                    store=store,
                    binding=forget_binding,
                )

    def test_forget_replay_rejects_every_tombstone_semantic_tamper(self):
        cases = (
            ("status", "UPDATE memory_items SET status='active'"),
            ("kind", "UPDATE memory_items SET kind='task_or_progress'"),
            (
                "scope",
                """UPDATE memory_items
                   SET scope_type='project',scope_ref='tampered'""",
            ),
            (
                "sensitivity",
                "UPDATE memory_items SET sensitivity='sensitive'",
            ),
            (
                "fingerprint-version",
                """UPDATE memory_items
                   SET fingerprint_version=fingerprint_version+1""",
            ),
            ("updated-at", "UPDATE memory_items SET updated_at='tampered'"),
            (
                "supersession",
                "UPDATE memory_items SET superseded_by_id=id",
            ),
            (
                "dangling-supersession",
                "UPDATE memory_items SET superseded_by_id=id+999999",
            ),
            ("valid-supersession", None),
            (
                "content",
                """UPDATE memory_items
                   SET normalized_content='FORGET_TAMPERED_CONTENT'""",
            ),
            (
                "fingerprint",
                """UPDATE memory_items
                   SET normalized_fingerprint=zeroblob(32)""",
            ),
        )
        for index, (name, statement) in enumerate(cases):
            with self.subTest(name=name):
                (
                    path,
                    store,
                    authority,
                    remember_binding,
                    remember_result,
                ) = self.completed_remember_case(
                    f"forget-tombstone-{index}"
                )
                binding = memory_action_ledger.MemoryActionRequestBinding(
                    request_id=chr(75 + index) * 32,
                    action_kind="forget",
                    origin="operator_cli",
                    target_memory_key=remember_result.result_memory_key,
                    scope_type=remember_binding.scope_type,
                    scope_ref=remember_binding.scope_ref,
                    kind=remember_binding.kind,
                    sensitivity=remember_binding.sensitivity,
                    normalized_content=None,
                )
                result, replay = self.execute_forget(
                    store=store,
                    authority=authority,
                    binding=binding,
                )
                self.assertFalse(replay)
                self.assertEqual(result.result_category, "forgotten")
                parameters = ()
                if name == "valid-supersession":
                    second_binding = self.binding(
                        request_id="Z" * 32,
                        content="Synthetic valid supersession target",
                    )
                    second_result, second_replay = (
                        self.execute_remember_for_store(
                            store,
                            authority,
                            second_binding,
                        )
                    )
                    self.assertFalse(second_replay)
                    statement = """UPDATE memory_items
                                   SET superseded_by_id=(
                                       SELECT id FROM memory_items
                                       WHERE memory_key=?
                                   )
                                   WHERE memory_key=?"""
                    parameters = (
                        second_result.result_memory_key,
                        binding.target_memory_key,
                    )
                with channel_store.connect(path) as conn:
                    conn.execute("PRAGMA foreign_keys=OFF")
                    conn.execute("PRAGMA ignore_check_constraints=ON")
                    conn.execute(statement, parameters)
                    conn.execute("PRAGMA ignore_check_constraints=OFF")
                    conn.execute("PRAGMA foreign_keys=ON")
                self.assert_replay_rejected_without_growth(
                    path=path,
                    store=store,
                    binding=binding,
                )

    def test_replay_rejects_missing_outcome_required_suppression_rows(self):
        def remove_and_replay(path, store, binding, reason):
            with channel_store.connect(path) as conn:
                deleted = conn.execute(
                    """DELETE FROM memory_suppressions
                       WHERE reason_category=?""",
                    (reason,),
                )
                self.assertGreaterEqual(deleted.rowcount, 1)
            self.assert_replay_rejected_without_growth(
                path=path,
                store=store,
                binding=binding,
            )

        (
            path,
            store,
            authority,
            remember_binding,
            remember_result,
        ) = self.completed_remember_case("missing-forgotten-suppression")
        forgotten_binding = memory_action_ledger.MemoryActionRequestBinding(
            request_id="A" * 32,
            action_kind="forget",
            origin="operator_cli",
            target_memory_key=remember_result.result_memory_key,
            scope_type=remember_binding.scope_type,
            scope_ref=remember_binding.scope_ref,
            kind=remember_binding.kind,
            sensitivity=remember_binding.sensitivity,
            normalized_content=remember_binding.normalized_content,
        )
        self.execute_forget(
            store=store,
            authority=authority,
            binding=forgotten_binding,
        )
        remove_and_replay(
            path,
            store,
            forgotten_binding,
            "user_forget",
        )

        (
            path,
            store,
            authority,
            remember_binding,
            remember_result,
        ) = self.completed_remember_case("missing-already-suppression")
        initial_forget = memory_action_ledger.MemoryActionRequestBinding(
            request_id="B" * 32,
            action_kind="forget",
            origin="operator_cli",
            target_memory_key=remember_result.result_memory_key,
            scope_type=remember_binding.scope_type,
            scope_ref=remember_binding.scope_ref,
            kind=remember_binding.kind,
            sensitivity=remember_binding.sensitivity,
            normalized_content=remember_binding.normalized_content,
        )
        self.execute_forget(
            store=store,
            authority=authority,
            binding=initial_forget,
        )
        already_binding = replace(
            initial_forget,
            request_id="C" * 32,
            normalized_content=None,
        )
        result, replay = self.execute_forget(
            store=store,
            authority=authority,
            binding=already_binding,
        )
        self.assertFalse(replay)
        self.assertEqual(result.result_category, "already_forgotten")
        remove_and_replay(
            path,
            store,
            already_binding,
            "user_forget",
        )

        (
            path,
            store,
            authority,
            remember_binding,
            remember_result,
        ) = self.completed_remember_case("missing-corrected-suppression")
        corrected_binding = memory_action_ledger.MemoryActionRequestBinding(
            request_id="D" * 32,
            action_kind="correct",
            origin="operator_cli",
            target_memory_key=remember_result.result_memory_key,
            scope_type=remember_binding.scope_type,
            scope_ref=remember_binding.scope_ref,
            kind=remember_binding.kind,
            sensitivity=remember_binding.sensitivity,
            normalized_content="Corrected missing suppression memory",
        )
        self.execute_correct(
            store=store,
            authority=authority,
            binding=corrected_binding,
        )
        remove_and_replay(
            path,
            store,
            corrected_binding,
            "corrected_obsolete",
        )

        (
            path,
            store,
            authority,
            remember_binding,
            remember_result,
        ) = self.completed_remember_case("missing-matched-suppression")
        forget_binding = memory_action_ledger.MemoryActionRequestBinding(
            request_id="E" * 32,
            action_kind="forget",
            origin="operator_cli",
            target_memory_key=remember_result.result_memory_key,
            scope_type=remember_binding.scope_type,
            scope_ref=remember_binding.scope_ref,
            kind=remember_binding.kind,
            sensitivity=remember_binding.sensitivity,
            normalized_content=remember_binding.normalized_content,
        )
        self.execute_forget(
            store=store,
            authority=authority,
            binding=forget_binding,
        )
        suppressed_binding = replace(
            remember_binding,
            request_id="F" * 32,
        )
        result, replay = self.execute_remember_for_store(
            store,
            authority,
            suppressed_binding,
        )
        self.assertFalse(replay)
        self.assertEqual(result.result_category, "suppressed")
        remove_and_replay(
            path,
            store,
            suppressed_binding,
            "user_forget",
        )

    def test_initial_terminal_semantic_miswiring_rolls_back_all_state(self):
        cases = (
            (
                "canonical-content",
                "Canonical content A",
                {"channel": "web", "source": "relay"},
                None,
            ),
            (
                "origin-source",
                "Memory content B",
                {"channel": "web", "source": "mcp"},
                None,
            ),
            (
                "scope",
                "Memory content B",
                {"channel": "web", "source": "relay"},
                """UPDATE memory_items
                   SET scope_type='project',scope_ref='miswired'""",
            ),
            (
                "sensitivity",
                "Memory content B",
                {"channel": "web", "source": "relay"},
                "UPDATE memory_items SET sensitivity='sensitive'",
            ),
        )
        for index, (name, canonical_text, metadata, mutation) in enumerate(cases):
            with self.subTest(name=name):
                path = str(
                    Path(self.temp.name) / f"miswire-{name}.sqlite3"
                )
                self._prepare_path(path)
                runtime = bootstrap_runtime(path, memory_config())
                actions = runtime.privileged_actions
                store = actions._store
                authority = actions._authority
                runtime_module = importlib.import_module(
                    type(authority).__module__
                )
                store_module = importlib.import_module(type(store).__module__)
                binding = self.binding(
                    request_id=chr(65 + index) * 32,
                    content="Memory content B",
                )
                with self.assertRaisesRegex(
                    memory_action_ledger.MemoryActionLedgerError,
                    "terminal_semantics_invalid",
                ):
                    with store._action_unit_of_work() as uow:
                        self.assertIsNone(uow.claim_request(binding))
                        canonical_id = uow._insert_canonical_action(
                            text=canonical_text,
                            metadata=metadata,
                        )
                        envelope = runtime_module.issue_action_envelope(
                            authority,
                            runtime_module.MemoryActionBinding(
                                action_type=(
                                    runtime_module.ACTION_REMEMBER_USER
                                ),
                                canonical_message_id=canonical_id,
                                kind=binding.kind,
                                scope_type=binding.scope_type,
                                scope_ref=binding.scope_ref,
                                normalized_content=binding.normalized_content,
                                sensitivity=binding.sensitivity,
                            ),
                        )
                        store_result = (
                            store.create_explicit_memory_from_user_action(
                                kind=binding.kind,
                                scope_type=binding.scope_type,
                                scope_ref=binding.scope_ref,
                                content=binding.normalized_content,
                                sensitivity=binding.sensitivity,
                                sources=[
                                    store_module.memory_policy.ProvenanceInput(
                                        canonical_message_id=canonical_id
                                    )
                                ],
                                authorization=envelope,
                                _transaction=uow,
                            )
                        )
                        if mutation is not None:
                            uow._execute(mutation)
                        uow.complete_request()
                self.assertTrue(
                    all(value == 0 for value in self.counts(path).values())
                )

    def test_concurrent_same_request_has_one_writer_and_stable_replays(self):
        binding = self.binding()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _index: self.execute_remember(binding),
                range(8),
            ))
        self.assertEqual(sum(not replay for _result, replay in results), 1)
        self.assertEqual(sum(replay for _result, replay in results), 7)
        self.assertEqual(
            {result.result_category for result, _replay in results},
            {"created"},
        )
        counts = self.counts()
        self.assertEqual(counts["messages"], 1)
        self.assertEqual(counts["memory_action_requests"], 1)
        self.assertEqual(counts["memory_evidence_events"], 1)
        self.assertEqual(counts["memory_items"], 1)
        self.assertEqual(counts["memory_sources"], 1)

    def test_deterministic_input_failure_has_no_ledger_or_other_state(self):
        with self.assertRaisesRegex(
            memory_action_ledger.MemoryActionLedgerError,
            "invalid_content",
        ):
            with self.store._action_unit_of_work() as uow:
                self.assertIsNone(uow.claim_request(self.binding()))
                raise memory_action_ledger.MemoryActionLedgerError(
                    "invalid_content"
                )
        self.assertTrue(all(value == 0 for value in self.counts().values()))

    def test_faults_at_claim_canonical_store_and_terminal_boundaries_rollback(self):
        stages = ("after_claim", "after_canonical", "inside_store", "after_store", "after_terminal")
        for index, stage in enumerate(stages):
            with self.subTest(stage=stage):
                path = str(Path(self.temp.name) / f"fault-{index}.sqlite3")
                self._prepare_path(path)
                runtime = bootstrap_runtime(path, memory_config())
                actions = runtime.privileged_actions
                store = actions._store
                authority = actions._authority
                binding = self.binding(request_id=chr(65 + index) * 32)
                with self.assertRaisesRegex(
                    RuntimeError,
                    "synthetic|storage_unavailable",
                ):
                    with store._action_unit_of_work() as uow:
                        uow.claim_request(binding)
                        if stage == "after_claim":
                            raise RuntimeError("synthetic_after_claim")
                        canonical_id = uow._insert_canonical_action(
                            text=binding.normalized_content,
                            metadata={"channel": "web", "source": "relay"},
                        )
                        if stage == "after_canonical":
                            raise RuntimeError("synthetic_after_canonical")
                        runtime_module = importlib.import_module(
                            type(authority).__module__
                        )
                        store_module = importlib.import_module(
                            type(store).__module__
                        )
                        envelope = runtime_module.issue_action_envelope(
                            authority,
                            runtime_module.MemoryActionBinding(
                                action_type=runtime_module.ACTION_REMEMBER_USER,
                                canonical_message_id=canonical_id,
                                kind="project",
                                scope_type="global_user",
                                scope_ref="",
                                normalized_content=binding.normalized_content,
                                sensitivity="normal",
                            ),
                        )
                        store_class = type(store)
                        patcher = (
                            mock.patch.object(
                                store_class,
                                "_insert_sources",
                                side_effect=sqlite3.OperationalError(
                                    "synthetic_inside_store"
                                ),
                            )
                            if stage == "inside_store"
                            else mock.patch.object(
                                store_class,
                                "_insert_sources",
                                wraps=store_class._insert_sources,
                            )
                        )
                        with patcher:
                            result = store.create_explicit_memory_from_user_action(
                                kind="project",
                                scope_type="global_user",
                                scope_ref="",
                                content=binding.normalized_content,
                                sensitivity="normal",
                                sources=[
                                    store_module.memory_policy.ProvenanceInput(
                                        canonical_id
                                    )
                                ],
                                authorization=envelope,
                                _transaction=uow,
                            )
                        if stage == "after_store":
                            raise RuntimeError("synthetic_after_store")
                        uow.complete_request()
                        raise RuntimeError("synthetic_after_terminal")
                counts = self.counts(path)
                self.assertTrue(all(value == 0 for value in counts.values()))

    def test_known_and_uncertain_commit_failures_are_distinguished(self):
        for after_commit, category, expected_rows in (
            (False, "storage_unavailable", 0),
            (True, "transaction_outcome_uncertain", 1),
        ):
            with self.subTest(after_commit=after_commit):
                path = str(
                    Path(self.temp.name) / f"commit-{int(after_commit)}.sqlite3"
                )
                self._prepare_path(path)
                runtime = bootstrap_runtime(path, memory_config())
                actions = runtime.privileged_actions
                store = actions._store
                authority = actions._authority
                binding = self.binding(
                    request_id=("U" if after_commit else "K") * 32
                )
                with store._action_unit_of_work() as uow:
                    uow.claim_request(binding)
                    self.stage_remember(
                        uow,
                        binding,
                        store=store,
                        authority=authority,
                    )
                    uow._connection = _CommitFailureConnection(
                        uow._connection,
                        after_commit=after_commit,
                    )
                    with self.assertRaisesRegex(
                        memory_action_ledger.MemoryActionLedgerError,
                        category,
                    ):
                        uow.commit()
                self.assertEqual(
                    self.counts(path)["memory_action_requests"],
                    expected_rows,
                )

    def test_uow_rejects_arbitrary_context_and_nested_store(self):
        with self.assertRaises(TypeError):
            self.store._action_unit_of_work(object())
        with self.assertRaisesRegex(RuntimeError, "transaction_context_invalid"):
            self.store.create_explicit_memory_from_user_action(
                kind="project",
                scope_type="global_user",
                scope_ref="",
                content="Synthetic invalid transaction",
                sensitivity="normal",
                sources=[object()],
                authorization=None,
                _transaction=object(),
            )
        with self.store._action_unit_of_work() as uow:
            uow.claim_request(self.binding())
            uow._insert_canonical_action(
                text="Synthetic UoW memory",
                metadata={"channel": "web", "source": "relay"},
            )
            connection = uow._store_connection(self.store)
            connection.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("BEGIN IMMEDIATE")

    def test_uow_binds_the_store_action_to_the_claimed_request(self):
        binding = self.binding()
        runtime_module = importlib.import_module(type(self.authority).__module__)
        with self.store._action_unit_of_work() as uow:
            self.assertIsNone(uow.claim_request(binding))
            canonical_id = uow._insert_canonical_action(
                text=binding.normalized_content,
                metadata={"channel": "web", "source": "relay"},
            )
            action = runtime_module.MemoryActionBinding(
                action_type=runtime_module.ACTION_REMEMBER_USER,
                canonical_message_id=canonical_id,
                kind=binding.kind,
                scope_type=binding.scope_type,
                scope_ref=binding.scope_ref,
                normalized_content=binding.normalized_content,
                sensitivity=binding.sensitivity,
            )
            uow._validate_store_action(self.store, action)
            mismatches = (
                replace(action, action_type=runtime_module.ACTION_CORRECT_USER),
                replace(action, canonical_message_id=canonical_id + 1),
                replace(action, kind="decision"),
                replace(action, scope_type="project", scope_ref="synthetic"),
                replace(action, scope_ref="synthetic"),
                replace(action, normalized_content="Different synthetic binding"),
                replace(action, sensitivity="sensitive"),
                replace(action, memory_key="M" * 32),
            )
            for mismatch in mismatches:
                with self.subTest(field=mismatch), self.assertRaisesRegex(
                    memory_action_ledger.MemoryActionLedgerError,
                    "request_binding_conflict",
                ):
                    uow._validate_store_action(self.store, mismatch)
        self.assertTrue(all(value == 0 for value in self.counts().values()))

    def test_store_rejects_a_capability_for_a_different_claimed_request(self):
        binding = self.binding()
        different_content = "Different synthetic binding"
        runtime_module = importlib.import_module(type(self.authority).__module__)
        store_module = importlib.import_module(type(self.store).__module__)
        with self.assertRaisesRegex(
            store_module.MemoryStoreError,
            "request_binding_conflict",
        ):
            with self.store._action_unit_of_work() as uow:
                self.assertIsNone(uow.claim_request(binding))
                canonical_id = uow._insert_canonical_action(
                    text=binding.normalized_content,
                    metadata={"channel": "web", "source": "relay"},
                )
                envelope = runtime_module.issue_action_envelope(
                    self.authority,
                    runtime_module.MemoryActionBinding(
                        action_type=runtime_module.ACTION_REMEMBER_USER,
                        canonical_message_id=canonical_id,
                        kind=binding.kind,
                        scope_type=binding.scope_type,
                        scope_ref=binding.scope_ref,
                        normalized_content=different_content,
                        sensitivity=binding.sensitivity,
                    ),
                )
                self.store.create_explicit_memory_from_user_action(
                    kind=binding.kind,
                    scope_type=binding.scope_type,
                    scope_ref=binding.scope_ref,
                    content=different_content,
                    sensitivity=binding.sensitivity,
                    sources=[
                        store_module.memory_policy.ProvenanceInput(
                            canonical_message_id=canonical_id
                        )
                    ],
                    authorization=envelope,
                    _transaction=uow,
                )
        self.assertTrue(all(value == 0 for value in self.counts().values()))


if __name__ == "__main__":
    unittest.main()
