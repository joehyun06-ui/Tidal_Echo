from __future__ import annotations

import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import (
    memory_hierarchy_baseline,
    memory_hierarchy_projection as hierarchy,
    memory_hierarchy_snapshot,
    memory_retrieval_vector as vector,
    memory_retrieval_vector_rebuild as rebuild,
    memory_retrieval_vector_store as store,
)


MODEL = "test/embedding-model-v1"
DIMS = 3
K1 = "vector_atomic_memory_000001"
K2 = "vector_atomic_memory_000002"
S1 = "vector_atomic_memory_000003"
PS1 = "vector_atomic_memory_000004"


def atomic(
    key: str,
    content: str,
    *,
    sensitivity: str = "normal",
    scope_type: str = "global_user",
    scope_ref: str = "",
):
    return hierarchy.AtomicMemoryProjectionInputV1(
        memory_key=key,
        kind="project",
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


def atomics():
    return (
        atomic(K1, "Backend runs on Render."),
        atomic(K2, "Frontend runs on Vercel."),
        atomic(S1, "Sensitive private vector content.", sensitivity="sensitive"),
        atomic(
            PS1,
            "Project scoped vector content.",
            scope_type="project",
            scope_ref="tidal-echo",
        ),
    )


def digest(items=None):
    values = atomics() if items is None else items
    return memory_hierarchy_baseline.build_baseline_hierarchy_plan_v1(
        values
    ).atomic_snapshot_digest


async def deterministic_embed(texts, model, dimensions):
    if model != MODEL or dimensions != DIMS:
        raise AssertionError("unexpected embedding contract")
    mapping = {
        "Backend runs on Render.": [1.0, 0.0, 0.0],
        "Frontend runs on Vercel.": [0.0, 1.0, 0.0],
        "render query": [1.0, 0.1, 0.0],
        "frontend query": [0.1, 1.0, 0.0],
    }
    return [mapping[text] for text in texts]


class VectorContractTests(unittest.IsolatedAsyncioTestCase):
    def assert_error(self, category: str, callable_, *args, **kwargs):
        with self.assertRaises(vector.MemoryRetrievalVectorError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.category, category)

    async def test_provider_sees_only_normal_global_plaintext_and_never_memory_keys(self):
        calls = []

        async def embed(texts, model, dimensions):
            calls.append((texts, model, dimensions))
            self.assertEqual(
                texts,
                ("Backend runs on Render.", "Frontend runs on Vercel."),
            )
            rendered = repr(texts)
            self.assertNotIn(K1, rendered)
            self.assertNotIn(K2, rendered)
            self.assertNotIn("Sensitive private vector content", rendered)
            self.assertNotIn("Project scoped vector content", rendered)
            return [[1, 0, 0], [0, 1, 0]]

        built = await vector.build_vector_index_v1(
            embed,
            atomics(),
            source_snapshot_digest=digest(),
            embedding_model=MODEL,
            dimensions=DIMS,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(built.provider_call_count, 1)
        self.assertEqual(
            tuple(document.memory_key for document in built.plan.documents),
            (K1, K2),
        )
        for document in built.plan.documents:
            norm = math.sqrt(sum(value * value for value in document.vector))
            self.assertAlmostEqual(norm, 1.0, places=5)
        rendered = repr(built) + " " + repr(built.plan)
        self.assertNotIn(K1, rendered)
        self.assertNotIn("Render", rendered)

    async def test_provider_output_is_bound_by_ordinal_and_invalid_shapes_fail_closed(self):
        async def swapped(texts, _model, _dimensions):
            self.assertEqual(len(texts), 2)
            return [[0, 1, 0], [1, 0, 0]]

        built = await vector.build_vector_index_v1(
            swapped,
            atomics(),
            source_snapshot_digest=digest(),
            embedding_model=MODEL,
            dimensions=DIMS,
        )
        self.assertEqual(built.plan.documents[0].memory_key, K1)
        self.assertEqual(built.plan.documents[0].vector, (0.0, 1.0, 0.0))
        self.assertEqual(built.plan.documents[1].memory_key, K2)
        self.assertEqual(built.plan.documents[1].vector, (1.0, 0.0, 0.0))

        async def wrong_count(_texts, _model, _dimensions):
            return [[1, 0, 0]]

        with self.assertRaises(vector.MemoryRetrievalVectorError) as raised:
            await vector.build_vector_index_v1(
                wrong_count,
                atomics(),
                source_snapshot_digest=digest(),
                embedding_model=MODEL,
                dimensions=DIMS,
            )
        self.assertEqual(raised.exception.category, "embedding_invalid_output")

        async def bad_dimension(texts, _model, _dimensions):
            return [[1, 0] for _ in texts]

        with self.assertRaises(vector.MemoryRetrievalVectorError) as raised:
            await vector.build_vector_index_v1(
                bad_dimension,
                atomics(),
                source_snapshot_digest=digest(),
                embedding_model=MODEL,
                dimensions=DIMS,
            )
        self.assertEqual(raised.exception.category, "embedding_invalid_output")

        async def nonfinite(texts, _model, _dimensions):
            return [[float("nan"), 0, 0] for _ in texts]

        with self.assertRaises(vector.MemoryRetrievalVectorError) as raised:
            await vector.build_vector_index_v1(
                nonfinite,
                atomics(),
                source_snapshot_digest=digest(),
                embedding_model=MODEL,
                dimensions=DIMS,
            )
        self.assertEqual(raised.exception.category, "embedding_invalid_output")

    async def test_embedding_batches_are_bounded_and_ordered(self):
        many = tuple(
            atomic(
                f"vector_batch_atomic_{index:06d}",
                f"document {index}",
            )
            for index in range(35)
        )
        seen = []

        async def embed(texts, model, dimensions):
            seen.append(texts)
            self.assertEqual(model, MODEL)
            self.assertEqual(dimensions, DIMS)
            self.assertLessEqual(len(texts), vector.MAX_EMBEDDING_BATCH)
            return [[1.0, float(index + 1), 0.0] for index, _ in enumerate(texts)]

        built = await vector.build_vector_index_v1(
            embed,
            many,
            source_snapshot_digest=digest(many),
            embedding_model=MODEL,
            dimensions=DIMS,
        )
        self.assertEqual(tuple(len(batch) for batch in seen), (32, 3))
        self.assertEqual(built.provider_call_count, 2)
        self.assertEqual(built.plan.document_count, 35)

    async def test_query_embedding_and_cosine_search_return_keys_only(self):
        built = await vector.build_vector_index_v1(
            deterministic_embed,
            atomics(),
            source_snapshot_digest=digest(),
            embedding_model=MODEL,
            dimensions=DIMS,
        )
        query = await vector.embed_query_vector_v1(
            deterministic_embed,
            "render query",
            embedding_model=MODEL,
            dimensions=DIMS,
        )
        result = vector.search_vector_index_v1(
            built.plan,
            query,
            minimum_similarity=0.0,
        )
        self.assertEqual(
            tuple(hit.memory_key for hit in result.hits),
            (K1, K2),
        )
        self.assertGreater(result.hits[0].similarity, result.hits[1].similarity)
        rendered = repr(query) + " " + repr(result) + " " + " ".join(
            repr(hit) for hit in result.hits
        )
        self.assertNotIn(K1, rendered)
        self.assertNotIn("render query", rendered)

    async def test_empty_eligible_set_skips_provider(self):
        only_sensitive = (
            atomic(S1, "Sensitive only", sensitivity="sensitive"),
        )
        calls = []

        async def forbidden(*_args):
            calls.append(1)
            return []

        built = await vector.build_vector_index_v1(
            forbidden,
            only_sensitive,
            source_snapshot_digest=digest(only_sensitive),
            embedding_model=MODEL,
            dimensions=DIMS,
        )
        self.assertEqual(calls, [])
        self.assertEqual(built.provider_call_count, 0)
        self.assertEqual(built.plan.documents, ())


class VectorStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "memory-vector.db"
        self.authority = self.root / "relay.db"
        self.build = await vector.build_vector_index_v1(
            deterministic_embed,
            atomics(),
            source_snapshot_digest=digest(),
            embedding_model=MODEL,
            dimensions=DIMS,
        )

    def assert_store_error(self, category: str, callable_, *args, **kwargs):
        with self.assertRaises(store.MemoryRetrievalVectorStoreError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.category, category)

    async def test_float32_store_round_trip_is_plaintext_free_and_searchable(self):
        store.initialize_vector_store(
            self.path,
            forbidden_paths=(self.authority,),
        )
        snapshot = store.apply_vector_index_plan(self.path, self.build.plan)
        self.assertEqual(snapshot.plan, self.build.plan)
        raw = self.path.read_bytes()
        for forbidden in (
            b"Backend runs on Render",
            b"Frontend runs on Vercel",
            b"Sensitive private vector content",
        ):
            self.assertNotIn(forbidden, raw)
        query = await vector.embed_query_vector_v1(
            deterministic_embed,
            "frontend query",
            embedding_model=MODEL,
            dimensions=DIMS,
        )
        result = store.search_vector_store(
            self.path,
            query,
            expected_source_snapshot_digest=digest(),
        )
        self.assertEqual(result.hits[0].memory_key, K2)

    def test_foreign_schema_and_path_alias_fail_closed(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY)")
            conn.commit()
        self.assert_store_error(
            "vector_index_schema_invalid",
            store.initialize_vector_store,
            self.path,
        )
        self.assert_store_error(
            "vector_index_path_invalid",
            store.initialize_vector_store,
            self.authority,
            forbidden_paths=(self.authority,),
        )

    def test_vector_blob_corruption_is_detected(self):
        store.initialize_vector_store(self.path)
        store.apply_vector_index_plan(self.path, self.build.plan)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE vector_documents SET vector_blob=x'0000' "
                "WHERE memory_key=?",
                (K1,),
            )
            conn.commit()
        self.assert_store_error(
            "vector_index_schema_invalid",
            store.load_vector_store_snapshot,
            self.path,
        )

    def test_delete_and_rebuild_reproduces_exact_plan(self):
        store.initialize_vector_store(self.path)
        first = store.apply_vector_index_plan(self.path, self.build.plan)
        self.path.unlink()
        store.initialize_vector_store(self.path)
        second = store.apply_vector_index_plan(self.path, self.build.plan)
        self.assertEqual(first.plan, second.plan)
        self.assertEqual(second.generation, 1)


class VectorRebuildTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.authority = self.root / "relay.db"
        self.path = self.root / "vector.db"
        reader = object.__new__(
            memory_hierarchy_snapshot.MemoryHierarchySnapshotReader
        )
        object.__setattr__(reader, "_database_path", str(self.authority))
        self.reader = reader
        self.snapshot = memory_hierarchy_snapshot.HierarchyAtomicSnapshotV1(
            atomics=atomics()
        )

    async def test_source_or_embedding_failure_leaves_no_sidecar(self):
        with mock.patch.object(
            memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
            "load_active_snapshot",
            side_effect=memory_hierarchy_snapshot.MemoryHierarchySnapshotError(
                "storage_unavailable"
            ),
        ):
            with self.assertRaises(rebuild.MemoryRetrievalVectorRebuildError) as raised:
                await rebuild.rebuild_vector_index_v1(
                    self.reader,
                    self.path,
                    deterministic_embed,
                    embedding_model=MODEL,
                    dimensions=DIMS,
                )
        self.assertEqual(raised.exception.category, "vector_rebuild_source_invalid")
        self.assertFalse(self.path.exists())

        async def unavailable(*_args):
            raise RuntimeError("provider private detail")

        with mock.patch.object(
            memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
            "load_active_snapshot",
            return_value=self.snapshot,
        ):
            with self.assertRaises(rebuild.MemoryRetrievalVectorRebuildError) as raised:
                await rebuild.rebuild_vector_index_v1(
                    self.reader,
                    self.path,
                    unavailable,
                    embedding_model=MODEL,
                    dimensions=DIMS,
                )
        self.assertEqual(raised.exception.category, "vector_rebuild_embedding_failed")
        self.assertFalse(self.path.exists())

    async def test_complete_rebuild_materializes_current_revision(self):
        with mock.patch.object(
            memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
            "load_active_snapshot",
            return_value=self.snapshot,
        ):
            receipt = await rebuild.rebuild_vector_index_v1(
                self.reader,
                self.path,
                deterministic_embed,
                embedding_model=MODEL,
                dimensions=DIMS,
            )
        self.assertEqual(receipt.source_atomic_count, 4)
        self.assertEqual(receipt.indexed_document_count, 2)
        self.assertEqual(receipt.provider_call_count, 1)
        snapshot = store.load_vector_store_snapshot(self.path)
        self.assertEqual(snapshot.plan.source_snapshot_digest, digest())
        self.assertEqual(snapshot.plan.embedding_model, MODEL)

    async def test_authoritative_db_cannot_be_vector_sidecar(self):
        with self.assertRaises(rebuild.MemoryRetrievalVectorRebuildError) as raised:
            await rebuild.rebuild_vector_index_v1(
                self.reader,
                self.authority,
                deterministic_embed,
                embedding_model=MODEL,
                dimensions=DIMS,
            )
        self.assertEqual(
            raised.exception.category,
            "vector_rebuild_configuration_invalid",
        )

    def test_c3_remains_unwired_to_context_and_runtime(self):
        root = Path(__file__).resolve().parents[2]
        context_source = (
            root / "backend" / "memory_context_integration.py"
        ).read_text(encoding="utf-8")
        relay_source = (
            root / "backend" / "p3_relay_app.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("memory_retrieval_vector", context_source)
        self.assertNotIn("memory_retrieval_vector", relay_source)


if __name__ == "__main__":
    unittest.main()
