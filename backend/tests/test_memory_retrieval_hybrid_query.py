from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import (
    memory_hierarchy_baseline,
    memory_hierarchy_projection as hierarchy,
    memory_hierarchy_snapshot,
    memory_retrieval_bm25 as bm25,
    memory_retrieval_bm25_store as bm25_store,
    memory_retrieval_hybrid_query as query,
    memory_retrieval_vector as vector,
    memory_retrieval_vector_store as vector_store,
)


SECRET = "Hybrid-D3A-BM25-Secret-0123456789-AbCd!"
KEY_ID = "hybrid-d3a-test-key"
MODEL = "test-embedding-v1"
DIMS = 2
REFERENCE_TIME = "2026-09-01T12:00:00+00:00"
QUERY = "CODEX_GENERATION_ENABLED dep-daak91hf2nfc73ak97p0"

K1 = "hybrid_query_atomic_000001"
K2 = "hybrid_query_atomic_000002"
K3 = "hybrid_query_atomic_000003"


def atomic(key: str, content: str, *, sensitivity: str = "normal"):
    return hierarchy.AtomicMemoryProjectionInputV1(
        memory_key=key,
        kind="project",
        scope_type="global_user",
        scope_ref="",
        normalized_content=content,
        fingerprint_version=1,
        status="active",
        explicitness="inferred",
        confidence=0.9,
        sensitivity=sensitivity,
        first_observed_at="2026-08-01T00:00:00+00:00",
        last_confirmed_at="2026-08-30T00:00:00+00:00",
        updated_at="2026-08-30T00:00:00+00:00",
    )


def atomics():
    return (
        atomic(K1, "Render deployment CODEX_GENERATION_ENABLED dep-daak91hf2nfc73ak97p0"),
        atomic(K2, "Android frontend release planning"),
        atomic(K3, "private internal project", sensitivity="sensitive"),
    )


def digest():
    return memory_hierarchy_baseline.build_baseline_hierarchy_plan_v1(
        atomics()
    ).atomic_snapshot_digest


def vector_plan(*, source_digest=None, forged_revision=False):
    documents = []
    vectors = {K1: (1.0, 0.0), K2: (0.0, 1.0)}
    for item in atomics():
        if item.sensitivity != "normal":
            continue
        revision = hierarchy._atomic_revision_digest(item)
        if forged_revision and item.memory_key == K1:
            revision = "f" * 64
        documents.append(vector.VectorDocumentPlanV1(
            memory_key=item.memory_key,
            atomic_revision_digest=revision,
            vector=vectors[item.memory_key],
        ))
    return vector.validate_vector_index_plan_v1(vector.VectorIndexPlanV1(
        contract_version=vector.VECTOR_CONTRACT_VERSION,
        embedding_contract_version=vector.EMBEDDING_CONTRACT_VERSION,
        source_snapshot_digest=source_digest or digest(),
        embedding_model=MODEL,
        dimensions=DIMS,
        documents=tuple(sorted(documents, key=lambda item: item.memory_key)),
    ))


class RecordingEmbedder:
    def __init__(self, *, failure: Exception | None = None, output=None):
        self.calls = []
        self.failure = failure
        self.output = output if output is not None else ((1.0, 0.0),)

    async def __call__(self, texts, model, dimensions):
        self.calls.append((texts, model, dimensions))
        if self.failure is not None:
            raise self.failure
        return self.output


class HybridQueryCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.authority = self.root / "relay.db"
        self.bm25_path = self.root / "memory-bm25.db"
        self.vector_path = self.root / "memory-vector.db"
        reader = object.__new__(memory_hierarchy_snapshot.MemoryHierarchySnapshotReader)
        object.__setattr__(reader, "_database_path", str(self.authority))
        self.reader = reader
        self.snapshot = memory_hierarchy_snapshot.HierarchyAtomicSnapshotV1(
            atomics=atomics()
        )

    def install_bm25(self, *, source_digest=None):
        plan = bm25.build_bm25_index_v1(
            atomics(),
            source_snapshot_digest=source_digest or digest(),
            term_key_id=KEY_ID,
            term_hmac_secret=SECRET,
        )
        bm25_store.initialize_bm25_store(self.bm25_path)
        return bm25_store.apply_bm25_index_plan(self.bm25_path, plan)

    def install_vector(self, *, source_digest=None, forged_revision=False):
        vector_store.initialize_vector_store(self.vector_path)
        return vector_store.apply_vector_index_plan(
            self.vector_path,
            vector_plan(
                source_digest=source_digest,
                forged_revision=forged_revision,
            ),
        )

    async def call(self, embedder, **overrides):
        kwargs = dict(
            query_text=QUERY,
            reference_time=REFERENCE_TIME,
            bm25_sidecar_path=self.bm25_path,
            term_key_id=KEY_ID,
            term_hmac_secret=SECRET,
            vector_sidecar_path=self.vector_path,
        )
        kwargs.update(overrides)
        with mock.patch.object(
            memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
            "load_active_snapshot",
            return_value=self.snapshot,
        ):
            return await query.fuse_current_hybrid_query_v1(
                self.reader,
                embedder,
                **kwargs,
            )

    async def assert_query_error(self, category, embedder, **overrides):
        with self.assertRaises(query.MemoryRetrievalHybridQueryError) as raised:
            await self.call(embedder, **overrides)
        self.assertEqual(raised.exception.category, category)
        return raised.exception

    async def test_exact_query_is_embedded_once_using_proved_sidecar_identity(self):
        bm = self.install_bm25()
        vec = self.install_vector()
        embedder = RecordingEmbedder()
        result = await self.call(embedder)
        self.assertEqual(result.contract_version, query.HYBRID_QUERY_CONTRACT_VERSION)
        self.assertEqual(result.bm25_generation, bm.generation)
        self.assertEqual(result.vector_generation, vec.generation)
        self.assertTrue(result.query_embedding_performed)
        self.assertEqual(embedder.calls, [((QUERY,), MODEL, DIMS)])
        self.assertEqual(result.fusion_result.hits[0].memory_key, K1)
        rendered = repr(result)
        self.assertNotIn(K1, rendered)
        self.assertNotIn("CODEX_GENERATION_ENABLED", rendered)

    async def test_no_vector_sidecar_performs_no_embedding(self):
        self.install_bm25()
        with mock.patch.object(
            memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
            "load_active_snapshot",
            return_value=self.snapshot,
        ):
            result = await query.fuse_current_hybrid_query_v1(
                self.reader,
                None,
                query_text=QUERY,
                reference_time=REFERENCE_TIME,
                bm25_sidecar_path=self.bm25_path,
                term_key_id=KEY_ID,
                term_hmac_secret=SECRET,
            )
        self.assertFalse(result.query_embedding_performed)
        self.assertIsNone(result.vector_generation)
        self.assertFalse(result.fusion_result.vector_available)
        self.assertEqual(result.fusion_result.hits[0].memory_key, K1)

    async def test_all_local_proof_precedes_provider_call(self):
        self.install_bm25(source_digest="b" * 64)
        self.install_vector()
        embedder = RecordingEmbedder()
        await self.assert_query_error("hybrid_query_stale", embedder)
        self.assertEqual(embedder.calls, [])

        # Fresh BM25 but forged vector binding must also fail before provider I/O.
        self.bm25_path.unlink()
        self.vector_path.unlink()
        self.install_bm25()
        self.install_vector(forged_revision=True)
        embedder = RecordingEmbedder()
        await self.assert_query_error("hybrid_query_vector_invalid", embedder)
        self.assertEqual(embedder.calls, [])

    async def test_embedding_failure_is_bounded_and_data_free(self):
        self.install_bm25()
        self.install_vector()
        embedder = RecordingEmbedder(failure=RuntimeError("private provider detail"))
        error = await self.assert_query_error("hybrid_query_embedding_failed", embedder)
        self.assertEqual(len(embedder.calls), 1)
        self.assertNotIn("private provider detail", repr(error))
        self.assertNotIn(QUERY, repr(error))

    async def test_invalid_provider_output_is_embedding_failure(self):
        self.install_bm25()
        self.install_vector()
        embedder = RecordingEmbedder(output=((0.0, 0.0),))
        await self.assert_query_error("hybrid_query_embedding_failed", embedder)

    async def test_vector_configuration_requires_callable_and_no_unused_callable(self):
        with mock.patch.object(
            memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
            "load_active_snapshot",
        ) as load:
            with self.assertRaises(query.MemoryRetrievalHybridQueryError) as raised:
                await query.fuse_current_hybrid_query_v1(
                    self.reader,
                    None,
                    query_text=QUERY,
                    reference_time=REFERENCE_TIME,
                    vector_sidecar_path=self.vector_path,
                )
            self.assertEqual(raised.exception.category, "hybrid_query_configuration_invalid")
            load.assert_not_called()

            with self.assertRaises(query.MemoryRetrievalHybridQueryError) as raised:
                await query.fuse_current_hybrid_query_v1(
                    self.reader,
                    RecordingEmbedder(),
                    query_text=QUERY,
                    reference_time=REFERENCE_TIME,
                )
            self.assertEqual(raised.exception.category, "hybrid_query_configuration_invalid")
            load.assert_not_called()

    async def test_vector_plan_is_loaded_once_and_never_reopened_for_search(self):
        self.install_bm25()
        self.install_vector()
        embedder = RecordingEmbedder()
        with mock.patch.object(
            vector_store,
            "load_vector_store_snapshot",
            wraps=vector_store.load_vector_store_snapshot,
        ) as load_vector, mock.patch.object(
            vector_store,
            "search_vector_store",
            side_effect=AssertionError("must not reopen vector store"),
        ) as search_store:
            await self.call(embedder)
        self.assertEqual(load_vector.call_count, 1)
        search_store.assert_not_called()

    def test_contract_has_no_query_vector_injection_and_remains_unwired(self):
        signature = inspect.signature(query.fuse_current_hybrid_query_v1)
        self.assertNotIn("query_vector", signature.parameters)

        root = Path(__file__).resolve().parents[2]
        context_source = (
            root / "backend" / "memory_context_integration.py"
        ).read_text(encoding="utf-8")
        relay_source = (
            root / "backend" / "p3_relay_app.py"
        ).read_text(encoding="utf-8")
        render_source = (root / "render.yaml").read_text(encoding="utf-8")
        for text in (context_source, relay_source, render_source):
            self.assertNotIn("memory_retrieval_hybrid_query", text)
            self.assertNotIn("MEMORY_HYBRID_QUERY", text)


if __name__ == "__main__":
    unittest.main()
