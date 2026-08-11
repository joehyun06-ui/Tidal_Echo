from __future__ import annotations

import dataclasses
import inspect
import json
import tempfile
import unittest
from pathlib import Path

from backend import (
    channel_store,
    memory_candidate_integrity,
    memory_candidate_review,
)
from backend.tests.test_memory_candidate_persistence import (
    TEST_SECRET,
    bootstrap,
    candidate_config,
)


class AutomaticCandidateIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "integrity.sqlite3")
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
        self.runtime = bootstrap(self.path, config)
        self.persistence = self.runtime.candidate_persistence
        self.verifier = (
            memory_candidate_integrity.AutomaticCandidateIntegrityVerifier(
                fingerprint_key_id="candidate-persistence-test-key",
                fingerprint_hmac_secret=TEST_SECRET,
                max_item_chars=1000,
            )
        )
        self.content = "Project Atlas uses Python."
        message_id = self.message(self.content)
        from backend import memory_formation

        self.persistence.persist(
            canonical_message_id=message_id,
            source_text=self.content,
            proposals=(memory_formation.AutoMemoryProposalV1(
                "project_fact",
                0,
                len(self.content),
            ),),
            formation_contract_version="memory-formation-v1",
            extractor_contract_version="memory-formation-extractor-v1",
        )
        with channel_store.connect(self.path) as conn:
            self.candidate_key = conn.execute(
                "SELECT memory_key FROM memory_items"
            ).fetchone()[0]

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

    def row(self, conn):
        return conn.execute(
            f"""SELECT {
                memory_candidate_integrity.AUTOMATIC_MEMORY_COLUMNS
            } FROM memory_items WHERE memory_key=?""",
            (self.candidate_key,),
        ).fetchone()

    def test_module_is_neutral_read_only_and_has_only_fixed_modes(self):
        source = inspect.getsource(memory_candidate_integrity)
        for forbidden in (
            "memory_candidate_review",
            "memory_candidate_decision_ledger",
            "memory_store",
            "memory_runtime",
            "PrivilegedMemoryActions",
            "BEGIN IMMEDIATE",
            "INSERT INTO",
            "UPDATE memory_",
            "DELETE FROM",
        ):
            self.assertNotIn(forbidden, source)
        public_verify = {
            name
            for name in memory_candidate_integrity.AutomaticCandidateIntegrityVerifier.__dict__
            if name.startswith("verify")
        }
        self.assertEqual(public_verify, {
            "verify_profile",
            "verify_pending_candidate",
            "verify_approved_memory",
            "verify_rejected_memory",
        })

    def test_pending_proof_returns_frozen_repr_safe_models(self):
        with channel_store.connect(self.path) as conn:
            self.verifier.verify_profile(conn)
            verified = self.verifier.verify_pending_candidate(
                conn,
                self.row(conn),
            )
        self.assertEqual(verified.candidate_key, self.candidate_key)
        self.assertEqual(verified.content, self.content)
        self.assertEqual(verified.confidence, 0.0)
        self.assertEqual(verified.explicitness, "inferred")
        self.assertEqual(len(verified.evidence), 1)
        self.assertEqual(repr(verified), "<VerifiedAutomaticMemoryV1>")
        self.assertEqual(
            repr(verified.evidence[0]),
            "<VerifiedAutomaticEvidenceV1>",
        )
        for secret in (
            verified.candidate_key,
            verified.content,
            verified.fingerprint.hex(),
            verified.evidence[0].source_excerpt,
        ):
            self.assertNotIn(secret, repr(verified))
            self.assertNotIn(secret, repr(verified.evidence[0]))

    def test_approved_and_rejected_modes_fix_terminal_semantics(self):
        with channel_store.connect(self.path) as conn:
            stamp = channel_store.now_iso()
            conn.execute(
                """UPDATE memory_items SET status='active',confidence=1.0,
                          last_confirmed_at=?,updated_at=?
                   WHERE memory_key=?""",
                (stamp, stamp, self.candidate_key),
            )
            approved = self.verifier.verify_approved_memory(
                conn,
                self.row(conn),
            )
            self.assertEqual(approved.confidence, 1.0)
            with self.assertRaises(
                memory_candidate_integrity.AutomaticCandidateIntegrityError
            ):
                self.verifier.verify_pending_candidate(conn, self.row(conn))

        other = str(Path(self.temp.name) / "rejected.sqlite3")
        with channel_store.connect(other) as conn:
            conn.execute("""CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')""")
        channel_store.run_migrations(other)
        config = dataclasses.replace(
            candidate_config(),
            candidate_review_enabled=True,
            candidate_decisions_enabled=True,
        )
        runtime = bootstrap(other, config)
        with channel_store.connect(other) as conn:
            cursor = conn.execute(
                """INSERT INTO messages(ts,direction,kind,text,meta)
                   VALUES(?,?,?,?,?)""",
                (channel_store.now_iso(), "in", "user", self.content, "{}"),
            )
            message_id = int(cursor.lastrowid)
        from backend import memory_formation

        runtime.candidate_persistence.persist(
            canonical_message_id=message_id,
            source_text=self.content,
            proposals=(memory_formation.AutoMemoryProposalV1(
                "project_fact", 0, len(self.content)
            ),),
            formation_contract_version="memory-formation-v1",
            extractor_contract_version="memory-formation-extractor-v1",
        )
        with channel_store.connect(other) as conn:
            stamp = channel_store.now_iso()
            conn.execute(
                "UPDATE memory_items SET status='rejected',updated_at=?",
                (stamp,),
            )
            row = conn.execute(
                f"SELECT {memory_candidate_integrity.AUTOMATIC_MEMORY_COLUMNS} "
                "FROM memory_items"
            ).fetchone()
            rejected = self.verifier.verify_rejected_memory(conn, row)
            self.assertEqual(rejected.confidence, 0.0)

    def test_profile_and_provenance_fail_closed_with_neutral_categories(self):
        with channel_store.connect(self.path) as conn:
            conn.execute("DELETE FROM memory_fingerprint_profile")
            with self.assertRaises(
                memory_candidate_integrity.AutomaticCandidateIntegrityError
            ) as profile:
                self.verifier.verify_profile(conn)
            self.assertEqual(
                profile.exception.category,
                "candidate_integrity_profile_mismatch",
            )
        with channel_store.connect(self.path) as conn:
            conn.execute(
                "DROP TRIGGER memory_candidate_sources_immutable_delete"
            )
            conn.execute("DELETE FROM memory_candidate_sources")
            with self.assertRaises(
                memory_candidate_integrity.AutomaticCandidateIntegrityError
            ) as provenance:
                self.verifier.verify_pending_candidate(conn, self.row(conn))
            self.assertEqual(
                provenance.exception.category,
                "candidate_provenance_missing",
            )

    def test_review_projects_the_shared_verifier_without_a_second_proof(self):
        review_source = inspect.getsource(memory_candidate_review)
        self.assertNotIn("build_auto_memory_candidates", review_source)
        self.assertNotIn("fingerprint_content", review_source)
        reader = memory_candidate_review.MemoryCandidateReviewReader(
            self.path,
            fingerprint_key_id="candidate-persistence-test-key",
            fingerprint_hmac_secret=TEST_SECRET,
            max_item_chars=1000,
        )
        self.assertIsInstance(
            reader._verifier,
            memory_candidate_integrity.AutomaticCandidateIntegrityVerifier,
        )
        service = memory_candidate_review.MemoryCandidateReviewService(
            reader,
            enabled=True,
            configuration_valid=True,
            error_category="",
        )
        self.assertEqual(
            service.get_candidate(self.candidate_key).content,
            self.content,
        )


if __name__ == "__main__":
    unittest.main()
