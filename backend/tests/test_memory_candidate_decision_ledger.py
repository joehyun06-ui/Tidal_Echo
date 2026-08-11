from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend import (
    channel_store,
    memory_candidate_decision_ledger,
    memory_candidate_review,
    memory_candidate_review_adapters,
    memory_policy,
    memory_store,
)


TEST_SECRET = "Synthetic-Decision-HMAC-Key-2026-Alpha!Z9q7"


class CandidateDecisionTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "decision.sqlite3")
        with channel_store.connect(self.path) as conn:
            conn.execute("""CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')""")
        channel_store.run_migrations(self.path)

    def add_memory(
        self,
        conn: sqlite3.Connection,
        *,
        candidate_key: str = "C" * 32,
        digest: bytes = b"c" * 32,
    ) -> int:
        stamp = channel_store.now_iso()
        cursor = conn.execute(
            """INSERT INTO memory_items
               (memory_key,kind,scope_type,scope_ref,normalized_content,
                normalized_fingerprint,fingerprint_version,status,explicitness,
                confidence,sensitivity,first_observed_at,last_confirmed_at,
                superseded_by_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,1,'candidate','inferred',0.0,'normal',
                      ?,?,NULL,?,?)""",
            (
                candidate_key,
                "project",
                "global_user",
                "",
                "synthetic candidate",
                digest,
                stamp,
                stamp,
                stamp,
                stamp,
            ),
        )
        return int(cursor.lastrowid)

    def add_suppression(self, conn: sqlite3.Connection) -> int:
        cursor = conn.execute(
            """INSERT INTO memory_suppressions
               (scope_type,scope_ref,kind,normalized_fingerprint,
                fingerprint_version,reason_category,created_at)
               VALUES('global_user','','project',?,1,'user_reject',?)""",
            (b"s" * 32, channel_store.now_iso()),
        )
        return int(cursor.lastrowid)

    def binding(
        self,
        *,
        request_id: str = "R" * 32,
        candidate_key: str = "C" * 32,
        origin: str = "operator_cli",
        decision: str = "approve",
    ) -> memory_candidate_decision_ledger.CandidateDecisionLedgerBindingV1:
        return memory_candidate_decision_ledger.CandidateDecisionLedgerBindingV1(
            request_id=request_id,
            origin=origin,
            decision=decision,
            candidate_key=candidate_key,
        )

    def insert_decision(
        self,
        conn: sqlite3.Connection,
        *,
        memory_id: int,
        binding: memory_candidate_decision_ledger.CandidateDecisionLedgerBindingV1,
        suppression_id: int | None = None,
    ) -> None:
        conn.execute(
            """INSERT INTO memory_candidate_decisions
               (request_id,memory_id,origin,decision,request_binding_digest,
                suppression_id,review_contract_version,
                decision_contract_version,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                binding.request_id,
                memory_id,
                binding.origin,
                binding.decision,
                memory_candidate_decision_ledger.binding_digest(binding),
                suppression_id,
                binding.review_contract_version,
                binding.decision_contract_version,
                channel_store.now_iso(),
            ),
        )


class CandidateDecisionMigrationTests(CandidateDecisionTestBase):
    def test_fresh_database_reaches_exact_v10_schema(self):
        with channel_store.connect(self.path) as conn:
            markers = [
                tuple(row)
                for row in conn.execute(
                    "SELECT version,name,status FROM schema_migrations ORDER BY version"
                )
            ]
            columns = tuple(
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_xinfo(memory_candidate_decisions)"
                )
            )
            indexes = {
                row["name"]: (
                    bool(row["unique"]),
                    row["origin"],
                    bool(row["partial"]),
                    channel_store._index_columns(conn, row["name"]),
                )
                for row in conn.execute(
                    "PRAGMA index_list(memory_candidate_decisions)"
                )
            }
            foreign_keys = {
                (
                    row["from"], row["table"], row["to"],
                    row["on_update"], row["on_delete"], row["match"],
                )
                for row in conn.execute(
                    "PRAGMA foreign_key_list(memory_candidate_decisions)"
                )
            }
            channel_store.validate_memory_candidate_decision_schema_v1_v10(
                conn
            )

        self.assertEqual(len(markers), 10)
        self.assertEqual(
            markers[-1],
            (
                10,
                "memory_candidate_decision_ledger_foundation",
                "applied",
            ),
        )
        self.assertEqual(
            columns,
            (
                "request_id",
                "memory_id",
                "origin",
                "decision",
                "request_binding_digest",
                "suppression_id",
                "review_contract_version",
                "decision_contract_version",
                "created_at",
            ),
        )
        self.assertEqual(
            indexes,
            {
                "sqlite_autoindex_memory_candidate_decisions_1": (
                    True, "pk", False, ("request_id",),
                ),
                "sqlite_autoindex_memory_candidate_decisions_2": (
                    True, "u", False, ("memory_id",),
                ),
            },
        )
        self.assertEqual(
            foreign_keys,
            {
                (
                    "memory_id", "memory_items", "id",
                    "NO ACTION", "RESTRICT", "NONE",
                ),
                (
                    "suppression_id", "memory_suppressions", "id",
                    "NO ACTION", "RESTRICT", "NONE",
                ),
            },
        )

    def test_decision_table_checks_are_closed_and_exact(self):
        with channel_store.connect(self.path) as conn:
            memory_id = self.add_memory(conn)
            suppression_id = self.add_suppression(conn)
            stamp = channel_store.now_iso()

            def insert(values):
                conn.execute(
                    """INSERT INTO memory_candidate_decisions
                       (request_id,memory_id,origin,decision,
                        request_binding_digest,suppression_id,
                        review_contract_version,decision_contract_version,
                        created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    values,
                )

            base = (
                "R" * 32,
                memory_id,
                "operator_cli",
                "approve",
                b"d" * 32,
                None,
                memory_candidate_review.CANDIDATE_REVIEW_CONTRACT_VERSION,
                memory_candidate_decision_ledger.CANDIDATE_DECISION_CONTRACT_VERSION,
                stamp,
            )
            invalid = (
                ("request-short", (*base[:0], "short", *base[1:])),
                ("request-long", (*base[:0], "R" * 97, *base[1:])),
                ("request-alphabet", (*base[:0], "R" * 31 + "!", *base[1:])),
                ("origin", (*base[:2], "web", *base[3:])),
                ("decision", (*base[:3], "maybe", *base[4:])),
                ("digest-type", (*base[:4], "d" * 32, *base[5:])),
                ("digest-length", (*base[:4], b"d" * 31, *base[5:])),
                (
                    "approve-suppression",
                    (*base[:5], suppression_id, *base[6:]),
                ),
                (
                    "reject-without-suppression",
                    (*base[:3], "reject", *base[4:]),
                ),
                ("review-version", (*base[:6], ".bad", *base[7:])),
                ("decision-version", (*base[:7], "bad/value", base[8])),
                ("timestamp", (*base[:8], "not-a-timestamp")),
            )
            for name, values in invalid:
                with self.subTest(name=name), self.assertRaises(
                    sqlite3.IntegrityError
                ):
                    insert(values)

            with self.assertRaises(sqlite3.IntegrityError):
                insert((*base[:1], memory_id + 999, *base[2:]))
            with self.assertRaises(sqlite3.IntegrityError):
                insert((
                    "S" * 32,
                    memory_id,
                    "mcp",
                    "reject",
                    b"e" * 32,
                    suppression_id + 999,
                    base[6],
                    base[7],
                    stamp,
                ))

            insert(base)
            with self.assertRaises(sqlite3.IntegrityError):
                insert(("S" * 32, *base[1:]))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM memory_items WHERE id=?", (memory_id,)
                )

    def test_reject_requires_and_restricts_real_suppression(self):
        with channel_store.connect(self.path) as conn:
            memory_id = self.add_memory(conn)
            suppression_id = self.add_suppression(conn)
            binding = self.binding(decision="reject", origin="mcp")
            self.insert_decision(
                conn,
                memory_id=memory_id,
                binding=binding,
                suppression_id=suppression_id,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM memory_suppressions WHERE id=?",
                    (suppression_id,),
                )

    def test_terminal_ledger_is_immutable(self):
        with channel_store.connect(self.path) as conn:
            memory_id = self.add_memory(conn)
            self.insert_decision(
                conn,
                memory_id=memory_id,
                binding=self.binding(),
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "memory_candidate_decision_immutable",
            ):
                conn.execute(
                    "UPDATE memory_candidate_decisions SET origin='mcp'"
                )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "memory_candidate_decision_immutable",
            ):
                conn.execute("DELETE FROM memory_candidate_decisions")

    def test_failed_v10_rolls_back_all_owned_objects_and_marker(self):
        path = str(Path(self.temp.name) / "rollback.sqlite3")
        with channel_store.connect(path) as conn:
            conn.execute("""CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')""")
        channel_store.run_migrations(path, channel_store.MIGRATIONS[:9])

        def broken(conn):
            channel_store._migration_010(conn)
            raise RuntimeError("injected-v10")

        migrations = (
            *channel_store.MIGRATIONS[:9],
            (
                10,
                "memory_candidate_decision_ledger_foundation",
                broken,
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "^injected-v10$"):
            channel_store.run_migrations(path, migrations)
        with channel_store.connect(path) as conn:
            objects = conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE name LIKE 'memory_candidate_decision%'"""
            ).fetchall()
            marker = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version=10"
            ).fetchone()
            self.assertEqual(objects, [])
            self.assertIsNone(marker)
            channel_store.validate_memory_candidate_persistence_schema(conn)

    def test_v1_v9_objects_are_untouched_by_additive_v10(self):
        path = str(Path(self.temp.name) / "additive.sqlite3")
        with channel_store.connect(path) as conn:
            conn.execute("""CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')""")
        channel_store.run_migrations(path, channel_store.MIGRATIONS[:9])
        with channel_store.connect(path) as conn:
            before = {
                (row["type"], row["name"]): row["sql"]
                for row in conn.execute(
                    """SELECT type,name,sql FROM sqlite_master
                       WHERE name NOT LIKE 'sqlite_autoindex_%'"""
                )
            }
        channel_store.run_migrations(path)
        with channel_store.connect(path) as conn:
            after = {
                key: conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
                    key,
                ).fetchone()[0]
                for key in before
            }
            channel_store.validate_memory_candidate_persistence_schema(conn)
        self.assertEqual(after, before)

    def test_v9_validator_allows_only_exact_named_v10_objects(self):
        with channel_store.connect(self.path) as conn:
            channel_store.validate_memory_candidate_persistence_schema(conn)

        corruptions = (
            (
                "table",
                "CREATE TABLE memory_candidate_decision_shadow(id INTEGER)",
            ),
            (
                "index",
                "CREATE INDEX idx_memory_candidate_decision_shadow "
                "ON memory_candidate_decisions(origin)",
            ),
        )
        for name, script in corruptions:
            with self.subTest(name=name):
                path = str(Path(self.temp.name) / f"v9-rogue-{name}.sqlite3")
                with channel_store.connect(path) as conn:
                    conn.execute("""CREATE TABLE messages(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,direction TEXT NOT NULL,
                        kind TEXT NOT NULL,text TEXT NOT NULL,
                        meta TEXT NOT NULL DEFAULT '{}')""")
                channel_store.run_migrations(path)
                with channel_store.connect(path) as conn:
                    conn.execute(script)
                with channel_store.connect(path) as conn, self.assertRaises(
                    sqlite3.DatabaseError
                ):
                    channel_store.validate_memory_candidate_persistence_schema(
                        conn
                    )
                with channel_store.connect(path) as conn, self.assertRaises(
                    sqlite3.DatabaseError
                ):
                    channel_store.validate_memory_candidate_decision_schema_v1_v10(
                        conn
                    )

    def test_v10_validator_rejects_marker_ddl_trigger_and_owned_object_drift(self):
        corruptions = (
            (
                "marker",
                "UPDATE schema_migrations SET name='wrong' WHERE version=10",
            ),
            (
                "table-check",
                """PRAGMA writable_schema=ON;
                   UPDATE sqlite_master
                   SET sql=replace(sql,'BETWEEN 32 AND 96','BETWEEN 31 AND 96')
                   WHERE type='table' AND name='memory_candidate_decisions';
                   PRAGMA writable_schema=OFF""",
            ),
            (
                "trigger",
                "DROP TRIGGER memory_candidate_decisions_immutable_update",
            ),
            (
                "owned-object",
                "CREATE INDEX idx_memory_candidate_decision_extra "
                "ON memory_candidate_decisions(origin)",
            ),
        )
        for name, script in corruptions:
            with self.subTest(name=name):
                path = str(Path(self.temp.name) / f"tamper-{name}.sqlite3")
                with channel_store.connect(path) as conn:
                    conn.execute("""CREATE TABLE messages(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,direction TEXT NOT NULL,
                        kind TEXT NOT NULL,text TEXT NOT NULL,
                        meta TEXT NOT NULL DEFAULT '{}')""")
                channel_store.run_migrations(path)
                with channel_store.connect(path) as conn:
                    conn.executescript(script)
                with channel_store.connect(path) as conn, self.assertRaises(
                    sqlite3.DatabaseError
                ):
                    channel_store.validate_memory_candidate_decision_schema_v1_v10(
                        conn
                    )


class CandidateDecisionBindingTests(CandidateDecisionTestBase):
    def test_request_issuer_and_validators_share_canonical_pattern(self):
        self.assertIs(
            memory_candidate_decision_ledger.CANDIDATE_DECISION_REQUEST_ID_PATTERN,
            memory_policy.OPAQUE_MEMORY_ID_PATTERN,
        )
        self.assertIs(
            memory_policy.MEMORY_KEY_PATTERN,
            memory_policy.OPAQUE_MEMORY_ID_PATTERN,
        )
        for _index in range(100):
            request_id = (
                memory_candidate_decision_ledger.issue_candidate_decision_request_id()
            )
            self.assertIsNotNone(
                memory_policy.OPAQUE_MEMORY_ID_PATTERN.fullmatch(request_id)
            )

    def test_binding_is_fixed_validated_and_repr_safe(self):
        binding = self.binding()
        self.assertIs(
            memory_candidate_decision_ledger.validate_binding(binding),
            binding,
        )
        self.assertEqual(repr(binding), "<CandidateDecisionLedgerBindingV1>")
        self.assertNotIn(binding.request_id, repr(binding))
        self.assertNotIn(binding.candidate_key, repr(binding))
        with self.assertRaises(TypeError):
            memory_candidate_decision_ledger.CandidateDecisionLedgerBindingV1(
                request_id="R" * 32,
                origin="mcp",
                decision="reject",
                candidate_key="C" * 32,
                review_contract_version="caller-override",
            )

        for field, value, category in (
            ("request_id", "short", "invalid_candidate_decision_request"),
            ("origin", "web", "invalid_candidate_decision_request"),
            ("decision", "maybe", "invalid_candidate_decision_request"),
            ("candidate_key", "bad", "invalid_candidate_key"),
        ):
            changed = dataclasses.replace(binding, **{field: value})
            with self.subTest(field=field), self.assertRaisesRegex(
                memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError,
                f"^{category}$",
            ):
                memory_candidate_decision_ledger.validate_binding(changed)

    def test_digest_is_exact_deterministic_and_semantically_bound(self):
        binding = self.binding()
        digest = memory_candidate_decision_ledger.binding_digest(binding)
        projection = {
            "review_contract_version": "memory-candidate-review-v1",
            "origin": "operator_cli",
            "decision_contract_version": "memory-candidate-decision-v1",
            "decision": "approve",
            "candidate_key": "C" * 32,
        }
        expected = hashlib.sha256(json.dumps(
            projection,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")).digest()
        self.assertEqual(digest, expected)
        self.assertEqual(
            digest,
            memory_candidate_decision_ledger.binding_digest(binding),
        )
        future_projection = {
            **projection,
            "decision_contract_version": "memory-candidate-decision-v2",
        }
        self.assertNotEqual(
            digest,
            hashlib.sha256(json.dumps(
                future_projection,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")).digest(),
        )
        for changed in (
            dataclasses.replace(binding, candidate_key="D" * 32),
            dataclasses.replace(binding, decision="reject"),
            dataclasses.replace(binding, origin="mcp"),
        ):
            self.assertNotEqual(
                digest,
                memory_candidate_decision_ledger.binding_digest(changed),
            )

        tampered = self.binding()
        object.__setattr__(
            tampered,
            "decision_contract_version",
            "memory-candidate-decision-v2",
        )
        with self.assertRaisesRegex(
            memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError,
            "^invalid_candidate_decision_request$",
        ):
            memory_candidate_decision_ledger.binding_digest(tampered)

    def test_lookup_recognizes_exact_replay_and_rejects_binding_conflict(self):
        binding = self.binding()
        with channel_store.connect(self.path) as conn:
            memory_id = self.add_memory(conn)
            self.insert_decision(
                conn,
                memory_id=memory_id,
                binding=binding,
            )
            row = memory_candidate_decision_ledger.lookup_request(
                conn, binding.request_id
            )
        self.assertIsNotNone(row)
        self.assertIs(
            memory_candidate_decision_ledger.validate_replay_binding(
                row, binding
            ),
            row,
        )
        self.assertEqual(repr(row), "<CandidateDecisionLedgerRowV1>")
        self.assertNotIn(binding.request_id, repr(row))
        self.assertNotIn("memory_id=", repr(row))

        for changed in (
            dataclasses.replace(binding, candidate_key="D" * 32),
            dataclasses.replace(binding, decision="reject"),
            dataclasses.replace(binding, origin="mcp"),
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(
                memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError,
                "^candidate_decision_request_conflict$",
            ):
                memory_candidate_decision_ledger.validate_replay_binding(
                    row, changed
                )
        version_drift = dataclasses.replace(
            row,
            decision_contract_version="memory-candidate-decision-v2",
        )
        with self.assertRaisesRegex(
            memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError,
            "^candidate_decision_request_conflict$",
        ):
            memory_candidate_decision_ledger.validate_replay_binding(
                version_drift, binding
            )

    def test_ledger_has_no_plaintext_provenance_or_explicit_action_fields(self):
        with channel_store.connect(self.path) as conn:
            columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_xinfo(memory_candidate_decisions)"
                )
            }
        forbidden = {
            "candidate_key",
            "plaintext",
            "content",
            "canonical_message_id",
            "span_start",
            "span_end",
            "fingerprint",
            "reviewer_prose",
            "reason",
            "provider",
            "model",
        }
        self.assertTrue(columns.isdisjoint(forbidden))
        source = inspect.getsource(memory_candidate_decision_ledger)
        self.assertNotIn("MemoryRuntime", source)
        self.assertNotIn("MemoryStore", source)
        self.assertNotIn("PrivilegedMemoryActions", source)
        self.assertNotIn("BEGIN ", source)
        self.assertNotIn("UPDATE memory_items", source)
        self.assertNotIn("INSERT INTO memory_suppressions", source)

    def test_errors_are_closed_and_data_free(self):
        error = (
            memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError(
                "invalid_candidate_key"
            )
        )
        self.assertEqual(str(error), "invalid_candidate_key")
        self.assertEqual(
            repr(error),
            "MemoryCandidateDecisionLedgerError('invalid_candidate_key')",
        )
        unknown = (
            memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError(
                "R" * 32
            )
        )
        self.assertEqual(str(unknown), "candidate_decision_state_invalid")
        self.assertNotIn("R" * 32, repr(unknown))


class CandidateDecisionProfileAndReviewTests(CandidateDecisionTestBase):
    def profile_store(self, *, secret: str = TEST_SECRET):
        store = object.__new__(memory_store.MemoryStore)
        store.path = self.path
        store._runtime_policy = SimpleNamespace(
            fingerprint_key_id="decision-test-key",
            fingerprint_hmac_secret=secret,
            normalization_version=memory_policy.NORMALIZATION_VERSION,
            fingerprint_version=memory_policy.FINGERPRINT_VERSION,
        )
        return store

    def test_profile_initialization_and_mismatch_rules_are_preserved(self):
        store = self.profile_store()
        with channel_store.connect(self.path) as conn:
            store._validate_or_initialize_profile(conn, initialize=True)
            first = tuple(conn.execute(
                "SELECT * FROM memory_fingerprint_profile"
            ).fetchone())
            store._validate_or_initialize_profile(conn, initialize=True)
            second = tuple(conn.execute(
                "SELECT * FROM memory_fingerprint_profile"
            ).fetchone())
        self.assertEqual(first, second)

        wrong = self.profile_store(secret=TEST_SECRET + "-different")
        with channel_store.connect(self.path) as conn, self.assertRaisesRegex(
            memory_store.MemoryStoreError,
            "^memory_fingerprint_profile_mismatch$",
        ):
            wrong._validate_or_initialize_profile(conn, initialize=True)

    def test_terminal_decision_state_blocks_missing_profile_reinitialization(self):
        self.assertIn(
            "memory_candidate_decisions", memory_store._PROFILE_STATE_TABLES
        )
        binding = self.binding()
        with channel_store.connect(self.path) as conn:
            memory_id = self.add_memory(conn)
            self.insert_decision(
                conn,
                memory_id=memory_id,
                binding=binding,
            )

        raw = sqlite3.connect(self.path)
        try:
            raw.execute("PRAGMA foreign_keys=OFF")
            raw.execute("DELETE FROM memory_items")
            raw.commit()
        finally:
            raw.close()

        store = self.profile_store()
        with channel_store.connect(self.path) as conn, self.assertRaisesRegex(
            memory_store.MemoryStoreError,
            "^memory_fingerprint_profile_mismatch$",
        ):
            store._validate_or_initialize_profile(conn, initialize=True)
        with channel_store.connect(self.path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM memory_fingerprint_profile"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM memory_candidate_decisions"
                ).fetchone()[0],
                1,
            )

    def test_4db_review_surface_remains_exactly_read_only(self):
        public_callables = {
            name
            for name, value in inspect.getmembers(
                memory_candidate_review_adapters.MemoryCandidateReviewAdapter
            )
            if not name.startswith("_") and callable(value)
        }
        self.assertEqual(
            public_callables,
            {"list_candidates", "get_candidate"},
        )
        for forbidden in (
            "approve_candidate",
            "reject_candidate",
            "decision",
            "promote",
        ):
            self.assertFalse(
                hasattr(
                    memory_candidate_review_adapters.MemoryCandidateReviewAdapter,
                    forbidden,
                )
            )
        self.assertEqual(
            memory_candidate_review.CANDIDATE_REVIEW_CONTRACT_VERSION,
            "memory-candidate-review-v1",
        )


if __name__ == "__main__":
    unittest.main()
