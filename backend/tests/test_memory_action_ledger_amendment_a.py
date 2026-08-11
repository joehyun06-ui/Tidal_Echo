from __future__ import annotations

import dataclasses
import importlib
import json
import tempfile
import unittest
from pathlib import Path

from backend import (
    channel_store,
    memory_candidate_decision_ledger,
    memory_explicit_actions,
    memory_formation,
    memory_policy,
)
from backend.tests.test_memory_candidate_persistence import (
    EXTRACTOR_VERSION,
    FORMATION_VERSION,
    TEST_SECRET,
    bootstrap,
    candidate_config,
)


class MemoryActionLedgerAmendmentATests(unittest.TestCase):
    def setUp(self):
        global channel_store
        global memory_candidate_decision_ledger
        global memory_explicit_actions, memory_formation, memory_policy

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "amendment-a.sqlite3")
        with channel_store.connect(self.path) as conn:
            conn.execute("""CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')""")
        channel_store.run_migrations(self.path)
        config = dataclasses.replace(
            candidate_config(writes=True),
            candidate_review_enabled=True,
            candidate_decisions_enabled=True,
        )
        self.runtime = bootstrap(self.path, config)
        # App-integration tests deliberately evict backend modules from
        # sys.modules.  Refresh this test's module references after bootstrap
        # so full-suite execution uses the same class identities as runtime.
        channel_store = importlib.import_module("backend.channel_store")
        memory_candidate_decision_ledger = importlib.import_module(
            "backend.memory_candidate_decision_ledger"
        )
        memory_explicit_actions = importlib.import_module(
            "backend.memory_explicit_actions"
        )
        memory_formation = importlib.import_module("backend.memory_formation")
        memory_policy = importlib.import_module("backend.memory_policy")
        self.persistence = self.runtime.candidate_persistence
        self.decisions = self.runtime.candidate_decisions
        backend = memory_explicit_actions.create_entry_backend(
            self.runtime.privileged_actions
        )
        self.explicit = memory_explicit_actions.bind_operator_cli(backend)
        self._decision_request = 0

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

    def approved(self, content: str) -> str:
        message_id = self.message(content)
        self.persistence.persist(
            canonical_message_id=message_id,
            source_text=content,
            proposals=(memory_formation.AutoMemoryProposalV1(
                "project_fact", 0, len(content)
            ),),
            formation_contract_version=FORMATION_VERSION,
            extractor_contract_version=EXTRACTOR_VERSION,
        )
        with channel_store.connect(self.path) as conn:
            key = conn.execute(
                """SELECT memory_key FROM memory_items
                   WHERE status='candidate' ORDER BY id DESC LIMIT 1"""
            ).fetchone()[0]
        self._decision_request += 1
        self.decisions.decide(
            binding=(
                memory_candidate_decision_ledger.CandidateDecisionLedgerBindingV1(
                    request_id=f"{self._decision_request:032d}",
                    origin="operator_cli",
                    decision="approve",
                    candidate_key=key,
                )
            )
        )
        return key

    def row(self, key: str):
        with channel_store.connect(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM memory_items WHERE memory_key=?",
                (key,),
            ).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def candidate_source_rows(self, key: str):
        with channel_store.connect(self.path) as conn:
            return tuple(conn.execute(
                """SELECT cs.* FROM memory_candidate_sources cs
                   JOIN memory_items m ON m.id=cs.memory_id
                   WHERE m.memory_key=? ORDER BY cs.id""",
                (key,),
            ).fetchall())

    def explicit_source_count(self, key: str) -> int:
        with channel_store.connect(self.path) as conn:
            return int(conn.execute(
                """SELECT count(*) FROM memory_sources s
                   JOIN memory_items m ON m.id=s.memory_id
                   WHERE m.memory_key=?""",
                (key,),
            ).fetchone()[0])

    @staticmethod
    def correct_request(key: str, content: str):
        return memory_explicit_actions.CorrectExplicitMemoryRequest(
            request_id=memory_explicit_actions.issue_request_id(),
            memory_key=key,
            replacement_content=content,
            sensitivity="normal",
        )

    @staticmethod
    def forget_request(key: str):
        return memory_explicit_actions.ForgetExplicitMemoryRequest(
            request_id=memory_explicit_actions.issue_request_id(),
            memory_key=key,
        )

    def assert_action_error(self, category: str, call, *args):
        with self.assertRaises(
            memory_explicit_actions.ExplicitMemoryActionError
        ) as ctx:
            call(*args)
        self.assertEqual(ctx.exception.category, category)

    def assert_terminal_error(self, call, *args):
        self.assert_action_error("terminal_semantics_invalid", call, *args)

    def assert_digest_tamper_error(self, call, *args):
        self.assert_action_error("request_binding_conflict", call, *args)

    def test_corrected_inferred_target_preserves_origin_and_replays(self):
        key = self.approved("Project AmendmentA uses Python.")
        automatic_sources = self.candidate_source_rows(key)
        request = self.correct_request(
            key,
            "Project AmendmentA uses Rust.",
        )
        result = self.explicit.correct_explicit_user_memory(request)
        old = self.row(key)
        replacement = self.row(result.memory_key)
        self.assertEqual(result.category, "corrected")
        self.assertEqual(
            (old["status"], old["explicitness"], old["confidence"]),
            ("superseded", "inferred", 1.0),
        )
        self.assertEqual(
            (
                replacement["status"],
                replacement["explicitness"],
                replacement["confidence"],
            ),
            ("active", "explicit", 1.0),
        )
        self.assertEqual(self.candidate_source_rows(key), automatic_sources)
        self.assertEqual(self.explicit_source_count(key), 0)
        self.assertEqual(self.explicit_source_count(result.memory_key), 1)
        replay = self.explicit.correct_explicit_user_memory(request)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.memory_key, result.memory_key)

    def test_unchanged_inferred_target_stays_inferred_and_replays(self):
        content = "Project AmendmentB uses Python."
        key = self.approved(content)
        automatic_sources = self.candidate_source_rows(key)
        request = self.correct_request(key, content)
        result = self.explicit.correct_explicit_user_memory(request)
        target = self.row(key)
        self.assertEqual(result.category, "unchanged")
        self.assertEqual(
            (target["status"], target["explicitness"], target["confidence"]),
            ("active", "inferred", 1.0),
        )
        self.assertEqual(self.candidate_source_rows(key), automatic_sources)
        self.assertEqual(self.explicit_source_count(key), 1)
        self.assertTrue(
            self.explicit.correct_explicit_user_memory(request).replayed
        )

    def test_suppressed_correction_preserves_inferred_target_and_replays(self):
        key = self.approved("Project AmendmentC uses Python.")
        replacement = "Project AmendmentC uses Rust."
        fingerprint = memory_policy.fingerprint_content(
            TEST_SECRET,
            scope_type="global_user",
            scope_ref="",
            kind="project",
            normalized_content=replacement,
        )
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO memory_suppressions
                   (scope_type,scope_ref,kind,normalized_fingerprint,
                    fingerprint_version,reason_category,created_at)
                   VALUES('global_user','','project',?,?,'user_forget',?)""",
                (
                    fingerprint,
                    memory_policy.FINGERPRINT_VERSION,
                    channel_store.now_iso(),
                ),
            )
        automatic_sources = self.candidate_source_rows(key)
        request = self.correct_request(key, replacement)
        result = self.explicit.correct_explicit_user_memory(request)
        target = self.row(key)
        self.assertEqual(result.category, "suppressed")
        self.assertIsNone(result.memory_key)
        self.assertEqual(
            (target["status"], target["explicitness"], target["confidence"]),
            ("active", "inferred", 1.0),
        )
        self.assertEqual(self.candidate_source_rows(key), automatic_sources)
        self.assertEqual(self.explicit_source_count(key), 0)
        self.assertTrue(
            self.explicit.correct_explicit_user_memory(request).replayed
        )

    def test_forget_inferred_target_preserves_origin_and_replays(self):
        key = self.approved("Project AmendmentD uses Python.")
        automatic_sources = self.candidate_source_rows(key)
        request = self.forget_request(key)
        result = self.explicit.forget_explicit_user_memory(request)
        target = self.row(key)
        self.assertEqual(result.category, "forgotten")
        self.assertEqual(
            (target["status"], target["explicitness"], target["confidence"]),
            ("forgotten", "inferred", 1.0),
        )
        self.assertIsNone(target["normalized_content"])
        self.assertIsNone(target["normalized_fingerprint"])
        self.assertEqual(self.candidate_source_rows(key), automatic_sources)
        self.assertEqual(self.explicit_source_count(key), 1)
        with channel_store.connect(self.path) as conn:
            reasons = tuple(row[0] for row in conn.execute(
                "SELECT reason_category FROM memory_suppressions ORDER BY id"
            ))
        self.assertIn("user_forget", reasons)
        self.assertTrue(self.explicit.forget_explicit_user_memory(request).replayed)

    def test_remember_and_corrected_replacement_still_require_explicit(self):
        remember = memory_explicit_actions.RememberExplicitMemoryRequest(
            request_id=memory_explicit_actions.issue_request_id(),
            kind="project",
            scope_type="global_user",
            scope_ref="",
            content="Project ExplicitHistory uses Python.",
            sensitivity="normal",
        )
        remembered = self.explicit.remember_explicit_user_memory(remember)
        with channel_store.connect(self.path) as conn:
            conn.execute(
                "UPDATE memory_items SET explicitness='inferred' WHERE memory_key=?",
                (remembered.memory_key,),
            )
        self.assert_digest_tamper_error(
            self.explicit.remember_explicit_user_memory,
            remember,
        )

        key = self.approved("Project ReplacementGuard uses Python.")
        correction = self.correct_request(
            key,
            "Project ReplacementGuard uses Rust.",
        )
        corrected = self.explicit.correct_explicit_user_memory(correction)
        with channel_store.connect(self.path) as conn:
            conn.execute(
                "UPDATE memory_items SET explicitness='inferred' WHERE memory_key=?",
                (corrected.memory_key,),
            )
        self.assert_digest_tamper_error(
            self.explicit.correct_explicit_user_memory,
            correction,
        )

    def test_confidence_and_arbitrary_explicitness_remain_invalid(self):
        key = self.approved("Project ConfidenceGuard uses Python.")
        request = self.correct_request(
            key,
            "Project ConfidenceGuard uses Rust.",
        )
        result = self.explicit.correct_explicit_user_memory(request)
        with channel_store.connect(self.path) as conn:
            conn.execute(
                "UPDATE memory_items SET confidence=0.5 WHERE memory_key=?",
                (result.memory_key,),
            )
        self.assert_digest_tamper_error(
            self.explicit.correct_explicit_user_memory,
            request,
        )

        other = self.approved("Project ExplicitnessGuard uses Python.")
        forgotten_request = self.forget_request(other)
        self.explicit.forget_explicit_user_memory(forgotten_request)
        with channel_store.connect(self.path) as conn:
            conn.execute("PRAGMA ignore_check_constraints=ON")
            conn.execute(
                "UPDATE memory_items SET explicitness='arbitrary' WHERE memory_key=?",
                (other,),
            )
        self.assert_terminal_error(
            self.explicit.forget_explicit_user_memory,
            forgotten_request,
        )

    def test_candidate_rejected_and_superseded_targets_gain_no_authority(self):
        candidate_message = self.message("Project CandidateGuard uses Python.")
        self.persistence.persist(
            canonical_message_id=candidate_message,
            source_text="Project CandidateGuard uses Python.",
            proposals=(memory_formation.AutoMemoryProposalV1(
                "project_fact", 0, len("Project CandidateGuard uses Python.")
            ),),
            formation_contract_version=FORMATION_VERSION,
            extractor_contract_version=EXTRACTOR_VERSION,
        )
        with channel_store.connect(self.path) as conn:
            candidate = conn.execute(
                """SELECT memory_key FROM memory_items
                   WHERE status='candidate' ORDER BY id DESC LIMIT 1"""
            ).fetchone()[0]
        for call, request in (
            (
                self.explicit.correct_explicit_user_memory,
                self.correct_request(candidate, "Project CandidateGuard uses Rust."),
            ),
            (
                self.explicit.forget_explicit_user_memory,
                self.forget_request(candidate),
            ),
        ):
            with self.assertRaises(memory_explicit_actions.ExplicitMemoryActionError):
                call(request)

        rejected = self.approved("Project RejectedGuard uses Python.")
        with channel_store.connect(self.path) as conn:
            conn.execute(
                "UPDATE memory_items SET status='rejected' WHERE memory_key=?",
                (rejected,),
            )
        with self.assertRaises(memory_explicit_actions.ExplicitMemoryActionError):
            self.explicit.forget_explicit_user_memory(
                self.forget_request(rejected)
            )

        superseded = self.approved("Project SupersededGuard uses Python.")
        correction = self.correct_request(
            superseded,
            "Project SupersededGuard uses Rust.",
        )
        self.explicit.correct_explicit_user_memory(correction)
        with self.assertRaises(memory_explicit_actions.ExplicitMemoryActionError):
            self.explicit.correct_explicit_user_memory(
                self.correct_request(
                    superseded,
                    "Project SupersededGuard uses Go.",
                )
            )

    def test_terminal_digest_binds_inferred_and_explicit_origin_both_directions(self):
        inferred = self.approved("Project DigestInferred uses Python.")
        inferred_request = self.correct_request(
            inferred,
            "Project DigestInferred uses Rust.",
        )
        self.explicit.correct_explicit_user_memory(inferred_request)
        with channel_store.connect(self.path) as conn:
            conn.execute(
                "UPDATE memory_items SET explicitness='explicit' WHERE memory_key=?",
                (inferred,),
            )
        self.assert_digest_tamper_error(
            self.explicit.correct_explicit_user_memory,
            inferred_request,
        )

        explicit_request = memory_explicit_actions.RememberExplicitMemoryRequest(
            request_id=memory_explicit_actions.issue_request_id(),
            kind="project",
            scope_type="global_user",
            scope_ref="",
            content="Project DigestExplicit uses Python.",
            sensitivity="normal",
        )
        explicit = self.explicit.remember_explicit_user_memory(explicit_request)
        with channel_store.connect(self.path) as conn:
            conn.execute(
                "UPDATE memory_items SET explicitness='inferred' WHERE memory_key=?",
                (explicit.memory_key,),
            )
        self.assert_digest_tamper_error(
            self.explicit.remember_explicit_user_memory,
            explicit_request,
        )


if __name__ == "__main__":
    unittest.main()
