from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import (
    memory_hierarchy_projection as hierarchy,
    memory_hierarchy_snapshot,
    memory_retrieval_bm25 as bm25,
    memory_retrieval_bm25_rebuild as rebuild,
    memory_retrieval_bm25_store as store,
)


SECRET = "Bm25-Test-HMAC-Secret-0123456789-AbCd!"
OTHER_SECRET = "Bm25-Other-HMAC-Secret-9876543210-ZyXw!"
KEY_ID = "test-key-v1"
DIGEST = "a" * 64

K1 = "bm25_atomic_memory_000001"
K2 = "bm25_atomic_memory_000002"
K3 = "bm25_atomic_memory_000003"
K4 = "bm25_atomic_memory_000004"
K5 = "bm25_atomic_memory_000005"


def atomic(
    key: str,
    content: str,
    *,
    kind: str = "project",
    sensitivity: str = "normal",
    scope_type: str = "global_user",
    scope_ref: str = "",
):
    return hierarchy.AtomicMemoryProjectionInputV1(
        memory_key=key,
        kind=kind,
        scope_type=scope_type,
        scope_ref=scope_ref,
        normalized_content=content,
        fingerprint_version=1,
        status="active",
        explicitness="inferred",
        confidence=1.0,
        sensitivity=sensitivity,
        first_observed_at="2026-09-01T08:00:00+00:00",
        last_confirmed_at="2026-09-01T08:00:00+00:00",
        updated_at="2026-09-01T08:00:00+00:00",
    )


def sample_atomics():
    return (
        atomic(K1, "Render render render Python."),
        atomic(K2, "Render Python."),
        atomic(K3, "Python database."),
        atomic(K4, "private secret project", sensitivity="sensitive"),
        atomic(
            K5,
            "project scoped lexical material",
            scope_type="project",
            scope_ref="tidal-echo",
        ),
    )


class BM25PureContractTests(unittest.TestCase):
    def assert_error(self, category: str, callable_, *args, **kwargs):
        with self.assertRaises(bm25.MemoryRetrievalBM25Error) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.category, category)

    def test_tokenizer_is_deterministic_multilingual_and_preserves_frequency(self):
        terms = bm25.tokenize_lexical_terms_v1("Render RENDER，归汀记忆")
        self.assertEqual(terms.count("a:render"), 2)
        self.assertIn("c:归", terms)
        self.assertIn("c:汀", terms)
        self.assertIn("b:归汀", terms)
        self.assertIn("b:汀记", terms)
        self.assertEqual(
            terms,
            bm25.tokenize_lexical_terms_v1("Render RENDER，归汀记忆"),
        )

    def test_index_excludes_non_normal_and_non_global_without_partial_plaintext(self):
        plan = bm25.build_bm25_index_v1(
            sample_atomics(),
            source_snapshot_digest=DIGEST,
            term_key_id=KEY_ID,
            term_hmac_secret=SECRET,
        )
        self.assertEqual(
            tuple(document.memory_key for document in plan.documents),
            (K1, K2, K3),
        )
        rendered = repr(plan)
        for forbidden in (
            "Render",
            "Python",
            "database",
            "private secret",
            K1,
            K2,
            K3,
        ):
            self.assertNotIn(forbidden, rendered)

    def test_term_hashes_are_keyed_domain_separated_and_not_plaintext(self):
        first = bm25.build_bm25_index_v1(
            (atomic(K1, "Render Python"),),
            source_snapshot_digest=DIGEST,
            term_key_id=KEY_ID,
            term_hmac_secret=SECRET,
        )
        second = bm25.build_bm25_index_v1(
            (atomic(K1, "Render Python"),),
            source_snapshot_digest=DIGEST,
            term_key_id=KEY_ID,
            term_hmac_secret=OTHER_SECRET,
        )
        first_hashes = tuple(
            posting.term_hash for posting in first.documents[0].postings
        )
        second_hashes = tuple(
            posting.term_hash for posting in second.documents[0].postings
        )
        self.assertNotEqual(first_hashes, second_hashes)
        self.assertTrue(all(len(value) == 32 for value in first_hashes))
        self.assertTrue(all(b"render" not in value for value in first_hashes))

    def test_bm25_ranks_repeated_relevant_term_and_returns_keys_only(self):
        plan = bm25.build_bm25_index_v1(
            sample_atomics(),
            source_snapshot_digest=DIGEST,
            term_key_id=KEY_ID,
            term_hmac_secret=SECRET,
        )
        result = bm25.search_bm25_index_v1(
            plan,
            "render",
            term_key_id=KEY_ID,
            term_hmac_secret=SECRET,
        )
        self.assertEqual(tuple(hit.memory_key for hit in result.hits), (K1, K2))
        self.assertGreater(result.hits[0].score, result.hits[1].score)
        self.assertEqual(result.query_term_count, 1)
        self.assertNotIn("render", repr(result).lower())

    def test_empty_signal_and_wrong_key_fail_or_return_empty_without_content(self):
        plan = bm25.build_bm25_index_v1(
            (atomic(K1, "Render Python"),),
            source_snapshot_digest=DIGEST,
            term_key_id=KEY_ID,
            term_hmac_secret=SECRET,
        )
        empty = bm25.search_bm25_index_v1(
            plan,
            "   !!!  ",
            term_key_id=KEY_ID,
            term_hmac_secret=SECRET,
        )
        self.assertEqual(empty.hits, ())
        self.assert_error(
            "invalid_term_key",
            bm25.search_bm25_index_v1,
            plan,
            "render",
            term_key_id="wrong-key",
            term_hmac_secret=SECRET,
        )

    def test_plan_rejects_tampered_term_frequency_and_document_frequency(self):
        plan = bm25.build_bm25_index_v1(
            (atomic(K1, "Render Python"),),
            source_snapshot_digest=DIGEST,
            term_key_id=KEY_ID,
            term_hmac_secret=SECRET,
        )
        document = plan.documents[0]
        bad_posting = dataclasses.replace(
            document.postings[0],
            term_frequency=document.postings[0].term_frequency + 1,
        )
        bad_document = dataclasses.replace(
            document,
            postings=(bad_posting, *document.postings[1:]),
        )
        tampered = dataclasses.replace(plan, documents=(bad_document,))
        self.assert_error(
            "invalid_index_plan",
            bm25.validate_bm25_index_plan_v1,
            tampered,
        )


class BM25StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "memory-retrieval-bm25.db"
        self.authority = self.root / "relay.db"

    def plan(self):
        return bm25.build_bm25_index_v1(
            sample_atomics(),
            source_snapshot_digest=DIGEST,
            term_key_id=KEY_ID,
            term_hmac_secret=SECRET,
        )

    def assert_store_error(self, category: str, callable_, *args, **kwargs):
        with self.assertRaises(store.MemoryRetrievalBM25StoreError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.category, category)

    def test_store_round_trip_is_plaintext_free_and_searchable(self):
        store.initialize_bm25_store(
            self.path,
            forbidden_paths=(self.authority,),
        )
        snapshot = store.apply_bm25_index_plan(self.path, self.plan())
        self.assertEqual(snapshot.generation, 1)
        self.assertEqual(snapshot.plan.document_count, 3)
        raw = self.path.read_bytes().lower()
        for forbidden in (
            b"render",
            b"python",
            b"database",
            b"private secret",
            b"project scoped lexical",
        ):
            self.assertNotIn(forbidden, raw)
        result = store.search_bm25_store(
            self.path,
            "database",
            term_key_id=KEY_ID,
            term_hmac_secret=SECRET,
            expected_source_snapshot_digest=DIGEST,
        )
        self.assertEqual(tuple(hit.memory_key for hit in result.hits), (K3,))

    def test_foreign_or_corrupt_schema_is_not_adopted_or_repaired(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY)")
            conn.commit()
        self.assert_store_error(
            "bm25_index_schema_invalid",
            store.initialize_bm25_store,
            self.path,
        )
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                },
                {"unrelated"},
            )

    def test_source_digest_mismatch_and_path_alias_fail_closed(self):
        store.initialize_bm25_store(self.path)
        store.apply_bm25_index_plan(self.path, self.plan())
        self.assert_store_error(
            "bm25_index_invalid",
            store.search_bm25_store,
            self.path,
            "render",
            term_key_id=KEY_ID,
            term_hmac_secret=SECRET,
            expected_source_snapshot_digest="b" * 64,
        )
        self.assert_store_error(
            "bm25_index_path_invalid",
            store.initialize_bm25_store,
            self.authority,
            forbidden_paths=(self.authority,),
        )

    def test_posting_corruption_is_detected_on_read(self):
        store.initialize_bm25_store(self.path)
        store.apply_bm25_index_plan(self.path, self.plan())
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE bm25_postings SET term_frequency=term_frequency+1 "
                "WHERE rowid=(SELECT rowid FROM bm25_postings LIMIT 1)"
            )
            conn.commit()
        self.assert_store_error(
            "bm25_index_schema_invalid",
            store.load_bm25_store_snapshot,
            self.path,
        )

    def test_delete_and_rebuild_reproduces_exact_plan(self):
        plan = self.plan()
        store.initialize_bm25_store(self.path)
        first = store.apply_bm25_index_plan(self.path, plan)
        self.path.unlink()
        store.initialize_bm25_store(self.path)
        second = store.apply_bm25_index_plan(self.path, plan)
        self.assertEqual(first.plan, second.plan)
        self.assertEqual(second.generation, 1)


class BM25RebuildCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.index_path = self.root / "bm25.db"
        self.authority = self.root / "relay.db"
        reader = object.__new__(
            memory_hierarchy_snapshot.MemoryHierarchySnapshotReader
        )
        object.__setattr__(reader, "_database_path", str(self.authority))
        self.reader = reader

    def test_source_failure_does_not_create_sidecar(self):
        with mock.patch.object(
            memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
            "load_active_snapshot",
            side_effect=memory_hierarchy_snapshot.MemoryHierarchySnapshotError(
                "storage_unavailable"
            ),
        ):
            with self.assertRaises(rebuild.MemoryRetrievalBM25RebuildError) as raised:
                rebuild.rebuild_bm25_index_v1(
                    self.reader,
                    self.index_path,
                    term_key_id=KEY_ID,
                    term_hmac_secret=SECRET,
                )
        self.assertEqual(raised.exception.category, "bm25_rebuild_source_invalid")
        self.assertFalse(self.index_path.exists())

    def test_complete_snapshot_rebuild_and_query(self):
        snapshot = memory_hierarchy_snapshot.HierarchyAtomicSnapshotV1(
            atomics=sample_atomics()
        )
        with mock.patch.object(
            memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
            "load_active_snapshot",
            return_value=snapshot,
        ):
            receipt = rebuild.rebuild_bm25_index_v1(
                self.reader,
                self.index_path,
                term_key_id=KEY_ID,
                term_hmac_secret=SECRET,
            )
        self.assertEqual(receipt.source_atomic_count, 5)
        self.assertEqual(receipt.indexed_document_count, 3)
        stored = store.load_bm25_store_snapshot(self.index_path)
        self.assertEqual(stored.generation, receipt.generation)
        result = store.search_bm25_store(
            self.index_path,
            "render",
            term_key_id=KEY_ID,
            term_hmac_secret=SECRET,
            expected_source_snapshot_digest=stored.plan.source_snapshot_digest,
        )
        self.assertEqual(tuple(hit.memory_key for hit in result.hits), (K1, K2))

    def test_rebuild_refuses_authoritative_db_as_index_path(self):
        with self.assertRaises(rebuild.MemoryRetrievalBM25RebuildError) as raised:
            rebuild.rebuild_bm25_index_v1(
                self.reader,
                self.authority,
                term_key_id=KEY_ID,
                term_hmac_secret=SECRET,
            )
        self.assertEqual(
            raised.exception.category,
            "bm25_rebuild_configuration_invalid",
        )

    def test_c1_is_unwired_to_context_or_runtime(self):
        root = Path(__file__).resolve().parents[2]
        context_source = (
            root / "backend" / "memory_context_integration.py"
        ).read_text(encoding="utf-8")
        relay_source = (
            root / "backend" / "p3_relay_app.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("memory_retrieval_bm25", context_source)
        self.assertNotIn("memory_retrieval_bm25", relay_source)


if __name__ == "__main__":
    unittest.main()
