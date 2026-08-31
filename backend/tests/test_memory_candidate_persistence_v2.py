from __future__ import annotations

import dataclasses
import importlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend import (
    channel_store,
    deployment_config,
    memory_candidate_integrity,
    memory_formation,
    memory_formation_v2,
    memory_policy,
)
from backend.tests._support import NoNetworkMixin


TEST_SECRET = "Synthetic-Candidate-HMAC-Key-2026-Alpha!Z9q7"
KEY_ID = "candidate-persistence-test-key"


def candidate_config() -> deployment_config.MemoryConfig:
    return deployment_config.MemoryConfig(
        enabled=True,
        context_injection_enabled=False,
        smart_retrieval_enabled=False,
        explicit_writes_enabled=False,
        sensitive_storage_enabled=False,
        max_item_chars=1000,
        forget_retention_policy="tombstone_without_content",
        fingerprint_key_id=KEY_ID,
        fingerprint_hmac_secret=TEST_SECRET,
        configuration_valid=True,
        error_category="",
        auto_formation_enabled=True,
        auto_candidate_persistence_enabled=True,
    )


def bootstrap(path: str):
    memory_runtime = importlib.reload(importlib.import_module("backend.memory_runtime"))
    memory_store = importlib.reload(importlib.import_module("backend.memory_store"))
    memory_service = importlib.reload(importlib.import_module("backend.memory_service"))
    deployment = dataclasses.replace(
        deployment_config.load_deployment_config(
            SimpleNamespace(requested=False, enabled=False),
            {"TELEGRAM_ENABLED": "false", "RELAY_DB": path},
        ),
        memory=candidate_config(),
    )
    with mock.patch.object(
        deployment_config,
        "load_deployment_config",
        return_value=deployment,
    ):
        runtime = memory_runtime.bootstrap_memory_runtime_from_environment(object())
    # Reload the unwired V2 module after memory_service/memory_store so its exact
    # capability type bindings match the fresh runtime used by this test.
    persistence_v2 = importlib.reload(
        importlib.import_module("backend.memory_candidate_persistence_v2")
    )
    integrity_v2 = importlib.reload(
        importlib.import_module("backend.memory_candidate_integrity_v2")
    )
    return runtime, memory_store, persistence_v2, integrity_v2


def span(source: str, part: str) -> memory_formation_v2.AutoMemorySourceSpanV2:
    start = source.index(part)
    return memory_formation_v2.AutoMemorySourceSpanV2(start, start + len(part))


def proposal(
    source: str,
    signal_type: str,
    *parts: str,
) -> memory_formation_v2.AutoMemoryProposalV2:
    return memory_formation_v2.AutoMemoryProposalV2(
        signal_type,
        tuple(span(source, part) for part in parts),
    )


class MemoryCandidatePersistenceV2Tests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "candidate-v2.sqlite3")
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """CREATE TABLE messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                    text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')"""
            )
        channel_store.run_migrations(self.path)
        (
            self.runtime,
            self.memory_store,
            self.persistence_v2_module,
            self.integrity_v2_module,
        ) = bootstrap(self.path)
        self.persistence = self.persistence_v2_module.bind_candidate_persistence_v2(
            self.runtime.candidate_persistence
        )

    def message(self, text: str) -> int:
        with channel_store.connect(self.path) as conn:
            cursor = conn.execute(
                """INSERT INTO messages(ts,direction,kind,text,meta)
                   VALUES(?,?,?,?,?)""",
                (
                    channel_store.now_iso(),
                    "in",
                    "user",
                    text,
                    json.dumps(
                        {"channel": "web", "source": "relay"},
                        separators=(",", ":"),
                    ),
                ),
            )
            return int(cursor.lastrowid)

    def persist(self, source: str, item, *, message_id: int | None = None):
        return self.persistence.persist(
            canonical_message_id=message_id or self.message(source),
            source_text=source,
            proposals=(item,),
        )

    def counts(self) -> dict[str, int]:
        tables = (
            "memory_items",
            "memory_candidate_sources",
            "memory_auto_formation_runs",
            "memory_fingerprint_profile",
        )
        with channel_store.connect(self.path) as conn:
            return {
                table: int(conn.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0])
                for table in tables
            }

    def pending_row(self):
        with channel_store.connect(self.path) as conn:
            return conn.execute(
                f"""SELECT {memory_candidate_integrity.AUTOMATIC_MEMORY_COLUMNS}
                      FROM memory_items WHERE status='candidate'"""
            ).fetchone()

    def verifier(self):
        return self.integrity_v2_module.AutomaticCandidateIntegrityVerifierV2(
            fingerprint_key_id=KEY_ID,
            fingerprint_hmac_secret=TEST_SECRET,
            max_item_chars=1000,
        )

    def verify_pending(self):
        with channel_store.connect(self.path) as conn:
            row = conn.execute(
                f"""SELECT {memory_candidate_integrity.AUTOMATIC_MEMORY_COLUMNS}
                      FROM memory_items WHERE status='candidate'"""
            ).fetchone()
            if row is None:
                raise AssertionError("missing candidate")
            return self.verifier().verify_pending_candidate(conn, row)

    def assert_v2_error(self, category: str, callable_, *args, **kwargs):
        with self.assertRaises(
            self.persistence_v2_module.MemoryCandidatePersistenceV2Error
        ) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.category, category)
        self.assertNotIn("Project Atlas", repr(raised.exception))
        return raised.exception

    def test_two_span_candidate_is_one_memory_with_two_immutable_source_rows(self):
        source = (
            "Project Atlas uses PostgreSQL 16. "
            "UNSELECTED FILLER. "
            "The project runs on port 5432."
        )
        item = proposal(
            source,
            "project_fact",
            "Project Atlas uses PostgreSQL 16.",
            "The project runs on port 5432.",
        )
        message_id = self.message(source)
        result = self.persist(source, item, message_id=message_id)
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.proposal_count, 1)
        self.assertEqual(result.candidate_count, 1)
        self.assertEqual(result.created_count, 1)
        self.assertFalse(result.replayed)
        self.assertEqual(
            self.counts(),
            {
                "memory_items": 1,
                "memory_candidate_sources": 2,
                "memory_auto_formation_runs": 1,
                "memory_fingerprint_profile": 1,
            },
        )

        with channel_store.connect(self.path) as conn:
            memory = conn.execute(
                "SELECT id,kind,normalized_content,status FROM memory_items"
            ).fetchone()
            rows = conn.execute(
                """SELECT canonical_message_id,signal_type,span_start,span_end,
                          formation_contract_version,extractor_contract_version
                     FROM memory_candidate_sources ORDER BY span_start"""
            ).fetchall()
            run = conn.execute(
                "SELECT * FROM memory_auto_formation_runs"
            ).fetchone()
        self.assertEqual(memory["kind"], "project")
        self.assertEqual(memory["status"], "candidate")
        self.assertEqual(
            memory["normalized_content"],
            "Project Atlas uses PostgreSQL 16. The project runs on port 5432.",
        )
        self.assertNotIn("UNSELECTED", memory["normalized_content"])
        for literal in ("Atlas", "PostgreSQL", "16", "5432"):
            self.assertIn(literal, memory["normalized_content"])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["canonical_message_id"] == message_id for row in rows))
        self.assertTrue(all(row["signal_type"] == "project_fact" for row in rows))
        self.assertTrue(all(
            row["formation_contract_version"] == "memory-formation-v2"
            for row in rows
        ))
        self.assertTrue(all(
            row["extractor_contract_version"] == "memory-formation-extractor-v2"
            for row in rows
        ))
        self.assertEqual(run["formation_contract_version"], "memory-formation-v2")
        self.assertEqual(
            run["extractor_contract_version"],
            "memory-formation-extractor-v2",
        )
        self.assertEqual(run["proposal_count"], 1)
        self.assertEqual(run["candidate_count"], 1)

        with channel_store.connect(self.path) as conn:
            source_id = conn.execute(
                "SELECT id FROM memory_candidate_sources ORDER BY id LIMIT 1"
            ).fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE memory_candidate_sources SET span_end=span_end+1 WHERE id=?",
                    (source_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM memory_candidate_sources WHERE id=?",
                    (source_id,),
                )

    def test_v2_integrity_rebuilds_multi_span_candidate_as_one_atomic_fact(self):
        source = (
            "Project Atlas uses PostgreSQL 16. filler filler. "
            "The project runs on port 5432."
        )
        item = proposal(
            source,
            "project_fact",
            "Project Atlas uses PostgreSQL 16.",
            "The project runs on port 5432.",
        )
        self.persist(source, item)
        verified = self.verify_pending()
        self.assertEqual(verified.kind, "project")
        self.assertEqual(
            verified.content,
            "Project Atlas uses PostgreSQL 16. The project runs on port 5432.",
        )
        self.assertEqual(len(verified.evidence), 2)
        self.assertEqual(
            [evidence.signal_type for evidence in verified.evidence],
            ["project_fact", "project_fact"],
        )
        self.assertTrue(all(
            evidence.formation_contract_version == "memory-formation-v2"
            for evidence in verified.evidence
        ))

    def test_exact_replay_returns_prior_terminal_run_without_duplicate_rows(self):
        source = (
            "Project Atlas uses Python. filler. "
            "The project runs on Render."
        )
        item = proposal(
            source,
            "project_fact",
            "Project Atlas uses Python.",
            "The project runs on Render.",
        )
        message_id = self.message(source)
        first = self.persist(source, item, message_id=message_id)
        before = self.counts()
        second = self.persist(source, item, message_id=message_id)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(before, self.counts())

    def test_changed_v2_ranges_on_same_canonical_message_fail_replay_closed(self):
        source = (
            "Project Atlas uses Python. "
            "The project runs on Render. "
            "The project uses SQLite."
        )
        first = proposal(
            source,
            "project_fact",
            "Project Atlas uses Python.",
            "The project runs on Render.",
        )
        changed = proposal(
            source,
            "project_fact",
            "Project Atlas uses Python.",
            "The project uses SQLite.",
        )
        message_id = self.message(source)
        self.persist(source, first, message_id=message_id)
        before = self.counts()
        self.assert_v2_error(
            "formation_replay_conflict",
            self.persist,
            source,
            changed,
            message_id=message_id,
        )
        self.assertEqual(before, self.counts())

    def test_same_pending_memory_accepts_multiple_v2_evidence_bundles(self):
        first_source = (
            "Project Atlas uses Python. filler-A. "
            "The project runs on Render."
        )
        second_source = (
            "prefix. Project Atlas uses Python. filler-B. "
            "The project runs on Render. suffix."
        )
        first = proposal(
            first_source,
            "project_fact",
            "Project Atlas uses Python.",
            "The project runs on Render.",
        )
        second = proposal(
            second_source,
            "project_fact",
            "Project Atlas uses Python.",
            "The project runs on Render.",
        )
        first_result = self.persist(first_source, first)
        second_result = self.persist(second_source, second)
        self.assertEqual(first_result.created_count, 1)
        self.assertEqual(second_result.created_count, 0)
        self.assertEqual(second_result.existing_candidate_count, 1)
        self.assertEqual(self.counts()["memory_items"], 1)
        self.assertEqual(self.counts()["memory_candidate_sources"], 4)
        self.assertEqual(self.counts()["memory_auto_formation_runs"], 2)
        verified = self.verify_pending()
        self.assertEqual(len(verified.evidence), 4)
        self.assertEqual(
            len({evidence.canonical_message_id for evidence in verified.evidence}),
            2,
        )

    def test_missing_one_v2_span_fails_integrity_rebuild_closed(self):
        source = (
            "Project Atlas uses Python. filler. "
            "The project runs on Render."
        )
        item = proposal(
            source,
            "project_fact",
            "Project Atlas uses Python.",
            "The project runs on Render.",
        )
        self.persist(source, item)
        self.verify_pending()
        with channel_store.connect(self.path) as conn:
            conn.execute("DROP TRIGGER memory_candidate_sources_immutable_delete")
            source_id = conn.execute(
                "SELECT id FROM memory_candidate_sources ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            conn.execute(
                "DELETE FROM memory_candidate_sources WHERE id=?",
                (source_id,),
            )
        with channel_store.connect(self.path) as conn:
            row = conn.execute(
                f"""SELECT {memory_candidate_integrity.AUTOMATIC_MEMORY_COLUMNS}
                      FROM memory_items WHERE status='candidate'"""
            ).fetchone()
            with self.assertRaises(
                memory_candidate_integrity.AutomaticCandidateIntegrityError
            ) as raised:
                self.verifier().verify_pending_candidate(conn, row)
        self.assertEqual(raised.exception.category, "candidate_integrity_invalid")

    def test_invalid_or_ambiguous_v2_batch_is_zero_write(self):
        source = "Project Atlas uses Python. Project Atlas uses Python."
        first_start = source.index("Project Atlas uses Python.")
        second_start = source.rindex("Project Atlas uses Python.")
        duplicate_memory_proposals = (
            memory_formation_v2.AutoMemoryProposalV2(
                "project_fact",
                (memory_formation_v2.AutoMemorySourceSpanV2(
                    first_start,
                    first_start + len("Project Atlas uses Python."),
                ),),
            ),
            memory_formation_v2.AutoMemoryProposalV2(
                "project_fact",
                (memory_formation_v2.AutoMemorySourceSpanV2(
                    second_start,
                    second_start + len("Project Atlas uses Python."),
                ),),
            ),
        )
        message_id = self.message(source)
        before = self.counts()
        self.assert_v2_error(
            "candidate_state_conflict",
            self.persistence.persist,
            canonical_message_id=message_id,
            source_text=source,
            proposals=duplicate_memory_proposals,
        )
        self.assertEqual(before, self.counts())

        overlapping = memory_formation_v2.AutoMemoryProposalV2(
            "project_fact",
            (
                memory_formation_v2.AutoMemorySourceSpanV2(0, 20),
                memory_formation_v2.AutoMemorySourceSpanV2(10, 30),
            ),
        )
        second_message = self.message(source)
        before = self.counts()
        self.assert_v2_error(
            "overlapping_spans",
            self.persistence.persist,
            canonical_message_id=second_message,
            source_text=source,
            proposals=(overlapping,),
        )
        self.assertEqual(before, self.counts())

    def test_v1_run_on_same_message_blocks_v2_without_mutating_v1_state(self):
        source = "Project Atlas uses Python."
        message_id = self.message(source)
        v1 = memory_formation.AutoMemoryProposalV1(
            "project_fact",
            0,
            len(source),
        )
        self.runtime.candidate_persistence.persist(
            canonical_message_id=message_id,
            source_text=source,
            proposals=(v1,),
            formation_contract_version="memory-formation-v1",
            extractor_contract_version="memory-formation-extractor-v1",
        )
        before = self.counts()
        self.assert_v2_error(
            "formation_replay_conflict",
            self.persistence.persist,
            canonical_message_id=message_id,
            source_text=source,
            proposals=(proposal(source, "project_fact", source),),
        )
        self.assertEqual(before, self.counts())
        with channel_store.connect(self.path) as conn:
            run = conn.execute(
                "SELECT formation_contract_version FROM memory_auto_formation_runs"
            ).fetchone()
        self.assertEqual(run["formation_contract_version"], "memory-formation-v1")

    def test_v2_verifier_delegates_unchanged_for_v1_only_candidate(self):
        source = "Project Atlas uses Python."
        message_id = self.message(source)
        v1 = memory_formation.AutoMemoryProposalV1(
            "project_fact",
            0,
            len(source),
        )
        self.runtime.candidate_persistence.persist(
            canonical_message_id=message_id,
            source_text=source,
            proposals=(v1,),
            formation_contract_version="memory-formation-v1",
            extractor_contract_version="memory-formation-extractor-v1",
        )
        verified = self.verify_pending()
        self.assertEqual(verified.content, source)
        self.assertEqual(len(verified.evidence), 1)
        self.assertEqual(
            verified.evidence[0].formation_contract_version,
            "memory-formation-v1",
        )

    def test_existing_schema_is_reused_without_new_migration_or_group_table(self):
        with channel_store.connect(self.path) as conn:
            versions = [
                int(row[0])
                for row in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertEqual(max(versions), 10)
        self.assertIn("memory_candidate_sources", tables)
        self.assertNotIn("memory_candidate_source_groups", tables)
        self.assertNotIn("memory_candidate_spans_v2", tables)


if __name__ == "__main__":
    unittest.main()
