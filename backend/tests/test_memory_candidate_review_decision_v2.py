from __future__ import annotations

import dataclasses
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend import channel_store, deployment_config
from backend.tests._support import NoNetworkMixin
from backend.tests.test_memory_candidate_persistence import (
    bootstrap as bootstrap_memory,
    candidate_config,
)


TEST_SECRET = "Synthetic-Candidate-HMAC-Key-2026-Alpha!Z9q7"
KEY_ID = "candidate-persistence-test-key"


def config():
    return dataclasses.replace(
        candidate_config(),
        candidate_review_enabled=True,
        candidate_decisions_enabled=True,
    )


def deployment_for(path: str):
    base = deployment_config.load_deployment_config(
        SimpleNamespace(requested=False, enabled=False),
        {"TELEGRAM_ENABLED": "false", "RELAY_DB": path},
    )
    return dataclasses.replace(base, memory=config())


class CandidateReviewDecisionV2Tests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "review-decision-v2.sqlite3")
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """CREATE TABLE messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                    text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')"""
            )
        channel_store.run_migrations(self.path)
        self.runtime = bootstrap_memory(self.path, config())

        # Refresh module identities after the runtime bootstrap/reload seams.
        self.channel_store = importlib.import_module("backend.channel_store")
        self.memory_candidate_decision_ledger = importlib.import_module(
            "backend.memory_candidate_decision_ledger"
        )
        self.memory_candidate_review = importlib.import_module(
            "backend.memory_candidate_review"
        )
        self.memory_formation = importlib.import_module("backend.memory_formation")
        self.memory_formation_v2 = importlib.import_module(
            "backend.memory_formation_v2"
        )
        self.memory_policy = importlib.import_module("backend.memory_policy")
        self.persistence_v2_module = importlib.reload(
            importlib.import_module("backend.memory_candidate_persistence_v2")
        )
        self.integrity_v2_module = importlib.reload(
            importlib.import_module("backend.memory_candidate_integrity_v2")
        )
        self.review_v2_module = importlib.reload(
            importlib.import_module("backend.memory_candidate_review_v2")
        )
        self.decision_v2_module = importlib.reload(
            importlib.import_module("backend.memory_candidate_decision_v2")
        )

        self.persistence_v2 = (
            self.persistence_v2_module.bind_candidate_persistence_v2(
                self.runtime.candidate_persistence
            )
        )
        # Review deliberately is not composed here. The fingerprint profile is
        # initialized lazily by the first successful authorized Memory write,
        # and review must fail closed while that profile is absent.
        self.writer_v2 = self.decision_v2_module.bind_candidate_decision_writer_v2(
            self.runtime.candidate_decisions
        )

    def review_capabilities(self):
        """Compose review only after a test has initialized the Memory profile."""
        return self.review_v2_module.compose_candidate_review_capabilities_v2(
            deployment_for(self.path)
        )

    def message(self, text: str) -> int:
        with self.channel_store.connect(self.path) as conn:
            cursor = conn.execute(
                """INSERT INTO messages(ts,direction,kind,text,meta)
                   VALUES(?,?,?,?,?)""",
                (
                    self.channel_store.now_iso(),
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

    def v2_proposal(self, source: str, *parts: str):
        spans = []
        for part in parts:
            start = source.index(part)
            spans.append(self.memory_formation_v2.AutoMemorySourceSpanV2(
                start,
                start + len(part),
            ))
        return self.memory_formation_v2.AutoMemoryProposalV2(
            "project_fact",
            tuple(spans),
        )

    def persist_v2(self, source: str, *parts: str) -> str:
        result = self.persistence_v2.persist(
            canonical_message_id=self.message(source),
            source_text=source,
            proposals=(self.v2_proposal(source, *parts),),
        )
        self.assertEqual(result.outcome, "completed")
        expected_content = self.memory_policy.normalize_content(
            "\n".join(parts),
            max_chars=1000,
        )
        fingerprint = self.memory_policy.fingerprint_content(
            TEST_SECRET,
            scope_type="global_user",
            scope_ref="",
            kind="project",
            normalized_content=expected_content,
        )
        with self.channel_store.connect(self.path) as conn:
            row = conn.execute(
                """SELECT memory_key FROM memory_items
                   WHERE normalized_fingerprint=?""",
                (fingerprint,),
            ).fetchone()
        self.assertIsNotNone(row)
        return str(row["memory_key"])

    def persist_v1(self, source: str) -> str:
        result = self.runtime.candidate_persistence.persist(
            canonical_message_id=self.message(source),
            source_text=source,
            proposals=(self.memory_formation.AutoMemoryProposalV1(
                "project_fact",
                0,
                len(source),
            ),),
            formation_contract_version="memory-formation-v1",
            extractor_contract_version="memory-formation-extractor-v1",
        )
        self.assertEqual(result.outcome, "completed")
        with self.channel_store.connect(self.path) as conn:
            return str(conn.execute(
                "SELECT memory_key FROM memory_items ORDER BY id DESC LIMIT 1"
            ).fetchone()[0])

    def binding(
        self,
        candidate_key: str,
        *,
        request_number: int,
        decision: str,
    ):
        return self.memory_candidate_decision_ledger.CandidateDecisionLedgerBindingV1(
            request_id=f"{request_number:032d}",
            origin="operator_cli",
            decision=decision,
            candidate_key=candidate_key,
        )

    def state(self):
        tables = (
            "memory_items",
            "memory_candidate_sources",
            "memory_candidate_decisions",
            "memory_suppressions",
            "memory_auto_formation_runs",
        )
        with self.channel_store.connect(self.path) as conn:
            return {
                table: tuple(
                    tuple(row)
                    for row in conn.execute(
                        f"SELECT * FROM {table} ORDER BY rowid"
                    )
                )
                for table in tables
            }

    def row(self, key: str):
        with self.channel_store.connect(self.path) as conn:
            return dict(conn.execute(
                "SELECT * FROM memory_items WHERE memory_key=?",
                (key,),
            ).fetchone())

    def test_v2_review_lists_one_atomic_candidate_with_per_span_provenance(self):
        source = (
            "Project Atlas uses PostgreSQL 16. filler. "
            "The project runs on port 5432."
        )
        key = self.persist_v2(
            source,
            "Project Atlas uses PostgreSQL 16.",
            "The project runs on port 5432.",
        )
        review_v2 = self.review_capabilities()
        listed = review_v2.operator_cli.list_candidates()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].candidate_key, key)
        self.assertEqual(listed[0].kind, "project")
        self.assertEqual(listed[0].provenance_count, 2)

        detail = review_v2.operator_cli.get_candidate(key)
        self.assertEqual(
            detail.content,
            "Project Atlas uses PostgreSQL 16. The project runs on port 5432.",
        )
        self.assertEqual(detail.provenance_count, 2)
        self.assertEqual(len(detail.evidence), 2)
        self.assertTrue(all(
            evidence.formation_contract_version == "memory-formation-v2"
            for evidence in detail.evidence
        ))
        self.assertTrue(all(
            evidence.extractor_contract_version == "memory-formation-extractor-v2"
            for evidence in detail.evidence
        ))

    def test_v2_approve_uses_existing_terminal_ledger_and_replays_exactly(self):
        source = (
            "Project Atlas uses Python. filler. "
            "The project runs on Render."
        )
        key = self.persist_v2(
            source,
            "Project Atlas uses Python.",
            "The project runs on Render.",
        )
        binding = self.binding(key, request_number=1, decision="approve")
        before_sources = self.state()["memory_candidate_sources"]
        result = self.writer_v2.decide(binding=binding)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.result_category, "approved")
        self.assertEqual(result.resulting_status, "active")
        self.assertFalse(result.replayed)
        row = self.row(key)
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["confidence"], 1.0)
        after = self.state()
        self.assertEqual(after["memory_candidate_sources"], before_sources)
        self.assertEqual(len(after["memory_candidate_decisions"]), 1)
        self.assertEqual(len(after["memory_suppressions"]), 0)

        snapshot = self.state()
        replay = self.writer_v2.decide(binding=binding)
        self.assertTrue(replay.replayed)
        self.assertEqual(self.state(), snapshot)

    def test_v2_reject_preserves_evidence_and_creates_one_suppression(self):
        source = (
            "Project Atlas uses SQLite. filler. "
            "The project runs on port 9000."
        )
        key = self.persist_v2(
            source,
            "Project Atlas uses SQLite.",
            "The project runs on port 9000.",
        )
        before_sources = self.state()["memory_candidate_sources"]
        result = self.writer_v2.decide(
            binding=self.binding(key, request_number=2, decision="reject")
        )
        self.assertEqual(result.result_category, "rejected")
        self.assertEqual(result.resulting_status, "rejected")
        row = self.row(key)
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["confidence"], 0.0)
        state = self.state()
        self.assertEqual(state["memory_candidate_sources"], before_sources)
        self.assertEqual(len(state["memory_candidate_decisions"]), 1)
        self.assertEqual(len(state["memory_suppressions"]), 1)

    def test_corrupt_v2_evidence_fails_review_and_decision_without_mutation(self):
        source = (
            "Project Atlas uses Python. filler. "
            "The project runs on Render."
        )
        key = self.persist_v2(
            source,
            "Project Atlas uses Python.",
            "The project runs on Render.",
        )
        # Compose while the durable profile/evidence are valid; the reader is
        # dynamic, so later corruption must still be detected on the next read.
        review_v2 = self.review_capabilities()
        with self.channel_store.connect(self.path) as conn:
            conn.execute("DROP TRIGGER memory_candidate_sources_immutable_delete")
            source_id = conn.execute(
                """SELECT id FROM memory_candidate_sources
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()[0]
            conn.execute(
                "DELETE FROM memory_candidate_sources WHERE id=?",
                (source_id,),
            )
            conn.execute(
                self.channel_store.MEMORY_CANDIDATE_PERSISTENCE_TRIGGER_DDL[
                    "memory_candidate_sources_immutable_delete"
                ]
            )
        before = self.state()
        with self.assertRaises(
            self.memory_candidate_review.MemoryCandidateReviewError
        ) as review_error:
            review_v2.operator_cli.get_candidate(key)
        self.assertEqual(
            review_error.exception.category,
            "candidate_review_state_invalid",
        )
        with self.assertRaises(
            self.memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError
        ) as decision_error:
            self.writer_v2.decide(
                binding=self.binding(key, request_number=3, decision="approve")
            )
        self.assertEqual(
            decision_error.exception.category,
            "candidate_decision_state_invalid",
        )
        self.assertEqual(self.state(), before)

    def test_v2_review_and_decision_paths_remain_backward_compatible_with_v1(self):
        key = self.persist_v1("Project Atlas uses Python.")
        review_v2 = self.review_capabilities()
        detail = review_v2.operator_cli.get_candidate(key)
        self.assertEqual(detail.content, "Project Atlas uses Python.")
        self.assertEqual(detail.provenance_count, 1)
        result = self.writer_v2.decide(
            binding=self.binding(key, request_number=4, decision="approve")
        )
        self.assertEqual(result.result_category, "approved")
        self.assertEqual(self.row(key)["status"], "active")

    def test_deployed_v1_review_and_decision_fail_closed_on_multi_span_v2_until_wired(self):
        source = (
            "Project Atlas uses Python. filler. "
            "The project runs on Render."
        )
        key = self.persist_v2(
            source,
            "Project Atlas uses Python.",
            "The project runs on Render.",
        )
        reader = self.memory_candidate_review.MemoryCandidateReviewReader(
            self.path,
            fingerprint_key_id=KEY_ID,
            fingerprint_hmac_secret=TEST_SECRET,
            max_item_chars=1000,
        )
        deployed_review = self.memory_candidate_review.MemoryCandidateReviewService(
            reader,
            enabled=True,
            configuration_valid=True,
            error_category="",
        )
        with self.assertRaises(
            self.memory_candidate_review.MemoryCandidateReviewError
        ) as review_error:
            deployed_review.get_candidate(key)
        self.assertEqual(
            review_error.exception.category,
            "candidate_review_state_invalid",
        )

        before = self.state()
        with self.assertRaises(
            self.memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError
        ) as decision_error:
            self.runtime.candidate_decisions.decide(
                binding=self.binding(key, request_number=5, decision="approve")
            )
        self.assertEqual(
            decision_error.exception.category,
            "candidate_decision_state_invalid",
        )
        self.assertEqual(self.state(), before)

    def test_v2_capabilities_are_not_present_on_runtime_without_explicit_wiring(self):
        # Initialize the fingerprint profile through the real V1 write boundary;
        # readiness is intentionally false before any successful Memory write.
        self.persist_v1("Project Atlas uses Python.")
        review_v2 = self.review_capabilities()
        self.assertFalse(hasattr(self.runtime, "candidate_persistence_v2"))
        self.assertFalse(hasattr(self.runtime, "candidate_review_v2"))
        self.assertFalse(hasattr(self.runtime, "candidate_decisions_v2"))
        self.assertIsNot(self.writer_v2, self.runtime.candidate_decisions)
        self.assertIsNot(review_v2.service, None)
        self.assertEqual(self.writer_v2.readiness(), (True, ""))


if __name__ == "__main__":
    unittest.main()
