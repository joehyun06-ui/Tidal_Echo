from __future__ import annotations

import unittest

from backend import (
    memory_hierarchy_projection as hierarchy,
    memory_retrieval_bm25 as bm25,
    memory_retrieval_hybrid_fusion as hybrid,
    memory_retrieval_v2 as lexical_v2,
    memory_retrieval_vector as vector,
)


NOW = "2026-09-01T12:00:00+00:00"


def atomic(
    key: str,
    content: str,
    *,
    confidence: float = 1.0,
    explicitness: str = "explicit",
    sensitivity: str = "normal",
    scope_type: str = "global_user",
    scope_ref: str = "",
    last_confirmed_at: str = "2026-08-31T12:00:00+00:00",
) -> hierarchy.AtomicMemoryProjectionInputV1:
    return hierarchy.AtomicMemoryProjectionInputV1(
        memory_key=key,
        kind="project",
        scope_type=scope_type,
        scope_ref=scope_ref,
        normalized_content=content,
        fingerprint_version=1,
        status="active",
        explicitness=explicitness,
        confidence=confidence,
        sensitivity=sensitivity,
        first_observed_at="2026-08-01T12:00:00+00:00",
        last_confirmed_at=last_confirmed_at,
        updated_at="2026-08-31T12:00:00+00:00",
    )


def bm25_result(*hits: tuple[str, float], query_terms: int = 3, documents: int = 2):
    return bm25.BM25SearchResultV1(
        hits=tuple(
            bm25.BM25SearchHitV1(
                memory_key=key,
                score=float(score),
                matched_term_count=1,
            )
            for key, score in hits
        ),
        query_term_count=query_terms,
        indexed_document_count=documents,
    )


def vector_result(*hits: tuple[str, float], documents: int = 2):
    return vector.VectorSearchResultV1(
        hits=tuple(
            vector.VectorSearchHitV1(memory_key=key, similarity=float(score))
            for key, score in hits
        ),
        indexed_document_count=documents,
    )


def v2_item(item: hierarchy.AtomicMemoryProjectionInputV1) -> dict:
    return {
        "memory_key": item.memory_key,
        "kind": item.kind,
        "scope_type": item.scope_type,
        "scope_ref": item.scope_ref,
        "normalized_content": item.normalized_content,
        "fingerprint_version": item.fingerprint_version,
        "status": item.status,
        "explicitness": item.explicitness,
        "confidence": item.confidence,
        "sensitivity": item.sensitivity,
        "first_observed_at": item.first_observed_at,
        "last_confirmed_at": item.last_confirmed_at,
        "created_at": item.first_observed_at,
        "updated_at": item.updated_at,
        "provenance": [],
    }


class HybridFusionChannelTests(unittest.TestCase):
    def test_exact_identifier_channel_is_a_separate_priority_tier(self):
        exact = atomic(
            "A" * 16,
            "Production gate CODEX_GENERATION_ENABLED remains false; "
            "deploy dep-daak91hf2nfc73ak97p0 is the observed release.",
            confidence=0.50,
            last_confirmed_at="2024-01-01T00:00:00+00:00",
        )
        semantic = atomic(
            "B" * 16,
            "Generation rollout provider settings and deployment behavior.",
            confidence=1.0,
            last_confirmed_at="2026-09-01T11:59:00+00:00",
        )
        result = hybrid.fuse_hybrid_retrieval_v1(
            [exact, semantic],
            query_text=(
                "Check CODEX_GENERATION_ENABLED for "
                "dep-daak91hf2nfc73ak97p0"
            ),
            bm25_result=bm25_result((semantic.memory_key, 10.0), documents=2),
            vector_result=vector_result((semantic.memory_key, 0.999), documents=2),
            reference_time=NOW,
            touch_hints=(hybrid.TouchHintV1(semantic.memory_key, 100),),
        )
        self.assertEqual(result.hits[0].memory_key, exact.memory_key)
        self.assertIsNotNone(result.hits[0].exact_rank)
        self.assertEqual(result.hits[0].exact_match_count, 2)
        self.assertIsNone(result.hits[1].exact_rank)
        self.assertLessEqual(result.hits[1].touch_boost, hybrid.MAX_TOUCH_BOOST)

    def test_current_v2_lexical_order_is_preserved(self):
        items = [
            atomic("A" * 16, "alpha elsewhere"),
            atomic("B" * 16, "prefix alpha beta gamma suffix"),
            atomic("C" * 16, "alpha beta elsewhere"),
            atomic("D" * 16, "alpha beta gamma"),
        ]
        lexical_keys = hybrid._lexical_channel(tuple(items), "alpha beta gamma")
        current = lexical_v2.plan_memory_recall_v2(
            [v2_item(item) for item in items],
            query_text="alpha beta gamma",
            scope_type="global_user",
            max_items=4,
            character_budget=2000,
        )
        current_keys = tuple(
            item.candidate["memory_key"] for item in current.items
        )
        self.assertEqual(lexical_keys, current_keys)
        self.assertEqual(
            lexical_keys,
            ("D" * 16, "B" * 16, "C" * 16, "A" * 16),
        )

    def test_multichannel_rank_fusion_beats_single_vector_hit(self):
        multi = atomic("A" * 16, "alpha beta memory", confidence=0.80)
        vector_only = atomic("B" * 16, "unrelated semantic memory", confidence=1.0)
        result = hybrid.fuse_hybrid_retrieval_v1(
            [multi, vector_only],
            query_text="alpha beta query",
            bm25_result=bm25_result((multi.memory_key, 5.0), documents=2),
            vector_result=vector_result(
                (vector_only.memory_key, 0.99),
                (multi.memory_key, 0.70),
                documents=2,
            ),
            reference_time=NOW,
        )
        self.assertEqual(result.hits[0].memory_key, multi.memory_key)
        self.assertEqual(result.hits[0].channel_count, 3)
        self.assertGreater(
            result.hits[0].rank_fusion_score,
            result.hits[1].rank_fusion_score,
        )

    def test_missing_sidecar_channels_degrade_to_exact_and_lexical(self):
        item = atomic("A" * 16, "alpha beta project memory")
        result = hybrid.fuse_hybrid_retrieval_v1(
            [item],
            query_text="alpha beta",
            bm25_result=None,
            vector_result=None,
            reference_time=NOW,
        )
        self.assertEqual(tuple(hit.memory_key for hit in result.hits), (item.memory_key,))
        self.assertFalse(result.bm25_available)
        self.assertFalse(result.vector_available)
        self.assertEqual(result.bm25_hit_count, 0)
        self.assertEqual(result.vector_hit_count, 0)
        self.assertIsNotNone(result.hits[0].lexical_rank)


class HybridFusionMetadataTests(unittest.TestCase):
    def test_metadata_boosts_are_individually_capped(self):
        item = atomic(
            "A" * 16,
            "alpha project memory",
            confidence=1.0,
            last_confirmed_at=NOW,
        )
        result = hybrid.fuse_hybrid_retrieval_v1(
            [item],
            query_text="alpha",
            bm25_result=None,
            vector_result=None,
            reference_time=NOW,
            touch_hints=(hybrid.TouchHintV1(item.memory_key, 10**9),),
        )
        hit = result.hits[0]
        self.assertLessEqual(hit.confidence_boost, hybrid.MAX_CONFIDENCE_BOOST)
        self.assertLessEqual(hit.recency_boost, hybrid.MAX_RECENCY_BOOST)
        self.assertLessEqual(hit.touch_boost, hybrid.MAX_TOUCH_BOOST)
        self.assertAlmostEqual(hit.touch_boost, hybrid.MAX_TOUCH_BOOST, places=12)

    def test_invalid_or_future_recency_never_gets_a_positive_boost(self):
        invalid = atomic(
            "A" * 16,
            "alpha invalid time",
            last_confirmed_at="not-a-time",
        )
        future = atomic(
            "B" * 16,
            "alpha future time",
            last_confirmed_at="2027-01-01T00:00:00+00:00",
        )
        result = hybrid.fuse_hybrid_retrieval_v1(
            [invalid, future],
            query_text="alpha",
            bm25_result=None,
            vector_result=None,
            reference_time=NOW,
        )
        by_key = {hit.memory_key: hit for hit in result.hits}
        self.assertEqual(by_key[invalid.memory_key].recency_boost, 0.0)
        self.assertEqual(by_key[future.memory_key].recency_boost, 0.0)


class HybridFusionBoundaryTests(unittest.TestCase):
    def test_sensitive_and_non_global_atomics_are_not_eligible(self):
        normal = atomic("A" * 16, "alpha normal")
        sensitive = atomic(
            "B" * 16,
            "alpha private",
            sensitivity="sensitive",
        )
        project = atomic(
            "C" * 16,
            "alpha scoped",
            scope_type="project",
            scope_ref="project-1",
        )
        result = hybrid.fuse_hybrid_retrieval_v1(
            [normal, sensitive, project],
            query_text="alpha",
            bm25_result=None,
            vector_result=None,
            reference_time=NOW,
        )
        self.assertEqual(result.eligible_atomic_count, 1)
        self.assertEqual(tuple(hit.memory_key for hit in result.hits), (normal.memory_key,))

    def test_channel_hit_for_ineligible_or_unknown_atomic_fails_closed(self):
        normal = atomic("A" * 16, "alpha normal")
        sensitive = atomic(
            "B" * 16,
            "alpha private",
            sensitivity="sensitive",
        )
        with self.assertRaisesRegex(
            hybrid.MemoryRetrievalHybridFusionError,
            r"^invalid_vector_result$",
        ):
            hybrid.fuse_hybrid_retrieval_v1(
                [normal, sensitive],
                query_text="alpha",
                bm25_result=None,
                vector_result=vector_result(
                    (sensitive.memory_key, 0.9),
                    documents=1,
                ),
                reference_time=NOW,
            )

    def test_vector_result_must_be_unique_positive_and_canonically_ranked(self):
        a = atomic("A" * 16, "alpha")
        b = atomic("B" * 16, "beta")
        bad_results = (
            vector_result(
                (a.memory_key, 0.5),
                (b.memory_key, 0.9),
                documents=2,
            ),
            vector_result(
                (a.memory_key, 0.9),
                (a.memory_key, 0.8),
                documents=2,
            ),
            vector_result((a.memory_key, 0.0), documents=2),
        )
        for raw in bad_results:
            with self.subTest(raw=repr(raw)), self.assertRaisesRegex(
                hybrid.MemoryRetrievalHybridFusionError,
                r"^invalid_vector_result$",
            ):
                hybrid.fuse_hybrid_retrieval_v1(
                    [a, b],
                    query_text="alpha",
                    bm25_result=None,
                    vector_result=raw,
                    reference_time=NOW,
                )

    def test_repr_is_data_free(self):
        secret_content = "CODEX_GENERATION_ENABLED secret-project-literal"
        item = atomic("SECRETKEY12345678", secret_content)
        result = hybrid.fuse_hybrid_retrieval_v1(
            [item],
            query_text="CODEX_GENERATION_ENABLED",
            bm25_result=None,
            vector_result=None,
            reference_time=NOW,
        )
        rendered = repr(result) + repr(result.hits[0])
        self.assertNotIn(item.memory_key, rendered)
        self.assertNotIn(secret_content, rendered)
        self.assertNotIn("CODEX_GENERATION_ENABLED", rendered)

    def test_reference_time_must_be_explicit_and_timezone_aware(self):
        item = atomic("A" * 16, "alpha")
        for raw in (None, "", "2026-09-01T12:00:00", "not-a-time"):
            with self.subTest(raw=raw), self.assertRaisesRegex(
                hybrid.MemoryRetrievalHybridFusionError,
                r"^invalid_reference_time$",
            ):
                hybrid.fuse_hybrid_retrieval_v1(
                    [item],
                    query_text="alpha",
                    bm25_result=None,
                    vector_result=None,
                    reference_time=raw,
                )


if __name__ == "__main__":
    unittest.main()
