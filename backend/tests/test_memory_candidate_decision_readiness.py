from __future__ import annotations

import dataclasses
import importlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import (
    channel_store,
    memory_candidate_integrity,
    memory_policy,
    memory_service,
    memory_store,
)
from backend.tests.test_memory_candidate_persistence import (
    TEST_SECRET,
    bootstrap,
    candidate_config,
)


class CandidateDecisionWriterReadinessTests(unittest.TestCase):
    def setUp(self):
        global channel_store, memory_candidate_integrity, memory_policy
        global memory_service, memory_store

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "readiness.sqlite3")
        with channel_store.connect(self.path) as conn:
            conn.execute("""CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')""")
        channel_store.run_migrations(self.path)
        config = dataclasses.replace(
            candidate_config(),
            candidate_review_enabled=True,
            candidate_decisions_enabled=True,
        )
        stamp = channel_store.now_iso()
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO memory_fingerprint_profile
                   (singleton,key_id,key_check,normalization_version,
                    fingerprint_version,created_at,updated_at)
                   VALUES(1,?,?,?,?,?,?)""",
                (
                    config.fingerprint_key_id,
                    memory_policy.fingerprint_profile_check(TEST_SECRET),
                    memory_policy.NORMALIZATION_VERSION,
                    memory_policy.FINGERPRINT_VERSION,
                    stamp,
                    stamp,
                ),
            )
        self.runtime = bootstrap(self.path, config)
        channel_store = importlib.import_module("backend.channel_store")
        memory_candidate_integrity = importlib.import_module(
            "backend.memory_candidate_integrity"
        )
        memory_policy = importlib.import_module("backend.memory_policy")
        memory_service = importlib.import_module("backend.memory_service")
        memory_store = importlib.import_module("backend.memory_store")
        self.writer = self.runtime.candidate_decisions

    def test_enabled_exact_runtime_schema_and_profile_are_ready(self):
        self.assertIsInstance(self.writer, memory_service.CandidateDecisionWriter)
        self.assertEqual(self.writer.readiness(), (True, ""))

    def test_disabled_configuration_and_runtime_authority_map_stably(self):
        policy = self.writer._store._runtime_policy
        object.__setattr__(policy, "candidate_decisions_enabled", False)
        self.assertEqual(
            self.writer.readiness(),
            (False, "candidate_decisions_disabled"),
        )
        object.__setattr__(policy, "candidate_decisions_enabled", True)
        object.__setattr__(policy, "configuration_valid", False)
        self.assertEqual(
            self.writer.readiness(),
            (False, "candidate_decision_configuration_invalid"),
        )
        object.__setattr__(policy, "configuration_valid", True)
        self.writer._store._authority = object()
        self.assertEqual(
            self.writer.readiness(),
            (False, "runtime_authority_invalid"),
        )

    def test_fingerprint_constant_drift_is_configuration_invalid(self):
        policy = self.writer._store._runtime_policy
        object.__setattr__(
            policy,
            "fingerprint_version",
            memory_policy.FINGERPRINT_VERSION + 1,
        )
        self.assertEqual(
            self.writer.readiness(),
            (False, "candidate_decision_configuration_invalid"),
        )

    def test_schema_drift_is_schema_invalid(self):
        with channel_store.connect(self.path) as conn:
            conn.execute(
                "DROP TRIGGER memory_candidate_decisions_immutable_update"
            )
        self.assertEqual(
            self.writer.readiness(),
            (False, "candidate_decision_schema_invalid"),
        )

    def test_missing_and_wrong_profile_are_profile_mismatch(self):
        with channel_store.connect(self.path) as conn:
            conn.execute("DELETE FROM memory_fingerprint_profile")
        self.assertEqual(
            self.writer.readiness(),
            (False, "candidate_decision_profile_mismatch"),
        )
        stamp = channel_store.now_iso()
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO memory_fingerprint_profile
                   (singleton,key_id,key_check,normalization_version,
                    fingerprint_version,created_at,updated_at)
                   VALUES(1,'wrong-key',?,1,1,?,?)""",
                (
                    memory_policy.fingerprint_profile_check(TEST_SECRET),
                    stamp,
                    stamp,
                ),
            )
        self.assertEqual(
            self.writer.readiness(),
            (False, "candidate_decision_profile_mismatch"),
        )

    def test_readiness_connection_is_read_only(self):
        original = channel_store.validate_memory_candidate_decision_schema_v1_v10
        observed = []

        def validate(conn):
            observed.append(conn.execute("PRAGMA query_only").fetchone()[0])
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute(
                    "UPDATE memory_fingerprint_profile SET key_id='tampered'"
                )
            original(conn)

        with mock.patch.object(
            channel_store,
            "validate_memory_candidate_decision_schema_v1_v10",
            new=validate,
        ):
            self.assertEqual(self.writer.readiness(), (True, ""))
        self.assertEqual(observed, [1])

    def test_candidate_rows_are_not_scanned_and_corruption_is_not_readiness(self):
        stamp = channel_store.now_iso()
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO memory_items
                   (memory_key,kind,scope_type,scope_ref,normalized_content,
                    normalized_fingerprint,fingerprint_version,status,
                    explicitness,confidence,sensitivity,first_observed_at,
                    last_confirmed_at,created_at,updated_at)
                   VALUES(?,'project','global_user','',
                          'corrupt candidate plaintext',zeroblob(32),1,
                          'candidate','inferred',0.0,'normal',?,?,?,?)""",
                ("Z" * 32, stamp, stamp, stamp, stamp),
            )
        with (
            mock.patch.object(
                memory_candidate_integrity.AutomaticCandidateIntegrityVerifier,
                "verify_pending_candidate",
                side_effect=AssertionError("candidate row scanned"),
            ),
            mock.patch.object(
                memory_candidate_integrity.AutomaticCandidateIntegrityVerifier,
                "verify_approved_memory",
                side_effect=AssertionError("approved row scanned"),
            ),
            mock.patch.object(
                memory_candidate_integrity.AutomaticCandidateIntegrityVerifier,
                "verify_rejected_memory",
                side_effect=AssertionError("rejected row scanned"),
            ),
        ):
            self.assertEqual(self.writer.readiness(), (True, ""))

    def test_missing_database_is_storage_unavailable(self):
        self.writer._store.path = str(Path(self.temp.name) / "missing.sqlite3")
        self.assertEqual(
            self.writer.readiness(),
            (False, "storage_unavailable"),
        )

    def test_unexpected_store_error_detail_is_closed(self):
        detail = "sensitive-database-path-and-secret"
        with mock.patch.object(
            self.writer._store,
            "candidate_decision_readiness",
            side_effect=memory_store.MemoryStoreError(detail),
        ):
            result = self.writer.readiness()
        self.assertEqual(result, (False, "candidate_decision_state_invalid"))
        self.assertNotIn(detail, repr(result))


if __name__ == "__main__":
    unittest.main()
