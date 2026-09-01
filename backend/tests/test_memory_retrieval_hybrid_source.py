from __future__ import annotations

import dataclasses
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
    memory_retrieval_hybrid_source as source,
    memory_retrieval_vector as vector,
    memory_retrieval_vector_store as vector_store,
)


SECRET = "Hybrid-D2-BM25-Secret-0123456789-AbCd!"
OTHER_SECRET = "Hybrid-D2-Other-Secret-9876543210-ZyXw!"
KEY_ID = "hybrid-d2-test-key"
MODEL = "test-embedding-v1"
DIMS = 2
REFERENCE_TIME = "2026-09-01T12:00:00+00:00"

K1 = "hybrid_source_atomic_000001"
K2 = "hybrid_source_atomic_000002"
K3 = "hybrid_source_atomic_000003"


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
        confidence=0.9,
        sensitivity=sensitivity,
        first_observed_at="2026-08-01T00:00:00+00:00",
        last_confirmed_at="2026-08-30T00:00:00+00:00",
        updated_at="2026-08-30T00:00:00+00:00",
    )


def atomics():
    return (
        atomic(
            K1,
            "Render deployment CODEX_GENERATION_ENABLED dep-daak91hf2nfc73ak97p0",
        ),
        atomic(K2, "Android frontend release planning"),
        atomic(K3, "private internal project", sensitivity="sensitive"),
    )


def current_digest(items=None):
    values = atomics() if items is None else items
    return memory_hierarchy_baseline.build_baseline_hierarchy_plan_v1(
        values
    ).atomic_snapshot_digest


def vector_plan(items=None, *, digest=None, forged_revision=False):
    values = atomics() if items is None else items
    selected = tuple(
        item
        for item in values
        if item.scope_type == "global_user" and item.sensitivity == "normal"
    )
    vectors = {
        K1: (1.0, 0.0),
        K2: (0.0, 1.0),
    }
    documents = []
    for item in selected:
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
        source_snapshot_digest=digest or current_digest(values),
        embedding_model=MODEL,
        dimensions=DIMS,
        documents=tuple(sorted(documents, key=lambda item: item.memory_key)),
    ))


class HybridSourceCompositionTests(unittest.TestCase):
    def setUp(self):
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

    def assert_source_error(self, category, callable_, *args, **kwargs):
        with self.assertRaises(source.MemoryRetrievalHybridSourceError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.category, category)

    def install_bm25(self, *, digest=None, secret=SECRET):
        plan = bm25.build_bm25_index_v1(
            atomics(),
            source_snapshot_digest=digest or current_digest(),
            term_key_id=KEY_ID,
            term_hmac_secret=secret,
        )
        bm25_store.initialize_bm25_store(self.bm25_path)
        return bm25_store.apply_bm25_index_plan(self.bm25_path, plan)

    def install_vector(self, *, digest=None, forged_revision=False):
        vector_store.initialize_vector_store(self.vector_path)
        return vector_store.apply_vector_index_plan(
            self.vector_path,
            vector_plan(digest=digest, forged_revision=forged_revision),
        )

    def call(self, **overrides):
        kwargs = dict(
            query_text="CODEX_GENERATION_ENABLED dep-daak91hf2nfc73ak97p0",
            reference_time=REFERENCE_TIME,
            bm25_sidecar_path=self.bm25_path,
            term_key_id=KEY_ID,
            term_hmac_secret=SECRET,
            vector_sidecar_path=self.vector_path,
            query_vector=vector.QueryVectorV1(
                embedding_model=MODEL,
                dimensions=DIMS,
                vector=(1.0, 0.0),
            ),
        )
        kwargs.update(overrides)
        with mock.patch.object(
            memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
            "load_active_snapshot",
            return_value=self.snapshot,
        ):
            return source.fuse_current_hybrid_retrieval_v1(
                self.reader,
                **kwargs,
            )

    def test_current_bm25_and_vector_are_bound_then_fused(self):
        bm = self.install_bm25()
        vec = self.install_vector()
        result = self.call()
        self.assertEqual(result.contract_version, source.HYBRID_SOURCE_CONTRACT_VERSION)
        self.assertEqual(result.source_atomic_count, 3)
        self.assertEqual(result.bm25_generation, bm.generation)
        self.assertEqual(result.vector_generation, vec.generation)
        self.assertEqual(result.fusion_result.hits[0].memory_key, K1)
        self.assertTrue(result.fusion_result.bm25_available)
        self.assertTrue(result.fusion_result.vector_available)
        rendered = repr(result)
        self.assertNotIn(K1, rendered)
        self.assertNotIn("CODEX_GENERATION_ENABLED", rendered)

    def test_explicitly_missing_sidecars_degrade_to_exact_and_lexical(self):
        with mock.patch.object(
            memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
            "load_active_snapshot",
            return_value=self.snapshot,
        ):
            result = source.fuse_current_hybrid_retrieval_v1(
                self.reader,
                query_text="CODEX_GENERATION_ENABLED",
                reference_time=REFERENCE_TIME,
            )
        self.assertIsNone(result.bm25_generation)
        self.assertIsNone(result.vector_generation)
        self.assertFalse(result.fusion_result.bm25_available)
        self.assertFalse(result.fusion_result.vector_available)
        self.assertEqual(result.fusion_result.hits[0].memory_key, K1)

    def test_stale_bm25_sidecar_fails_instead_of_silent_use(self):
        self.install_bm25(digest="b" * 64)
        self.install_vector()
        self.assert_source_error("hybrid_source_stale", self.call)

    def test_stale_vector_sidecar_fails_instead_of_silent_use(self):
        self.install_bm25()
        self.install_vector(digest="c" * 64)
        self.assert_source_error("hybrid_source_stale", self.call)

    def test_vector_document_revision_is_reproved_not_just_snapshot_digest(self):
        self.install_bm25()
        self.install_vector(forged_revision=True)
        self.assert_source_error("hybrid_source_vector_invalid", self.call)

    def test_same_key_id_wrong_bm25_secret_fails_exact_plan_reproof(self):
        self.install_bm25(secret=SECRET)
        self.install_vector()
        self.assert_source_error(
            "hybrid_source_bm25_invalid",
            self.call,
            term_hmac_secret=OTHER_SECRET,
        )

    def test_path_alias_and_partial_channel_configuration_fail_before_io(self):
        self.assert_source_error(
            "hybrid_source_configuration_invalid",
            source.fuse_current_hybrid_retrieval_v1,
            self.reader,
            query_text="render",
            reference_time=REFERENCE_TIME,
            bm25_sidecar_path=self.authority,
            term_key_id=KEY_ID,
            term_hmac_secret=SECRET,
        )
        self.assert_source_error(
            "hybrid_source_configuration_invalid",
            source.fuse_current_hybrid_retrieval_v1,
            self.reader,
            query_text="render",
            reference_time=REFERENCE_TIME,
            vector_sidecar_path=self.vector_path,
            query_vector=None,
        )

    def test_authority_failure_is_fatal_and_data_free(self):
        with mock.patch.object(
            memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
            "load_active_snapshot",
            side_effect=memory_hierarchy_snapshot.MemoryHierarchySnapshotError(
                "storage_unavailable"
            ),
        ):
            with self.assertRaises(source.MemoryRetrievalHybridSourceError) as raised:
                source.fuse_current_hybrid_retrieval_v1(
                    self.reader,
                    query_text="render",
                    reference_time=REFERENCE_TIME,
                )
        self.assertEqual(
            raised.exception.category,
            "hybrid_source_authority_unavailable",
        )
        self.assertNotIn(str(self.authority), repr(raised.exception))

    def test_d2_remains_unwired_to_context_runtime_and_render(self):
        root = Path(__file__).resolve().parents[2]
        context_source = (
            root / "backend" / "memory_context_integration.py"
        ).read_text(encoding="utf-8")
        relay_source = (
            root / "backend" / "p3_relay_app.py"
        ).read_text(encoding="utf-8")
        render_source = (root / "render.yaml").read_text(encoding="utf-8")
        for text in (context_source, relay_source, render_source):
            self.assertNotIn("memory_retrieval_hybrid_source", text)
            self.assertNotIn("MEMORY_HYBRID_RETRIEVAL", text)


if __name__ == "__main__":
    unittest.main()
