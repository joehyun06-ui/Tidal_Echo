from __future__ import annotations

import dataclasses
import importlib
import inspect
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from backend import (
    channel_store,
    memory_candidate_decision_ledger,
    memory_candidate_review,
    memory_explicit_actions,
    memory_formation,
    memory_policy,
    memory_service,
    memory_store,
)
from backend.tests.test_memory_candidate_persistence import (
    FORMATION_VERSION,
    EXTRACTOR_VERSION,
    bootstrap,
    candidate_config,
)


class CandidateDecisionTests(unittest.TestCase):
    def setUp(self):
        global channel_store, memory_candidate_decision_ledger
        global memory_candidate_review, memory_explicit_actions
        global memory_formation, memory_policy, memory_service, memory_store

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "decisions.sqlite3")
        with channel_store.connect(self.path) as conn:
            conn.execute("""CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')""")
        channel_store.run_migrations(self.path)
        self.runtime = bootstrap(self.path, self.config())
        # App-integration tests deliberately evict backend modules from
        # sys.modules.  Refresh this test's module references after bootstrap
        # so full-suite execution uses the same class identities and seams.
        channel_store = importlib.import_module("backend.channel_store")
        memory_candidate_decision_ledger = importlib.import_module(
            "backend.memory_candidate_decision_ledger"
        )
        memory_candidate_review = importlib.import_module(
            "backend.memory_candidate_review"
        )
        memory_explicit_actions = importlib.import_module(
            "backend.memory_explicit_actions"
        )
        memory_formation = importlib.import_module("backend.memory_formation")
        memory_policy = importlib.import_module("backend.memory_policy")
        memory_service = importlib.import_module("backend.memory_service")
        memory_store = importlib.import_module("backend.memory_store")
        self.writer = self.runtime.candidate_decisions
        self.persistence = self.runtime.candidate_persistence

    @staticmethod
    def config(*, writes: bool = False):
        return dataclasses.replace(
            candidate_config(writes=writes),
            candidate_review_enabled=True,
            candidate_decisions_enabled=True,
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

    def persist(
        self,
        content: str,
        signal_type: str = "project_fact",
    ) -> str:
        message_id = self.message(content)
        start = 0
        result = self.persistence.persist(
            canonical_message_id=message_id,
            source_text=content,
            proposals=(memory_formation.AutoMemoryProposalV1(
                signal_type,
                start,
                start + len(content),
            ),),
            formation_contract_version=FORMATION_VERSION,
            extractor_contract_version=EXTRACTOR_VERSION,
        )
        self.assertEqual(result.outcome, "completed")
        fingerprint = memory_policy.fingerprint_content(
            self.config().fingerprint_hmac_secret,
            scope_type=memory_formation.SCOPE_TYPE,
            scope_ref=memory_formation.SCOPE_REF,
            kind=memory_formation.SIGNAL_KIND_MAPPING[signal_type],
            normalized_content=memory_policy.normalize_content(
                content,
                max_chars=1000,
            ),
        )
        with channel_store.connect(self.path) as conn:
            rows = conn.execute(
                """SELECT memory_key,normalized_fingerprint FROM memory_items
                   ORDER BY id"""
            ).fetchall()
        keys = [
            row["memory_key"]
            for row in rows
            if memory_policy.secure_digest_equal(
                row["normalized_fingerprint"], fingerprint
            )
        ]
        self.assertEqual(len(keys), 1)
        return keys[0]

    @staticmethod
    def binding(
        candidate_key: str,
        *,
        request_number: int = 1,
        decision: str = "approve",
        origin: str = "operator_cli",
    ):
        return memory_candidate_decision_ledger.CandidateDecisionLedgerBindingV1(
            request_id=f"{request_number:032d}",
            origin=origin,
            decision=decision,
            candidate_key=candidate_key,
        )

    def row(self, candidate_key: str) -> dict:
        with channel_store.connect(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM memory_items WHERE memory_key=?",
                (candidate_key,),
            ).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def table_rows(self, table: str):
        with channel_store.connect(self.path) as conn:
            return tuple(
                tuple(row)
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
            )

    def state(self):
        tables = (
            "memory_items",
            "memory_candidate_sources",
            "memory_candidate_decisions",
            "memory_suppressions",
            "memory_action_requests",
            "memory_evidence_events",
            "memory_sources",
        )
        return {table: self.table_rows(table) for table in tables}

    def assert_error(self, category: str, call, *args, **kwargs):
        with self.assertRaises(
            memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError
        ) as ctx:
            call(*args, **kwargs)
        self.assertEqual(ctx.exception.category, category)
        self.assertEqual(str(ctx.exception), category)
        return ctx.exception

    def review_service(self):
        reader = memory_candidate_review.MemoryCandidateReviewReader(
            self.path,
            fingerprint_key_id=self.config().fingerprint_key_id,
            fingerprint_hmac_secret=self.config().fingerprint_hmac_secret,
            max_item_chars=1000,
        )
        return memory_candidate_review.MemoryCandidateReviewService(
            reader,
            enabled=True,
            configuration_valid=True,
            error_category="",
        )

    def test_writer_is_narrow_and_not_an_explicit_or_review_capability(self):
        self.assertIsInstance(self.writer, memory_service.CandidateDecisionWriter)
        self.assertIs(self.writer._store, self.runtime.privileged_actions._store)
        self.assertIs(
            self.writer._authority,
            self.runtime.privileged_actions._authority,
        )
        for name in ("decide", "approve", "reject"):
            self.assertFalse(hasattr(self.runtime.privileged_actions, name))
        self.assertEqual(
            tuple(inspect.signature(self.writer.decide).parameters),
            ("binding",),
        )
        adapter_source = inspect.getsource(
            __import__(
                "backend.memory_candidate_review_adapters",
                fromlist=["*"],
            )
        )
        self.assertNotIn("approve", adapter_source)
        self.assertNotIn("reject", adapter_source)

    def test_approve_exact_mutation_retrieval_and_no_explicit_evidence(self):
        key = self.persist("Project Atlas uses Python.")
        before = self.row(key)
        protected = {
            table: self.table_rows(table)
            for table in (
                "memory_action_requests",
                "memory_evidence_events",
                "memory_sources",
                "memory_suppressions",
                "memory_candidate_sources",
            )
        }
        binding = self.binding(key)
        result = self.writer.decide(binding=binding)
        after = self.row(key)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.result_category, "approved")
        self.assertEqual(result.resulting_status, "active")
        self.assertFalse(result.replayed)
        self.assertEqual(repr(result), "<CandidateDecisionResultV1>")
        self.assertNotIn(key, repr(result))
        self.assertNotIn(binding.request_id, repr(result))
        self.assertEqual(after["status"], "active")
        self.assertEqual(after["confidence"], 1.0)
        self.assertEqual(after["last_confirmed_at"], after["updated_at"])
        for field in (
            "id",
            "memory_key",
            "kind",
            "scope_type",
            "scope_ref",
            "normalized_content",
            "normalized_fingerprint",
            "fingerprint_version",
            "explicitness",
            "sensitivity",
            "first_observed_at",
            "superseded_by_id",
            "created_at",
        ):
            self.assertEqual(after[field], before[field], field)
        for table, rows in protected.items():
            self.assertEqual(self.table_rows(table), rows, table)
        active = self.runtime.read_service.get_active_memories(
            scope_type="global_user",
            scope_ref="",
        )
        self.assertEqual([item["memory_key"] for item in active], [key])
        self.assertEqual(active[0]["explicitness"], "inferred")
        self.assertEqual(active[0]["confidence"], 1.0)
        self.assertEqual(active[0]["sensitivity"], "normal")
        self.assertEqual(active[0]["provenance"], [])
        with self.assertRaises(
            memory_candidate_review.MemoryCandidateReviewError
        ) as review:
            self.review_service().get_candidate(key)
        self.assertEqual(review.exception.category, "candidate_not_found")

    def test_reject_exact_mutation_and_suppression_blocks_future_candidate(self):
        content = "Project Atlas uses Python."
        key = self.persist(content)
        before = self.row(key)
        protected = {
            table: self.table_rows(table)
            for table in (
                "memory_action_requests",
                "memory_evidence_events",
                "memory_sources",
                "memory_candidate_sources",
            )
        }
        binding = self.binding(key, decision="reject")
        result = self.writer.decide(binding=binding)
        after = self.row(key)
        self.assertEqual(result.result_category, "rejected")
        self.assertEqual(result.resulting_status, "rejected")
        self.assertEqual(after["status"], "rejected")
        self.assertEqual(after["confidence"], 0.0)
        self.assertEqual(after["updated_at"], self.table_rows(
            "memory_candidate_decisions"
        )[0][-1])
        for field in (
            "id",
            "memory_key",
            "kind",
            "scope_type",
            "scope_ref",
            "normalized_content",
            "normalized_fingerprint",
            "fingerprint_version",
            "explicitness",
            "confidence",
            "sensitivity",
            "first_observed_at",
            "last_confirmed_at",
            "superseded_by_id",
            "created_at",
        ):
            self.assertEqual(after[field], before[field], field)
        for table, rows in protected.items():
            self.assertEqual(self.table_rows(table), rows, table)
        suppressions = self.table_rows("memory_suppressions")
        self.assertEqual(len(suppressions), 1)
        self.assertEqual(suppressions[0][6], "user_reject")
        active = self.runtime.read_service.get_active_memories(
            scope_type="global_user",
            scope_ref="",
        )
        self.assertEqual(active, [])
        with self.assertRaises(
            memory_candidate_review.MemoryCandidateReviewError
        ) as review:
            self.review_service().get_candidate(key)
        self.assertEqual(review.exception.category, "candidate_not_found")

        second_id = self.message(content)
        persistence_result = self.persistence.persist(
            canonical_message_id=second_id,
            source_text=content,
            proposals=(memory_formation.AutoMemoryProposalV1(
                "project_fact", 0, len(content)
            ),),
            formation_contract_version=FORMATION_VERSION,
            extractor_contract_version=EXTRACTOR_VERSION,
        )
        self.assertEqual(persistence_result.suppressed_count, 1)
        self.assertEqual(persistence_result.created_count, 0)
        with channel_store.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM memory_items").fetchone()[0],
                1,
            )

    def test_approve_and_reject_replay_are_read_only_and_exactly_once(self):
        for number, decision in ((10, "approve"), (20, "reject")):
            with self.subTest(decision=decision):
                if number == 20:
                    key = self.persist("I usually prefer window seats.", "durable_preference")
                else:
                    key = self.persist("Project Atlas uses Python.")
                binding = self.binding(
                    key,
                    request_number=number,
                    decision=decision,
                )
                fresh = self.writer.decide(binding=binding)
                before_replay = self.state()
                replay = self.writer.decide(binding=binding)
                self.assertFalse(fresh.replayed)
                self.assertTrue(replay.replayed)
                self.assertEqual(self.state(), before_replay)
        self.assertEqual(len(self.table_rows("memory_candidate_decisions")), 2)
        self.assertEqual(len(self.table_rows("memory_suppressions")), 1)

    def test_request_conflict_precedes_target_resolution_and_new_request_is_not_replay(self):
        first = self.persist("Project Atlas uses Python.")
        second = self.persist(
            "I usually prefer window seats.",
            "durable_preference",
        )
        original = self.binding(first, request_number=31)
        self.writer.decide(binding=original)
        conflict = self.binding(second, request_number=31)
        self.assert_error(
            "candidate_decision_request_conflict",
            self.writer.decide,
            binding=conflict,
        )
        self.assert_error(
            "candidate_not_pending",
            self.writer.decide,
            binding=self.binding(first, request_number=32),
        )
        self.assertEqual(self.row(second)["status"], "candidate")

    def test_preexisting_matching_suppression_is_corruption_with_zero_mutation(self):
        key = self.persist("Project Atlas uses Python.")
        row = self.row(key)
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO memory_suppressions
                   (scope_type,scope_ref,kind,normalized_fingerprint,
                    fingerprint_version,reason_category,created_at)
                   VALUES(?,?,?,?,?,'user_forget',?)""",
                (
                    row["scope_type"],
                    row["scope_ref"],
                    row["kind"],
                    row["normalized_fingerprint"],
                    row["fingerprint_version"],
                    channel_store.now_iso(),
                ),
            )
        before = self.state()
        self.assert_error(
            "candidate_decision_state_invalid",
            self.writer.decide,
            binding=self.binding(key),
        )
        self.assertEqual(self.state(), before)

    def test_approve_and_reject_rollback_seam_reverts_every_write(self):
        cases = (
            ("approve", "Project Atlas uses Python.", "project_fact"),
            ("reject", "I usually prefer window seats.", "durable_preference"),
        )
        for number, (decision, content, signal) in enumerate(cases, 50):
            with self.subTest(decision=decision):
                key = self.persist(content, signal)
                before = self.state()
                with mock.patch.object(
                    memory_store.MemoryStore,
                    "_before_candidate_decision_ledger_insert",
                    side_effect=RuntimeError("synthetic rollback seam"),
                ):
                    self.assert_error(
                        "candidate_decision_state_invalid",
                        self.writer.decide,
                        binding=self.binding(
                            key,
                            request_number=number,
                            decision=decision,
                        ),
                    )
                self.assertEqual(self.state(), before)
                self.assertEqual(self.row(key)["status"], "candidate")

    def test_schema_and_profile_failures_do_not_bootstrap_or_mutate(self):
        key = self.persist("Project Atlas uses Python.")
        with channel_store.connect(self.path) as conn:
            conn.execute(
                "DROP TRIGGER memory_candidate_decisions_immutable_update"
            )
        before = self.state()
        self.assert_error(
            "candidate_decision_schema_invalid",
            self.writer.decide,
            binding=self.binding(key, request_number=61),
        )
        self.assertEqual(self.state(), before)

        other = str(Path(self.temp.name) / "profile.sqlite3")
        with channel_store.connect(other) as conn:
            conn.execute("""CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')""")
        channel_store.run_migrations(other)
        runtime = bootstrap(other, self.config())
        old_path, old_runtime, old_writer, old_persistence = (
            self.path,
            self.runtime,
            self.writer,
            self.persistence,
        )
        self.path, self.runtime = other, runtime
        self.writer, self.persistence = (
            runtime.candidate_decisions,
            runtime.candidate_persistence,
        )
        try:
            missing_profile_key = self.persist("Project Borealis uses Rust.")
            with channel_store.connect(other) as conn:
                conn.execute("DELETE FROM memory_fingerprint_profile")
            before = self.state()
            self.assert_error(
                "candidate_decision_profile_mismatch",
                self.writer.decide,
                binding=self.binding(
                    missing_profile_key,
                    request_number=62,
                ),
            )
            self.assertEqual(self.state(), before)
            self.assertEqual(self.table_rows("memory_fingerprint_profile"), ())
        finally:
            self.path, self.runtime = old_path, old_runtime
            self.writer, self.persistence = old_writer, old_persistence

    def test_concurrency_first_committer_wins_for_replay_and_conflicts(self):
        key = self.persist("Project Atlas uses Python.")
        binding = self.binding(key, request_number=70)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(
                lambda _index: self.writer.decide(binding=binding),
                range(2),
            ))
        self.assertEqual(sorted(item.replayed for item in outcomes), [False, True])
        self.assertEqual(len(self.table_rows("memory_candidate_decisions")), 1)

        second = self.persist(
            "I usually prefer window seats.",
            "durable_preference",
        )
        bindings = (
            self.binding(second, request_number=71, decision="approve"),
            self.binding(second, request_number=72, decision="reject"),
        )

        def decide(value):
            try:
                return self.writer.decide(binding=value)
            except Exception as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            mixed = list(pool.map(decide, bindings))
        completed = [item for item in mixed if not isinstance(item, Exception)]
        failed = [item for item in mixed if isinstance(item, Exception)]
        self.assertEqual(len(completed), 1)
        self.assertEqual(len(failed), 1)
        self.assertEqual(
            failed[0].category,
            "candidate_not_pending",
        )
        terminal = self.row(second)["status"]
        ledger = self.table_rows("memory_candidate_decisions")[-1]
        self.assertEqual(
            (terminal, ledger[3]),
            ("active", "approve")
            if completed[0].decision == "approve"
            else ("rejected", "reject"),
        )

    def _corrupt_candidate(self, case: str, key: str) -> None:
        with channel_store.connect(self.path) as conn:
            if case == "wrong_explicitness":
                conn.execute(
                    "UPDATE memory_items SET explicitness='explicit' WHERE memory_key=?",
                    (key,),
                )
            elif case == "wrong_confidence":
                conn.execute(
                    "UPDATE memory_items SET confidence=0.5 WHERE memory_key=?",
                    (key,),
                )
            elif case == "wrong_scope":
                conn.execute(
                    """UPDATE memory_items SET scope_type='channel',scope_ref='web'
                       WHERE memory_key=?""",
                    (key,),
                )
            elif case == "wrong_sensitivity":
                conn.execute(
                    "UPDATE memory_items SET sensitivity='sensitive' WHERE memory_key=?",
                    (key,),
                )
            elif case == "wrong_kind":
                conn.execute(
                    """UPDATE memory_items SET kind='user_preference'
                       WHERE memory_key=?""",
                    (key,),
                )
            elif case == "wrong_fingerprint":
                memory_id = conn.execute(
                    "SELECT id FROM memory_items WHERE memory_key=?",
                    (key,),
                ).fetchone()[0]
                conn.execute(
                    """UPDATE memory_items SET normalized_fingerprint=?
                       WHERE memory_key=?""",
                    (bytes([(int(memory_id) % 250) + 1]) * 32, key),
                )
            elif case == "noncanonical_content":
                conn.execute(
                    """UPDATE memory_items SET normalized_content='  changed  '
                       WHERE memory_key=?""",
                    (key,),
                )
            elif case == "missing_provenance":
                name = "memory_candidate_sources_immutable_delete"
                conn.execute(f"DROP TRIGGER {name}")
                conn.execute(
                    """DELETE FROM memory_candidate_sources
                       WHERE memory_id=(SELECT id FROM memory_items WHERE memory_key=?)""",
                    (key,),
                )
                conn.execute(
                    channel_store.MEMORY_CANDIDATE_PERSISTENCE_TRIGGER_DDL[name]
                )
            elif case == "bad_provenance_span":
                name = "memory_candidate_sources_immutable_update"
                conn.execute(f"DROP TRIGGER {name}")
                conn.execute(
                    """UPDATE memory_candidate_sources SET span_end=span_end-1
                       WHERE memory_id=(SELECT id FROM memory_items WHERE memory_key=?)""",
                    (key,),
                )
                conn.execute(
                    channel_store.MEMORY_CANDIDATE_PERSISTENCE_TRIGGER_DDL[name]
                )
            elif case == "wrong_canonical_message":
                conn.execute(
                    """UPDATE messages SET direction='out'
                       WHERE id=(SELECT canonical_message_id
                                   FROM memory_candidate_sources
                                  WHERE memory_id=(SELECT id FROM memory_items
                                                    WHERE memory_key=?))""",
                    (key,),
                )
            elif case == "phase4a_rebuild_mismatch":
                name = "memory_candidate_sources_immutable_update"
                conn.execute(f"DROP TRIGGER {name}")
                conn.execute(
                    """UPDATE memory_candidate_sources SET signal_type='durable_preference'
                       WHERE memory_id=(SELECT id FROM memory_items WHERE memory_key=?)""",
                    (key,),
                )
                conn.execute(
                    channel_store.MEMORY_CANDIDATE_PERSISTENCE_TRIGGER_DDL[name]
                )
            else:
                self.fail(f"unknown corruption case: {case}")

    def test_all_fresh_candidate_corruption_fails_closed_for_both_decisions(self):
        cases = (
            "wrong_explicitness",
            "wrong_confidence",
            "wrong_scope",
            "wrong_sensitivity",
            "wrong_kind",
            "wrong_fingerprint",
            "noncanonical_content",
            "missing_provenance",
            "bad_provenance_span",
            "wrong_canonical_message",
            "phase4a_rebuild_mismatch",
        )
        request_number = 100
        for decision in ("approve", "reject"):
            for case in cases:
                with self.subTest(decision=decision, case=case):
                    request_number += 1
                    content = f"Project Fresh{request_number} uses Python."
                    key = self.persist(content)
                    self._corrupt_candidate(case, key)
                    before = self.state()
                    self.assert_error(
                        "candidate_decision_state_invalid",
                        self.writer.decide,
                        binding=self.binding(
                            key,
                            request_number=request_number,
                            decision=decision,
                        ),
                    )
                    self.assertEqual(self.state(), before)
                    self.assertEqual(
                        len(self.table_rows("memory_candidate_decisions")),
                        0,
                    )

    def _tamper_terminal_replay(
        self,
        *,
        decision: str,
        case: str,
        key: str,
    ) -> None:
        with channel_store.connect(self.path) as conn:
            memory_id = conn.execute(
                "SELECT id FROM memory_items WHERE memory_key=?",
                (key,),
            ).fetchone()[0]
            if case == "confidence":
                conn.execute(
                    "UPDATE memory_items SET confidence=0.5 WHERE id=?",
                    (memory_id,),
                )
            elif case == "content":
                conn.execute(
                    "UPDATE memory_items SET normalized_content='tampered' WHERE id=?",
                    (memory_id,),
                )
            elif case == "fingerprint":
                conn.execute(
                    """UPDATE memory_items SET normalized_fingerprint=zeroblob(32)
                       WHERE id=?""",
                    (memory_id,),
                )
            elif case == "provenance":
                name = "memory_candidate_sources_immutable_update"
                conn.execute(f"DROP TRIGGER {name}")
                conn.execute(
                    """UPDATE memory_candidate_sources SET span_end=span_end-1
                       WHERE memory_id=?""",
                    (memory_id,),
                )
                conn.execute(
                    channel_store.MEMORY_CANDIDATE_PERSISTENCE_TRIGGER_DDL[name]
                )
            elif case == "ledger_suppression":
                row = conn.execute(
                    "SELECT * FROM memory_items WHERE id=?",
                    (memory_id,),
                ).fetchone()
                suppression = conn.execute(
                    """INSERT INTO memory_suppressions
                       (scope_type,scope_ref,kind,normalized_fingerprint,
                        fingerprint_version,reason_category,created_at)
                       VALUES(?,?,?,?,?,'user_reject',?)""",
                    (
                        row["scope_type"],
                        row["scope_ref"],
                        row["kind"],
                        row["normalized_fingerprint"],
                        row["fingerprint_version"],
                        channel_store.now_iso(),
                    ),
                ).lastrowid
                name = "memory_candidate_decisions_immutable_update"
                conn.execute(f"DROP TRIGGER {name}")
                conn.execute("PRAGMA ignore_check_constraints=ON")
                conn.execute(
                    """UPDATE memory_candidate_decisions SET suppression_id=?
                       WHERE memory_id=?""",
                    (suppression, memory_id),
                )
                conn.execute(
                    channel_store.MEMORY_CANDIDATE_DECISION_TRIGGER_DDL[name]
                )
            elif case.startswith("suppression_"):
                suppression_id = conn.execute(
                    """SELECT suppression_id FROM memory_candidate_decisions
                       WHERE memory_id=?""",
                    (memory_id,),
                ).fetchone()[0]
                assignments = {
                    "suppression_reason": "reason_category='user_forget'",
                    "suppression_fingerprint": (
                        "normalized_fingerprint=zeroblob(32)"
                    ),
                    "suppression_scope": "scope_type='channel',scope_ref='web'",
                    "suppression_kind": "kind='user_preference'",
                    "suppression_version": "fingerprint_version=99",
                }
                conn.execute(
                    f"UPDATE memory_suppressions SET {assignments[case]} WHERE id=?",
                    (suppression_id,),
                )
            else:
                self.fail(f"unknown replay tamper: {decision}/{case}")

    def test_terminal_replay_reproves_memory_provenance_ledger_and_suppression(self):
        cases = {
            "approve": (
                "confidence",
                "content",
                "fingerprint",
                "provenance",
                "ledger_suppression",
            ),
            "reject": (
                "content",
                "fingerprint",
                "provenance",
                "suppression_reason",
                "suppression_fingerprint",
                "suppression_scope",
                "suppression_kind",
                "suppression_version",
            ),
        }
        request_number = 200
        for decision, decision_cases in cases.items():
            for case in decision_cases:
                with self.subTest(decision=decision, case=case):
                    request_number += 1
                    isolated = str(
                        Path(self.temp.name)
                        / f"replay-{request_number}.sqlite3"
                    )
                    with channel_store.connect(isolated) as conn:
                        conn.execute("""CREATE TABLE messages(
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            ts TEXT NOT NULL,direction TEXT NOT NULL,
                            kind TEXT NOT NULL,text TEXT NOT NULL,
                            meta TEXT NOT NULL DEFAULT '{}')""")
                    channel_store.run_migrations(isolated)
                    runtime = bootstrap(isolated, self.config())
                    old = (
                        self.path,
                        self.runtime,
                        self.writer,
                        self.persistence,
                    )
                    self.path = isolated
                    self.runtime = runtime
                    self.writer = runtime.candidate_decisions
                    self.persistence = runtime.candidate_persistence
                    try:
                        key = self.persist(
                            f"Project Replay{request_number} uses Python."
                        )
                        binding = self.binding(
                            key,
                            request_number=request_number,
                            decision=decision,
                        )
                        self.writer.decide(binding=binding)
                        self._tamper_terminal_replay(
                            decision=decision,
                            case=case,
                            key=key,
                        )
                        before = self.state()
                        self.assert_error(
                            "candidate_decision_state_invalid",
                            self.writer.decide,
                            binding=binding,
                        )
                        self.assertEqual(self.state(), before)
                    finally:
                        (
                            self.path,
                            self.runtime,
                            self.writer,
                            self.persistence,
                        ) = old

    def test_same_request_different_binding_and_different_requests_concurrently(self):
        first = self.persist("Project ConcurrentA uses Python.")
        second = self.persist("Project ConcurrentB uses Python.")
        same_request = (
            self.binding(first, request_number=300),
            self.binding(second, request_number=300),
        )

        def decide(value):
            try:
                return self.writer.decide(binding=value)
            except Exception as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(decide, same_request))
        successes = [item for item in outcomes if not isinstance(item, Exception)]
        failures = [item for item in outcomes if isinstance(item, Exception)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(
            failures[0].category,
            "candidate_decision_request_conflict",
        )
        self.assertEqual(len(self.table_rows("memory_candidate_decisions")), 1)
        self.assertEqual(
            sorted((self.row(first)["status"], self.row(second)["status"])),
            ["active", "candidate"],
        )

        target = self.persist("Project ConcurrentC uses Python.")
        distinct = (
            self.binding(target, request_number=301),
            self.binding(target, request_number=302),
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(decide, distinct))
        successes = [item for item in outcomes if not isinstance(item, Exception)]
        failures = [item for item in outcomes if isinstance(item, Exception)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].category, "candidate_not_pending")

    def test_runtime_guard_runs_inside_every_fresh_and_replay_transaction(self):
        key = self.persist("Project Guarded uses Python.")
        binding = self.binding(key, request_number=400)
        calls = []
        original = memory_store.MemoryStore._require_candidate_decision_runtime

        def guarded(store):
            calls.append(store)
            return original(store)

        with mock.patch.object(
            memory_store.MemoryStore,
            "_require_candidate_decision_runtime",
            new=guarded,
        ):
            self.writer.decide(binding=binding)
            self.writer.decide(binding=binding)
        self.assertEqual(calls, [self.writer._store, self.writer._store])

    def test_invalid_binding_and_errors_are_closed_and_data_free(self):
        key = self.persist("Project Bound uses Python.")
        invalid = self.binding(key, request_number=410)
        object.__setattr__(invalid, "candidate_key", "bad/key")
        before = self.state()
        error = self.assert_error(
            "invalid_candidate_key",
            self.writer.decide,
            binding=invalid,
        )
        self.assertEqual(self.state(), before)
        self.assertNotIn("bad/key", repr(error))
        self.assertNotIn("bad/key", str(error))
        self.assert_error(
            "invalid_candidate_decision_request",
            self.writer.decide,
            binding=object(),
        )
        with self.assertRaises(TypeError):
            memory_candidate_decision_ledger.CandidateDecisionResultV1(
                self.binding(key, request_number=411),
                replayed=False,
                status="overridden",
            )

    def test_approved_inferred_memory_remains_manageable_by_explicit_apis(self):
        isolated = str(Path(self.temp.name) / "explicit.sqlite3")
        with channel_store.connect(isolated) as conn:
            conn.execute("""CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')""")
        channel_store.run_migrations(isolated)
        runtime = bootstrap(isolated, self.config(writes=True))
        old = self.path, self.runtime, self.writer, self.persistence
        self.path, self.runtime = isolated, runtime
        self.writer = runtime.candidate_decisions
        self.persistence = runtime.candidate_persistence
        try:
            correct_key = self.persist("Project ExplicitA uses Python.")
            forget_key = self.persist("Project ExplicitB uses Python.")
            self.writer.decide(
                binding=self.binding(correct_key, request_number=420)
            )
            self.writer.decide(
                binding=self.binding(forget_key, request_number=421)
            )
            backend = memory_explicit_actions.create_entry_backend(
                runtime.privileged_actions
            )
            service = memory_explicit_actions.bind_operator_cli(backend)
            corrected = service.correct_explicit_user_memory(
                memory_explicit_actions.CorrectExplicitMemoryRequest(
                    request_id=memory_explicit_actions.issue_request_id(),
                    memory_key=correct_key,
                    replacement_content="Project ExplicitA uses Rust.",
                    sensitivity="normal",
                )
            )
            forgotten = service.forget_explicit_user_memory(
                memory_explicit_actions.ForgetExplicitMemoryRequest(
                    request_id=memory_explicit_actions.issue_request_id(),
                    memory_key=forget_key,
                )
            )
            self.assertEqual(corrected.category, "corrected")
            self.assertEqual(forgotten.category, "forgotten")
            self.assertEqual(self.row(correct_key)["status"], "superseded")
            self.assertEqual(self.row(forget_key)["status"], "forgotten")
        finally:
            self.path, self.runtime, self.writer, self.persistence = old


if __name__ == "__main__":
    unittest.main()
