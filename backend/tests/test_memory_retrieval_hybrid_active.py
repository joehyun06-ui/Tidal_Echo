from __future__ import annotations

import asyncio
from dataclasses import replace
import json
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
    memory_retrieval_hybrid_active as active,
    memory_retrieval_hybrid_fusion as fusion,
    memory_retrieval_hybrid_query as hybrid_query,
    memory_retrieval_hybrid_runtime_composition as runtime_composition,
    memory_retrieval_vector as vector,
    memory_retrieval_vector_store as vector_store,
)


TERM_SECRET = "Hybrid-D3C1-BM25-Secret-0123456789-AbCd!"
TERM_KEY_ID = "hybrid-d3c1-test-key"
EMBEDDING_MODEL = "hybrid-d3c1-embedding-v1"
DIMS = 2
REFERENCE_TIME = "2026-09-03T12:00:00+00:00"
QUERY = "Check CODEX_GENERATION_ENABLED for dep-daak91hf2nfc73ak97p0"
K1 = "hybrid_active_atomic_000001"
K2 = "hybrid_active_atomic_000002"
K3 = "hybrid_active_atomic_000003"


def atomic(
    key: str,
    content: str,
    *,
    sensitivity: str = "normal",
    kind: str = "project",
):
    return hierarchy.AtomicMemoryProjectionInputV1(
        memory_key=key,
        kind=kind,
        scope_type="global_user",
        scope_ref="",
        normalized_content=content,
        fingerprint_version=1,
        status="active",
        explicitness="explicit",
        confidence=0.95,
        sensitivity=sensitivity,
        first_observed_at="2026-08-01T00:00:00+00:00",
        last_confirmed_at="2026-09-02T00:00:00+00:00",
        updated_at="2026-09-02T00:00:00+00:00",
    )


def atomics():
    return (
        atomic(
            K1,
            "Production gate CODEX_GENERATION_ENABLED remains false; "
            "deploy dep-daak91hf2nfc73ak97p0 is the observed release.",
        ),
        atomic(K2, "Android frontend release planning and manual deployment."),
        atomic(K3, "private internal project", sensitivity="sensitive"),
    )


def snapshot_digest(items):
    return memory_hierarchy_baseline.build_baseline_hierarchy_plan_v1(
        items
    ).atomic_snapshot_digest


class HybridActiveAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.authority = self.root / "relay.db"
        self.bm25_path = self.root / "hybrid-bm25.db"
        self.vector_path = self.root / "hybrid-vector.db"

        reader = object.__new__(memory_hierarchy_snapshot.MemoryHierarchySnapshotReader)
        object.__setattr__(reader, "_database_path", str(self.authority))
        self.reader = reader
        self.snapshot = memory_hierarchy_snapshot.HierarchyAtomicSnapshotV1(
            atomics=atomics()
        )
        self.digest = snapshot_digest(atomics())

        sparse_plan = bm25.build_bm25_index_v1(
            atomics(),
            source_snapshot_digest=self.digest,
            term_key_id=TERM_KEY_ID,
            term_hmac_secret=TERM_SECRET,
        )
        bm25_store.initialize_bm25_store(self.bm25_path)
        self.sparse = bm25_store.apply_bm25_index_plan(
            self.bm25_path,
            sparse_plan,
        )

        vectors = {K1: (1.0, 0.0), K2: (0.0, 1.0)}
        documents = tuple(
            sorted(
                (
                    vector.VectorDocumentPlanV1(
                        memory_key=item.memory_key,
                        atomic_revision_digest=hierarchy._atomic_revision_digest(item),
                        vector=vectors[item.memory_key],
                    )
                    for item in atomics()
                    if item.sensitivity == "normal"
                ),
                key=lambda item: item.memory_key,
            )
        )
        vector_plan = vector.validate_vector_index_plan_v1(
            vector.VectorIndexPlanV1(
                contract_version=vector.VECTOR_CONTRACT_VERSION,
                embedding_contract_version=vector.EMBEDDING_CONTRACT_VERSION,
                source_snapshot_digest=self.digest,
                embedding_model=EMBEDDING_MODEL,
                dimensions=DIMS,
                documents=documents,
            )
        )
        vector_store.initialize_vector_store(self.vector_path)
        self.semantic = vector_store.apply_vector_index_plan(
            self.vector_path,
            vector_plan,
        )

        config = runtime_composition.HybridRuntimeConfigV1(
            authority_path=self.authority,
            persistent_root=self.root,
            bm25_path=self.bm25_path,
            vector_path=self.vector_path,
            fingerprint_key_id="memory-fingerprint-key",
            fingerprint_hmac_secret="Memory-Fingerprint-Secret-0123456789-AbCd!",
            max_item_chars=2000,
            sensitive_storage_enabled=False,
            term_key_id=TERM_KEY_ID,
            term_hmac_secret=TERM_SECRET,
            embedding_model=EMBEDDING_MODEL,
            provider_embedding_model="provider-embedding-model",
            embedding_dimensions=DIMS,
            embedding_adapter=object(),
        )
        self.runner = runtime_composition.HybridRetrievalShadowRunnerV1(
            config=config,
            reader=self.reader,
        )
        self.query_result = self._query_result()

    def _query_result(self):
        sparse = bm25.search_bm25_index_v1(
            self.sparse.plan,
            QUERY,
            term_key_id=TERM_KEY_ID,
            term_hmac_secret=TERM_SECRET,
            max_hits=bm25.MAX_HITS,
        )
        semantic = vector.search_vector_index_v1(
            self.semantic.plan,
            vector.QueryVectorV1(
                embedding_model=EMBEDDING_MODEL,
                dimensions=DIMS,
                vector=(1.0, 0.0),
            ),
            max_hits=vector.MAX_VECTOR_HITS,
            minimum_similarity=0.0,
        )
        fused = fusion.fuse_hybrid_retrieval_v1(
            atomics(),
            query_text=QUERY,
            bm25_result=sparse,
            vector_result=semantic,
            reference_time=REFERENCE_TIME,
        )
        return hybrid_query.HybridQueryResultV1(
            contract_version=hybrid_query.HYBRID_QUERY_CONTRACT_VERSION,
            source_atomic_count=self.snapshot.count,
            bm25_generation=self.sparse.generation,
            vector_generation=self.semantic.generation,
            query_embedding_performed=True,
            fusion_result=fused,
        )

    async def plan(self, result=None, *, snapshot=None, **kwargs):
        chosen = self.query_result if result is None else result
        current = self.snapshot if snapshot is None else snapshot
        with mock.patch.object(
            runtime_composition.HybridRetrievalShadowRunnerV1,
            "__call__",
            new=mock.AsyncMock(return_value=chosen),
        ), mock.patch.object(
            memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
            "load_active_snapshot",
            return_value=current,
        ):
            return await active.plan_hybrid_active_selection_v1(
                self.runner,
                query_text=QUERY,
                **kwargs,
            )

    async def test_same_revision_selection_renders_existing_memory_envelope(self):
        selection = await self.plan()
        self.assertEqual(
            selection.contract_version,
            active.HYBRID_ACTIVE_CONTRACT_VERSION,
        )
        self.assertEqual(selection.memory_keys[0], K1)
        self.assertGreaterEqual(selection.selected_count, 1)
        self.assertLessEqual(selection.selected_count, active.ACTIVE_MAX_ITEMS)
        self.assertLessEqual(selection.total_chars, active.ACTIVE_CHARACTER_BUDGET)
        self.assertTrue(selection.query_embedding_performed)
        self.assertEqual(type(selection.atomics), tuple)
        self.assertTrue(
            all(
                type(item) is hierarchy.AtomicMemoryProjectionInputV1
                for item in selection.atomics
            )
        )

        message = active.render_hybrid_active_developer_message_v1(selection)
        self.assertIsNotNone(message)
        self.assertEqual(message["role"], "developer")
        self.assertIn("memory_context_developer_message/v1", message["content"])
        self.assertIn("CODEX_GENERATION_ENABLED", message["content"])

        rendered = repr(selection)
        self.assertNotIn(K1, rendered)
        self.assertNotIn("CODEX_GENERATION_ENABLED", rendered)
        self.assertNotIn(QUERY, rendered)

    async def test_authority_change_after_query_fails_closed_as_stale(self):
        changed = tuple(
            replace(item, normalized_content=item.normalized_content + " changed")
            if item.memory_key == K1
            else item
            for item in atomics()
        )
        snapshot = memory_hierarchy_snapshot.HierarchyAtomicSnapshotV1(
            atomics=changed
        )
        with self.assertRaises(active.MemoryRetrievalHybridActiveError) as raised:
            await self.plan(snapshot=snapshot)
        self.assertEqual(raised.exception.category, "hybrid_active_stale")

    async def test_sidecar_generation_change_after_query_fails_closed(self):
        stale = replace(
            self.query_result,
            bm25_generation=self.query_result.bm25_generation + 1,
        )
        with self.assertRaises(active.MemoryRetrievalHybridActiveError) as raised:
            await self.plan(result=stale)
        self.assertEqual(raised.exception.category, "hybrid_active_stale")

    async def test_ineligible_ranked_key_is_never_rendered(self):
        forged_hit = replace(
            self.query_result.fusion_result.hits[0],
            memory_key=K3,
        )
        forged_fusion = replace(
            self.query_result.fusion_result,
            hits=(forged_hit,),
        )
        forged = replace(self.query_result, fusion_result=forged_fusion)
        with self.assertRaises(active.MemoryRetrievalHybridActiveError) as raised:
            await self.plan(result=forged)
        self.assertEqual(
            raised.exception.category,
            "hybrid_active_selection_invalid",
        )

    async def test_active_path_requires_both_proved_sidecar_channels(self):
        no_vector = replace(
            self.query_result,
            vector_generation=None,
            fusion_result=replace(
                self.query_result.fusion_result,
                vector_available=False,
            ),
        )
        with self.assertRaises(active.MemoryRetrievalHybridActiveError) as raised:
            await self.plan(result=no_vector)
        self.assertEqual(
            raised.exception.category,
            "hybrid_active_channels_unavailable",
        )

    async def test_query_failure_is_bounded_and_data_free(self):
        with mock.patch.object(
            runtime_composition.HybridRetrievalShadowRunnerV1,
            "__call__",
            new=mock.AsyncMock(side_effect=RuntimeError("private provider payload")),
        ):
            with self.assertRaises(active.MemoryRetrievalHybridActiveError) as raised:
                await active.plan_hybrid_active_selection_v1(
                    self.runner,
                    query_text=QUERY,
                )
        self.assertEqual(raised.exception.category, "hybrid_active_query_failed")
        self.assertNotIn("private provider payload", repr(raised.exception))
        self.assertNotIn(QUERY, repr(raised.exception))

    async def test_query_cancellation_propagates(self):
        with mock.patch.object(
            runtime_composition.HybridRetrievalShadowRunnerV1,
            "__call__",
            new=mock.AsyncMock(side_effect=asyncio.CancelledError()),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await active.plan_hybrid_active_selection_v1(
                    self.runner,
                    query_text=QUERY,
                )

    def test_budget_stops_in_rank_order_without_skipping(self):
        items = (
            atomic(K1, "A" * 1200),
            atomic(K2, "B" * 900),
            atomic("hybrid_active_atomic_000004", "C" * 20),
        )
        snapshot = memory_hierarchy_snapshot.HierarchyAtomicSnapshotV1(atomics=items)
        base_hit = self.query_result.fusion_result.hits[0]
        hits = (
            replace(base_hit, memory_key=items[0].memory_key),
            replace(base_hit, memory_key=items[1].memory_key),
            replace(base_hit, memory_key=items[2].memory_key),
        )
        fused = replace(
            self.query_result.fusion_result,
            hits=hits,
        )
        result = replace(
            self.query_result,
            source_atomic_count=snapshot.count,
            fusion_result=fused,
        )
        selection = active._selection_from_result(
            snapshot,
            result,
            max_items=3,
            character_budget=1500,
        )
        self.assertEqual(selection.memory_keys, (K1,))
        self.assertEqual(selection.total_chars, 1200)

    def test_selection_constructor_and_foreign_renderer_inputs_are_rejected(self):
        with self.assertRaises(active.MemoryRetrievalHybridActiveError):
            active.HybridActiveSelectionV1()
        with self.assertRaises(active.MemoryRetrievalHybridActiveError):
            active.render_hybrid_active_developer_message_v1(object())

    def test_d3c1_is_wired_only_through_reviewed_d3c2_runtime_and_default_off_gate(self):
        root = Path(__file__).resolve().parents[2]
        context_source = (
            root / "backend" / "memory_context_integration.py"
        ).read_text(encoding="utf-8")
        relay_source = (
            root / "backend" / "p3_relay_app.py"
        ).read_text(encoding="utf-8")
        render_source = (root / "render.yaml").read_text(encoding="utf-8")

        # The existing synchronous context module still has no Hybrid-active
        # dependency. P3 imports only the reviewed D3C2 runtime wrapper, never
        # the D3C1 selection module directly.
        self.assertNotIn("memory_retrieval_hybrid_active", context_source)
        self.assertNotIn("from backend import memory_retrieval_hybrid_active", relay_source)
        self.assertIn("memory_retrieval_hybrid_runtime_active", relay_source)

        blueprint = json.loads(render_source)
        env = {
            item["key"]: item
            for item in blueprint["services"][0].get("envVars", [])
        }
        self.assertEqual(
            env["MEMORY_HYBRID_RETRIEVAL_ACTIVE_ENABLED"].get("value"),
            "false",
        )
        self.assertEqual(
            env["MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED"].get("value"),
            "false",
        )


if __name__ == "__main__":
    unittest.main()
