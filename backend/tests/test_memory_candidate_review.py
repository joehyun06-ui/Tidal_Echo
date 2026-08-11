from __future__ import annotations

import dataclasses
import inspect
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import (
    channel_store,
    memory_action_ledger,
    memory_candidate_review,
    memory_formation,
    memory_policy,
)
from backend.tests._support import NoNetworkMixin
from backend.tests.test_memory_candidate_persistence import (
    EXTRACTOR_VERSION,
    FORMATION_VERSION,
    TEST_SECRET,
    bootstrap,
    candidate_config,
)


KEY_ID = "candidate-persistence-test-key"
MEMORY_TABLES = (
    "memory_items",
    "memory_fingerprint_profile",
    "memory_sources",
    "memory_suppressions",
    "memory_evidence_events",
    "memory_action_requests",
    "memory_candidate_sources",
    "memory_auto_formation_runs",
)


class MemoryCandidateReviewTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "candidate-review.sqlite3")
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """CREATE TABLE messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                    text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')"""
            )
        channel_store.run_migrations(self.path)
        self.runtime = bootstrap(self.path, candidate_config())
        self.persistence = self.runtime.candidate_persistence
        self.reader = memory_candidate_review.MemoryCandidateReviewReader(
            self.path,
            fingerprint_key_id=KEY_ID,
            fingerprint_hmac_secret=TEST_SECRET,
            max_item_chars=1000,
        )
        self.service = memory_candidate_review.MemoryCandidateReviewService(
            self.reader,
            enabled=True,
            configuration_valid=True,
            error_category="",
        )

    def message(
        self,
        text: str,
        *,
        direction: str = "in",
        kind: str = "user",
    ) -> int:
        with channel_store.connect(self.path) as conn:
            cursor = conn.execute(
                """INSERT INTO messages(ts,direction,kind,text,meta)
                   VALUES(?,?,?,?,?)""",
                (
                    channel_store.now_iso(),
                    direction,
                    kind,
                    text,
                    json.dumps({"channel": "web", "source": "relay"}),
                ),
            )
            return int(cursor.lastrowid)

    def persist(
        self,
        content: str,
        signal_type: str = "durable_preference",
        *,
        source_text: str | None = None,
    ) -> str:
        source_text = content if source_text is None else source_text
        start = source_text.index(content)
        message_id = self.message(source_text)
        self.persistence.persist(
            canonical_message_id=message_id,
            source_text=source_text,
            proposals=(memory_formation.AutoMemoryProposalV1(
                signal_type=signal_type,
                start=start,
                end=start + len(content),
            ),),
            formation_contract_version=FORMATION_VERSION,
            extractor_contract_version=EXTRACTOR_VERSION,
        )
        fingerprint = memory_policy.fingerprint_content(
            TEST_SECRET,
            scope_type="global_user",
            scope_ref="",
            kind=memory_formation.SIGNAL_KIND_MAPPING[signal_type],
            normalized_content=memory_policy.normalize_content(
                content, max_chars=1000
            ),
        )
        with channel_store.connect(self.path) as conn:
            return str(conn.execute(
                """SELECT memory_key FROM memory_items
                   WHERE fingerprint_version=? AND normalized_fingerprint=?""",
                (memory_policy.FINGERPRINT_VERSION, fingerprint),
            ).fetchone()[0])

    def candidate_row(self, key: str) -> sqlite3.Row:
        with channel_store.connect(self.path) as conn:
            return conn.execute(
                "SELECT * FROM memory_items WHERE memory_key=?", (key,)
            ).fetchone()

    def snapshot(self) -> tuple[object, ...]:
        with channel_store.connect(self.path) as conn:
            rows = tuple(
                (
                    table,
                    tuple(tuple(row) for row in conn.execute(
                        f"SELECT * FROM {table} ORDER BY rowid"
                    )),
                )
                for table in MEMORY_TABLES
            )
            inserts = tuple(
                line
                for line in conn.iterdump()
                if line.startswith("INSERT INTO \"memory_")
            )
        return rows, inserts

    def assert_error(self, category: str, call, *args, **kwargs):
        before = self.snapshot()
        with self.assertRaises(memory_candidate_review.MemoryCandidateReviewError) as ctx:
            call(*args, **kwargs)
        self.assertEqual(ctx.exception.category, category)
        self.assertEqual(str(ctx.exception), category)
        self.assertEqual(self.snapshot(), before)
        return ctx.exception

    def drop_and_restore_source_trigger(self, suffix: str, sql: str, parameters=()):
        name = f"memory_candidate_sources_immutable_{suffix}"
        with channel_store.connect(self.path) as conn:
            conn.execute(f"DROP TRIGGER {name}")
            conn.execute("PRAGMA ignore_check_constraints=ON")
            conn.execute(sql, parameters)
            conn.execute(
                channel_store.MEMORY_CANDIDATE_PERSISTENCE_TRIGGER_DDL[name]
            )

    def test_module_has_isolated_read_only_object_graph_and_safe_api(self):
        source = inspect.getsource(memory_candidate_review)
        for forbidden in (
            "MemoryStore(",
            "MemoryRuntime(",
            "PrivilegedMemoryActions(",
            "AutomaticCandidatePersistence(",
            "BEGIN IMMEDIATE",
        ):
            self.assertNotIn(forbidden, source)
        self.assertEqual(repr(self.reader), "<MemoryCandidateReviewReader>")
        self.assertEqual(repr(self.service), "<MemoryCandidateReviewService>")
        parameters = tuple(
            inspect.signature(self.service.list_candidates).parameters
        )
        self.assertEqual(
            parameters,
            ("limit", "after_candidate_key", "kind"),
        )
        self.assertEqual(
            tuple(inspect.signature(self.service.get_candidate).parameters),
            ("candidate_key",),
        )

    def test_review_and_explicit_ledger_share_canonical_memory_key_validator(self):
        self.assertIs(
            memory_action_ledger.MEMORY_KEY_PATTERN,
            memory_policy.MEMORY_KEY_PATTERN,
        )
        source = inspect.getsource(memory_candidate_review)
        self.assertNotIn("_MEMORY_KEY", source)
        self.assertEqual(
            source.count("memory_policy.MEMORY_KEY_PATTERN.fullmatch"),
            3,
        )

        key = self.persist("Project Atlas uses Python.", "project_fact")
        self.assertTrue(memory_policy.MEMORY_KEY_PATTERN.fullmatch(key))
        self.assertEqual(self.service.get_candidate(key).candidate_key, key)
        self.assert_error(
            "invalid_candidate_key",
            self.service.get_candidate,
            "bad/key",
        )
        self.assert_error(
            "invalid_candidate_cursor",
            self.service.list_candidates,
            after_candidate_key="bad/key",
        )

    def test_disabled_and_invalid_configuration_short_circuit_before_db(self):
        disabled = memory_candidate_review.MemoryCandidateReviewService(
            self.reader,
            enabled=False,
            configuration_valid=True,
            error_category="",
        )
        invalid = memory_candidate_review.MemoryCandidateReviewService(
            self.reader,
            enabled=True,
            configuration_valid=False,
            error_category="memory_fingerprint_key_id_missing",
        )
        with mock.patch.object(
            channel_store,
            "connect_read_only",
            side_effect=AssertionError("database opened"),
        ):
            for service, category in (
                (disabled, "candidate_review_disabled"),
                (invalid, "candidate_review_configuration_invalid"),
            ):
                with self.subTest(category=category):
                    with self.assertRaises(
                        memory_candidate_review.MemoryCandidateReviewError
                    ) as ctx:
                        service.list_candidates()
                    self.assertEqual(ctx.exception.category, category)

    def test_list_and_detail_have_fixed_immutable_repr_safe_shapes(self):
        content = "Project Atlas uses Python."
        source = "left context " + content + " right context"
        key = self.persist(content, "project_fact", source_text=source)
        before = self.snapshot()
        listed = self.service.list_candidates()
        detail = self.service.get_candidate(key)
        self.assertEqual(self.snapshot(), before)
        self.assertIsInstance(listed, tuple)
        self.assertEqual(len(listed), 1)
        summary = listed[0]
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(summary)),
            (
                "candidate_key",
                "kind",
                "content_preview",
                "created_at",
                "provenance_count",
            ),
        )
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(detail)),
            (
                "candidate_key",
                "kind",
                "content",
                "scope_type",
                "scope_ref",
                "sensitivity",
                "explicitness",
                "confidence",
                "created_at",
                "provenance_count",
                "evidence",
            ),
        )
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(detail.evidence[0])),
            (
                "signal_type",
                "observed_at",
                "formation_contract_version",
                "extractor_contract_version",
                "source_excerpt",
            ),
        )
        self.assertEqual(summary.candidate_key, key)
        self.assertEqual(summary.content_preview, content)
        self.assertEqual(detail.content, content)
        self.assertEqual(
            (
                detail.scope_type,
                detail.scope_ref,
                detail.sensitivity,
                detail.explicitness,
                detail.confidence,
                detail.provenance_count,
            ),
            ("global_user", "", "normal", "inferred", 0.0, 1),
        )
        serialized_fields = {
            field.name for field in dataclasses.fields(detail.evidence[0])
        }
        self.assertTrue({
            "canonical_message_id", "span_start", "span_end", "fingerprint"
        }.isdisjoint(serialized_fields))
        for value in (repr(summary), repr(detail), repr(detail.evidence[0])):
            self.assertNotIn(content, value)
            self.assertNotIn(key, value)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            detail.content = "changed"

    def test_list_order_filter_limits_and_keyset_pagination_are_fixed(self):
        keys = (
            self.persist("I usually prefer window seats."),
            self.persist("I work as a product designer.", "stable_profile"),
            self.persist("Project Atlas uses Python.", "project_fact"),
            self.persist("We first met in Boston together.", "shared_episode"),
        )
        timestamps = (
            "2026-01-01T00:00:01+00:00",
            "2026-01-01T00:00:02+00:00",
            "2026-01-01T00:00:03+00:00",
            "2026-01-01T00:00:03+00:00",
        )
        with channel_store.connect(self.path) as conn:
            for key, created_at in zip(keys, timestamps):
                conn.execute(
                    "UPDATE memory_items SET created_at=? WHERE memory_key=?",
                    (created_at, key),
                )
            expected = tuple(row[0] for row in conn.execute(
                """SELECT memory_key FROM memory_items WHERE status='candidate'
                   ORDER BY created_at DESC,id DESC"""
            ))
        page_one = self.service.list_candidates(limit=2)
        page_two = self.service.list_candidates(
            limit=2,
            after_candidate_key=page_one[-1].candidate_key,
        )
        self.assertEqual(
            tuple(item.candidate_key for item in page_one + page_two),
            expected,
        )
        projects = self.service.list_candidates(kind="project")
        self.assertEqual(tuple(item.candidate_key for item in projects), (keys[2],))
        self.assertEqual(len(self.service.list_candidates()), 4)
        self.assertEqual(len(self.service.list_candidates(limit=50)), 4)
        for value in (True, 0, 51, 1.0, "20"):
            with self.subTest(limit=value):
                self.assert_error(
                    "invalid_candidate_limit",
                    self.service.list_candidates,
                    limit=value,
                )
        for kind in ("assistant_experience", "unknown", 1):
            with self.subTest(kind=kind):
                self.assert_error(
                    "invalid_candidate_kind",
                    self.service.list_candidates,
                    kind=kind,
                )

    def test_invalid_cursor_is_closed_and_kind_compatible(self):
        preference = self.persist("I usually prefer window seats.")
        project = self.persist("Project Atlas uses Python.", "project_fact")
        self.assert_error(
            "invalid_candidate_cursor",
            self.service.list_candidates,
            after_candidate_key="Z" * 32,
        )
        self.assert_error(
            "invalid_candidate_cursor",
            self.service.list_candidates,
            after_candidate_key=preference,
            kind="project",
        )
        with channel_store.connect(self.path) as conn:
            conn.execute(
                "UPDATE memory_items SET status='active' WHERE memory_key=?",
                (project,),
            )
        self.assert_error(
            "invalid_candidate_cursor",
            self.service.list_candidates,
            after_candidate_key=project,
        )
        self.assert_error(
            "invalid_candidate_cursor",
            self.service.list_candidates,
            after_candidate_key="bad/key",
        )

    def test_detail_uses_only_opaque_candidate_keys_and_hides_other_statuses(self):
        active = self.persist("Project Atlas uses Python.", "project_fact")
        forgotten = self.persist("I usually prefer window seats.")
        with channel_store.connect(self.path) as conn:
            conn.execute(
                "UPDATE memory_items SET status='active' WHERE memory_key=?",
                (active,),
            )
            conn.execute(
                """UPDATE memory_items SET status='forgotten',
                          normalized_content=NULL,normalized_fingerprint=NULL
                   WHERE memory_key=?""",
                (forgotten,),
            )
        self.assertEqual(self.service.list_candidates(), ())
        for key in (active, forgotten, "N" * 32):
            with self.subTest(key=key):
                self.assert_error(
                    "candidate_not_found", self.service.get_candidate, key
                )
        for key in ("", "bad/key", 123, True):
            with self.subTest(key=key):
                self.assert_error(
                    "invalid_candidate_key", self.service.get_candidate, key
                )

    def test_candidate_row_tampering_fails_closed_without_review_mutation(self):
        key = self.persist("Project Atlas uses Python.", "project_fact")
        original = self.candidate_row(key)
        cases = (
            ("explicitness", "explicit"),
            ("confidence", 0.1),
            ("scope_type", "channel"),
            ("sensitivity", "sensitive"),
            ("kind", "assistant_experience"),
            ("fingerprint_version", 99),
            ("normalized_fingerprint", b"x" * 32),
            ("normalized_content", "Project Atlas uses Rust."),
        )
        for column, value in cases:
            with self.subTest(column=column):
                with channel_store.connect(self.path) as conn:
                    conn.execute("PRAGMA ignore_check_constraints=ON")
                    conn.execute(
                        f"UPDATE memory_items SET {column}=? WHERE memory_key=?",
                        (value, key),
                    )
                self.assert_error(
                    "candidate_review_state_invalid",
                    self.service.get_candidate,
                    key,
                )
                with channel_store.connect(self.path) as conn:
                    conn.execute("PRAGMA ignore_check_constraints=ON")
                    conn.execute(
                        f"UPDATE memory_items SET {column}=? WHERE memory_key=?",
                        (original[column], key),
                    )

    def test_profile_mismatch_variants_fail_closed_and_never_initialize(self):
        key = self.persist("Project Atlas uses Python.", "project_fact")
        cases = (
            "DELETE FROM memory_fingerprint_profile",
            "UPDATE memory_fingerprint_profile SET key_id='other-key'",
            "UPDATE memory_fingerprint_profile SET key_check=zeroblob(32)",
            "UPDATE memory_fingerprint_profile SET normalization_version=99",
            "UPDATE memory_fingerprint_profile SET fingerprint_version=99",
        )
        for sql in cases:
            with self.subTest(sql=sql):
                with channel_store.connect(self.path) as conn:
                    original = tuple(conn.execute(
                        "SELECT * FROM memory_fingerprint_profile"
                    ).fetchone())
                    conn.execute(sql)
                self.assert_error(
                    "candidate_review_profile_mismatch",
                    self.service.get_candidate,
                    key,
                )
                with channel_store.connect(self.path) as conn:
                    conn.execute("DELETE FROM memory_fingerprint_profile")
                    conn.execute(
                        """INSERT INTO memory_fingerprint_profile VALUES(
                            ?,?,?,?,?,?,?)""",
                        original,
                    )

    def test_multiple_profile_rows_fail_closed(self):
        key = self.persist("Project Atlas uses Python.", "project_fact")
        with channel_store.connect(self.path) as conn:
            profile = tuple(conn.execute(
                "SELECT * FROM memory_fingerprint_profile"
            ).fetchone())
            conn.execute("PRAGMA ignore_check_constraints=ON")
            conn.execute(
                """INSERT INTO memory_fingerprint_profile VALUES(
                    2,?,?,?,?,?,?)""",
                profile[1:],
            )
        self.assert_error(
            "candidate_review_profile_mismatch",
            self.service.get_candidate,
            key,
        )

    def test_missing_profile_on_empty_database_is_not_initialized(self):
        with channel_store.connect(self.path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM memory_fingerprint_profile"
            ).fetchone()[0], 0)
        self.assert_error(
            "candidate_review_profile_mismatch", self.service.list_candidates
        )
        with channel_store.connect(self.path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM memory_fingerprint_profile"
            ).fetchone()[0], 0)

    def test_zero_provenance_is_unreviewable_for_detail_and_invalidates_list(self):
        key = self.persist("Project Atlas uses Python.", "project_fact")
        self.drop_and_restore_source_trigger(
            "delete",
            "DELETE FROM memory_candidate_sources",
        )
        self.assert_error(
            "candidate_unreviewable", self.service.get_candidate, key
        )
        self.assert_error(
            "candidate_review_state_invalid", self.service.list_candidates
        )

    def test_each_provenance_is_revalidated_against_canonical_phase4a(self):
        content = "I usually prefer window seats."
        key = self.persist(content)
        self.persist(content)
        detail = self.service.get_candidate(key)
        self.assertEqual(detail.provenance_count, 2)
        self.assertEqual(len(detail.evidence), 2)
        with channel_store.connect(self.path) as conn:
            message_id = conn.execute(
                """SELECT canonical_message_id FROM memory_candidate_sources
                   WHERE memory_id=(SELECT id FROM memory_items WHERE memory_key=?)
                   ORDER BY id DESC LIMIT 1""",
                (key,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE messages SET text=? WHERE id=?",
                ("I usually prefer aisle seats.", message_id),
            )
        self.assert_error(
            "candidate_review_state_invalid", self.service.get_candidate, key
        )

    def test_provenance_shape_canonical_binding_and_contracts_fail_closed(self):
        keys = (
            self.persist("Project Atlas uses Python.", "project_fact"),
            self.persist("Project Beacon uses Rust.", "project_fact"),
            self.persist("Project Comet uses Go.", "project_fact"),
            self.persist("Project Delta uses Java.", "project_fact"),
            self.persist("Project Echo uses Kotlin.", "project_fact"),
        )
        mutations = (
            (
                "update",
                "UPDATE memory_candidate_sources SET signal_type='invalid_signal' WHERE memory_id=(SELECT id FROM memory_items WHERE memory_key=?)",
            ),
            (
                "update",
                "UPDATE memory_candidate_sources SET span_end=9999 WHERE memory_id=(SELECT id FROM memory_items WHERE memory_key=?)",
            ),
            (
                "update",
                "UPDATE memory_candidate_sources SET formation_contract_version='bad/value' WHERE memory_id=(SELECT id FROM memory_items WHERE memory_key=?)",
            ),
            (
                "update",
                "UPDATE memory_candidate_sources SET created_at='not-a-timestamp' WHERE memory_id=(SELECT id FROM memory_items WHERE memory_key=?)",
            ),
            (
                "update",
                "UPDATE memory_candidate_sources SET extractor_contract_version='bad/value' WHERE memory_id=(SELECT id FROM memory_items WHERE memory_key=?)",
            ),
        )
        for key, (suffix, sql) in zip(keys, mutations):
            with self.subTest(sql=sql):
                self.drop_and_restore_source_trigger(suffix, sql, (key,))
                self.assert_error(
                    "candidate_review_state_invalid",
                    self.service.get_candidate,
                    key,
                )

    def test_wrong_canonical_direction_and_kind_fail_closed(self):
        direction_key = self.persist("Project Atlas uses Python.", "project_fact")
        kind_key = self.persist("Project Beacon uses Rust.", "project_fact")
        with channel_store.connect(self.path) as conn:
            rows = conn.execute(
                """SELECT s.canonical_message_id,m.memory_key
                   FROM memory_candidate_sources s
                   JOIN memory_items m ON m.id=s.memory_id"""
            ).fetchall()
            message_ids = {row["memory_key"]: row["canonical_message_id"] for row in rows}
            conn.execute(
                "UPDATE messages SET direction='out' WHERE id=?",
                (message_ids[direction_key],),
            )
            conn.execute(
                "UPDATE messages SET kind='voice' WHERE id=?",
                (message_ids[kind_key],),
            )
        for key in (direction_key, kind_key):
            with self.subTest(key=key):
                self.assert_error(
                    "candidate_review_state_invalid",
                    self.service.get_candidate,
                    key,
                )

    def test_missing_or_wrong_canonical_message_fails_closed(self):
        key = self.persist("Project Atlas uses Python.", "project_fact")
        with channel_store.connect(self.path) as conn:
            message_id = conn.execute(
                "SELECT canonical_message_id FROM memory_candidate_sources"
            ).fetchone()[0]
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DELETE FROM messages WHERE id=?", (message_id,))
        self.assert_error(
            "candidate_review_state_invalid", self.service.get_candidate, key
        )

    def test_current_phase4a_rejection_fails_closed(self):
        key = self.persist("I usually prefer window seats.")
        with channel_store.connect(self.path) as conn:
            message_id = conn.execute(
                "SELECT canonical_message_id FROM memory_candidate_sources"
            ).fetchone()[0]
            conn.execute(
                "UPDATE messages SET text=? WHERE id=?",
                ("Maybe I usually prefer window seats.", message_id),
            )
        self.assert_error(
            "candidate_review_state_invalid", self.service.get_candidate, key
        )

    def test_rebuilt_candidate_kind_must_match_stored_candidate(self):
        key = self.persist("Project Atlas uses Python.", "project_fact")
        fingerprint = memory_policy.fingerprint_content(
            TEST_SECRET,
            scope_type="global_user",
            scope_ref="",
            kind="decision",
            normalized_content="Project Atlas uses Python.",
        )
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """UPDATE memory_items SET kind='decision',normalized_fingerprint=?
                   WHERE memory_key=?""",
                (fingerprint, key),
            )
        self.assert_error(
            "candidate_review_state_invalid", self.service.get_candidate, key
        )

    def test_candidate_content_must_match_every_phase4a_proof(self):
        key = self.persist("Project Atlas uses Python.", "project_fact")
        replacement = "Project Atlas uses Rust."
        fingerprint = memory_policy.fingerprint_content(
            TEST_SECRET,
            scope_type="global_user",
            scope_ref="",
            kind="project",
            normalized_content=replacement,
        )
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """UPDATE memory_items
                   SET normalized_content=?,normalized_fingerprint=?
                   WHERE memory_key=?""",
                (replacement, fingerprint, key),
            )
        self.assert_error(
            "candidate_review_state_invalid", self.service.get_candidate, key
        )

    def test_evidence_excerpt_is_bounded_and_contains_the_complete_span(self):
        content = "Project Atlas uses Python."
        left = "L" * 200
        right = "R" * 200
        key = self.persist(
            content,
            "project_fact",
            source_text=left + content + right,
        )
        excerpt = self.service.get_candidate(key).evidence[0].source_excerpt
        self.assertEqual(excerpt, left[-160:] + content + right[:160])
        self.assertLessEqual(len(excerpt), 2320)

    def test_oversized_raw_proposal_span_fails_instead_of_truncating(self):
        content = "I usually prefer" + (" " * 2001) + "window seats."
        self.assertGreater(len(content), 2000)
        key = self.persist(content)
        self.assert_error(
            "candidate_review_state_invalid", self.service.get_candidate, key
        )

    def test_preview_is_deterministic_and_hard_bounded(self):
        content = "I usually prefer " + ("window seats " * 25)
        key = self.persist(content)
        summary = self.service.list_candidates()[0]
        normalized = memory_policy.normalize_content(content, max_chars=1000)
        self.assertEqual(summary.candidate_key, key)
        self.assertEqual(summary.content_preview, normalized[:239] + "…")
        self.assertEqual(len(summary.content_preview), 240)

    def test_page_with_one_invalid_candidate_fails_closed_without_skipping(self):
        self.persist("I usually prefer window seats.")
        bad = self.persist("Project Atlas uses Python.", "project_fact")
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """UPDATE memory_items SET normalized_fingerprint=zeroblob(32)
                   WHERE memory_key=?""",
                (bad,),
            )
        self.assert_error(
            "candidate_review_state_invalid", self.service.list_candidates
        )

    def test_schema_storage_and_read_only_connection_fail_closed(self):
        self.persist("Project Atlas uses Python.", "project_fact")
        with self.reader._connect_read_only() as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute(
                    "UPDATE memory_items SET sensitivity='normal'"
                )

        with channel_store.connect(self.path) as conn:
            conn.execute("DROP INDEX idx_memory_candidate_sources_canonical")
        self.assert_error(
            "candidate_review_schema_invalid", self.service.list_candidates
        )

        missing = memory_candidate_review.MemoryCandidateReviewReader(
            str(Path(self.temp.name) / "missing.sqlite3"),
            fingerprint_key_id=KEY_ID,
            fingerprint_hmac_secret=TEST_SECRET,
            max_item_chars=1000,
        )
        service = memory_candidate_review.MemoryCandidateReviewService(
            missing,
            enabled=True,
            configuration_valid=True,
            error_category="",
        )
        with self.assertRaises(memory_candidate_review.MemoryCandidateReviewError) as ctx:
            service.list_candidates()
        self.assertEqual(ctx.exception.category, "storage_unavailable")

    def test_active_read_service_regression_excludes_candidate(self):
        candidate = self.persist("I usually prefer window seats.")
        active = self.persist("Project Atlas uses Python.", "project_fact")
        with channel_store.connect(self.path) as conn:
            conn.execute(
                "UPDATE memory_items SET status='active' WHERE memory_key=?",
                (active,),
            )
        result = self.runtime.read_service.get_active_memories(
            scope_type="global_user",
            scope_ref="",
        )
        self.assertEqual(tuple(item["memory_key"] for item in result), (active,))
        self.assertNotIn(candidate, json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
