from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest import mock

from backend import (
    channel_store,
    memory_action_ledger,
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
            )
            self.assertEqual(terminal.status, "completed")
            return uow.commit(), False

    @staticmethod
    def stage_remember(uow, binding, *, store, authority):
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
        return uow.complete_request(
            result_category=result.outcome,
            result_memory_key=result.item["memory_key"],
        )

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
            conn.execute(
                """UPDATE memory_action_requests
                   SET result_category='idempotent_existing'
                   WHERE request_id=?""",
                (binding.request_id,),
            )
        with self.store._action_unit_of_work() as uow:
            with self.assertRaisesRegex(
                memory_action_ledger.MemoryActionLedgerError,
                "request_binding_conflict",
            ):
                uow.claim_request(binding)
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """UPDATE memory_action_requests SET result_category='created'
                   WHERE request_id=?""",
                (binding.request_id,),
            )
            conn.execute(
                "DELETE FROM memory_sources",
            )
            conn.execute(
                "DROP TRIGGER memory_evidence_events_immutable_delete",
            )
            conn.execute(
                "DELETE FROM memory_evidence_events",
            )
            conn.execute(
                channel_store.MEMORY_TRIGGER_DDL[
                    "memory_evidence_events_immutable_delete"
                ],
            )
        with self.store._action_unit_of_work() as uow:
            with self.assertRaisesRegex(
                memory_action_ledger.MemoryActionLedgerError,
                "memory_schema_invalid",
            ):
                uow.claim_request(binding)

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
                        uow.complete_request(
                            result_category=result.outcome,
                            result_memory_key=result.item["memory_key"],
                        )
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
                        memory_action_ledger.memory_policy.ProvenanceInput(
                            canonical_message_id=canonical_id
                        )
                    ],
                    authorization=envelope,
                    _transaction=uow,
                )
        self.assertTrue(all(value == 0 for value in self.counts().values()))


if __name__ == "__main__":
    unittest.main()
