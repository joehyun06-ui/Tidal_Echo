from __future__ import annotations

import json
import unittest
from dataclasses import replace

from backend import (
    memory_retrieval_hybrid_fusion as fusion,
    memory_retrieval_hybrid_observability as observability,
    memory_retrieval_hybrid_query as query,
    memory_retrieval_hybrid_relevance as relevance,
    memory_retrieval_hybrid_shadow as shadow,
)
from backend.tests.test_memory_retrieval_hybrid_relevance import (
    CONTENT, EXACT_QUERY, K1, K2, SECRET, UNRELATED_QUERY,
    atomic, semantic_result, sparse_result,
)


NOW = "2026-09-11T00:00:00+00:00"


def query_result(items, text, *, semantic=None, max_hits=10, touches=()):
    fused, summary = relevance.fuse_hybrid_retrieval_with_relevance_v1(
        items, query_text=text, bm25_result=sparse_result(items, text),
        vector_result=semantic, reference_time=NOW,
        touch_hints=touches, max_hits=max_hits,
    )
    return query.HybridQueryResultV1(
        contract_version=query.HYBRID_QUERY_CONTRACT_VERSION,
        source_atomic_count=len(items), bm25_generation=1,
        vector_generation=1 if semantic is not None else None,
        query_embedding_performed=semantic is not None and semantic.indexed_document_count > 0,
        fusion_result=fused, relevance_summary=summary,
    )


def empty_report():
    return shadow.compare_hybrid_retrieval_shadow_v1(
        (), query_result((atomic(),), UNRELATED_QUERY,
                         semantic=semantic_result((K1, 1.0))),
    )


def status(tracker):
    return observability.project_status_payload_v1(
        tracker.snapshot(), enabled=True, installed=True,
        in_flight=False, observability_available=True,
    )


class RelevanceFusionIntegrationTests(unittest.TestCase):
    def test_empty_retrieval_preserves_raw_counts_and_available_channels(self):
        result = query_result((atomic(),), UNRELATED_QUERY,
                              semantic=semantic_result((K1, 1.0)))
        self.assertEqual(result.fusion_result.hits, ())
        self.assertEqual(result.fusion_result.bm25_hit_count, 1)
        self.assertEqual(result.fusion_result.vector_hit_count, 1)
        self.assertTrue(result.fusion_result.bm25_available)
        self.assertTrue(result.fusion_result.vector_available)
        summary = result.relevance_summary
        self.assertEqual((summary.raw_candidate_count, summary.admitted_count,
                          summary.rejected_count, summary.selected_count), (1, 0, 1, 0))
        self.assertEqual(summary.empty_reason, "no_relevance_evidence")
        self.assertEqual(summary.qualified_vector_hit_count, 0)

    def test_admission_runs_before_top_k_rrf_and_metadata_boosts(self):
        items = (atomic(content="Render deployment", confidence=0.01),
                 atomic(K2, "关闭服务", confidence=1.0))
        text = "Render 相关"
        sparse = sparse_result(items, text)
        sparse = replace(sparse, hits=tuple(sorted(
            (replace(hit, score=1_000_000.0) if hit.memory_key == K2 else hit
             for hit in sparse.hits),
            key=lambda hit: (-hit.score, -hit.matched_term_count, hit.memory_key),
        )))
        fused, summary = relevance.fuse_hybrid_retrieval_with_relevance_v1(
            items, query_text=text, bm25_result=sparse,
            vector_result=semantic_result((K2, 1.0), (K1, 0.1), documents=2),
            reference_time=NOW, touch_hints=(fusion.TouchHintV1(K2, 100),), max_hits=1,
        )
        self.assertEqual(tuple(hit.memory_key for hit in fused.hits), (K1,))
        self.assertEqual(fused.hits[0].bm25_rank, 1)
        self.assertIsNone(fused.hits[0].vector_rank)
        self.assertEqual(fused.hits[0].channel_count, 2)
        self.assertEqual(fused.hits[0].touch_boost, 0.0)
        self.assertEqual(fused.bm25_hit_count, 2)
        self.assertEqual(fused.vector_hit_count, 2)
        self.assertEqual(summary.qualified_bm25_hit_count, 1)
        self.assertEqual(summary.rejected_count, 1)

    def test_exact_priority_survives_relevance_ranking(self):
        items = (atomic(content="CODEX_GENERATION_ENABLED", confidence=0.01,
                        last_confirmed_at="2020-01-01T00:00:00+00:00"),
                 atomic(K2, "alpha alpha alpha release", confidence=1.0))
        result = query_result(
            items, "CODEX_GENERATION_ENABLED alpha",
            semantic=semantic_result((K2, 1.0), documents=2),
            touches=(fusion.TouchHintV1(K2, 100),),
        )
        self.assertEqual(result.fusion_result.hits[0].memory_key, K1)
        self.assertIsNotNone(result.fusion_result.hits[0].exact_rank)
        self.assertTrue(all(hit.vector_rank is None for hit in result.fusion_result.hits))

    def test_summary_counts_admission_before_shadow_limit(self):
        items = tuple(atomic(f"relevance_many_{n:06d}", f"Render rollout {n}")
                      for n in range(14))
        result = query_result(items, "Render", max_hits=shadow.MAX_SELECTED)
        summary = result.relevance_summary
        self.assertEqual(summary.admitted_count, 14)
        self.assertEqual(summary.selected_count, 10)
        self.assertEqual(summary.truncated_count, 4)
        report = shadow.compare_hybrid_retrieval_shadow_v1((), result)
        self.assertEqual(report.status, "completed")
        self.assertEqual(report.hybrid_selected_count, 10)
        self.assertEqual(report.relevance_summary, summary)

    def test_summary_validation_rejects_fabricated_counts_or_policy(self):
        original = query_result((atomic(),), UNRELATED_QUERY,
                                semantic=semantic_result((K1, 1.0))).relevance_summary
        for changes in (
            {"policy_version": "private arbitrary policy"},
            {"raw_candidate_count": 257},
            {"admitted_count": True},
            {"rejected_count": 0},
            {"qualified_vector_hit_count": 1},
            {"empty_reason": "none"},
            {"selected_count": 1},
            {"truncated_count": 1},
        ):
            with self.subTest(fields=tuple(changes)), self.assertRaisesRegex(
                relevance.MemoryRetrievalHybridRelevanceError, "^invalid_relevance_summary$",
            ):
                replace(original, **changes)


class RelevanceShadowObservabilityTests(unittest.TestCase):
    def test_both_empty_completes_and_exposes_separate_admission_counts(self):
        report = empty_report()
        self.assertEqual(report.status, "completed")
        self.assertEqual(report.relation, "both_empty")
        tracker = observability.HybridShadowObservabilityV1()
        tracker.record_attempt()
        tracker.record_started()
        tracker.record_report(report)
        payload = status(tracker)
        self.assertEqual(payload["outcomes"]["completed"], 1)
        self.assertEqual(payload["outcomes"]["failed"], 0)
        self.assertEqual(payload["channels"], {
            "bm25_available": 1, "vector_available": 1, "query_embedding_performed": 1,
        })
        self.assertEqual(payload["relevance"], {
            "evaluated": 1, "empty": 1, "admitted_total": 0, "rejected_total": 1,
        })
        self.assertEqual(payload["last"]["bm25_hits"], 1)
        self.assertEqual(payload["last"]["vector_hits"], 1)
        self.assertEqual(payload["last"]["hybrid_selected"], 0)
        self.assertEqual(payload["last"]["relevance"]["empty_reason"], "no_relevance_evidence")

    def test_reports_and_status_never_contain_query_keys_or_source_text(self):
        report = empty_report()
        tracker = observability.HybridShadowObservabilityV1()
        tracker.record_report(report)
        line = shadow.render_hybrid_retrieval_shadow_telemetry_v1(report)
        self.assertIn("bm25=1 vector=1", line)
        self.assertIn("admitted=0 rejected=1", line)
        self.assertIn("qualified_bm25=0 qualified_vector=0", line)
        output = line + repr(report) + repr(tracker.snapshot()) + json.dumps(status(tracker))
        for private in (K1, CONTENT, UNRELATED_QUERY, SECRET, "c:关"):
            self.assertNotIn(private, output)

    def test_malformed_or_inconsistent_summary_fails_instead_of_reporting_empty(self):
        result = query_result((atomic(),), UNRELATED_QUERY,
                              semantic=semantic_result((K1, 1.0)))
        for bad in (
            replace(result, relevance_summary={"private": "payload"}),
            replace(result, fusion_result=replace(
                result.fusion_result, bm25_hit_count=0, vector_hit_count=0,
            )),
            replace(result, fusion_result=replace(result.fusion_result, eligible_atomic_count=0)),
        ):
            with self.subTest(result=repr(bad)):
                report = shadow.compare_hybrid_retrieval_shadow_v1((), bad)
                self.assertEqual(report.status, "failed")
                self.assertIsNone(report.relevance_summary)

    def test_summary_cannot_claim_disabled_vector_ranking_while_using_it(self):
        result = query_result((atomic(),), EXACT_QUERY, semantic=semantic_result((K1, 1.0)))
        bad = replace(result, fusion_result=replace(
            result.fusion_result,
            hits=(replace(result.fusion_result.hits[0], vector_rank=1),),
        ))
        self.assertEqual(shadow.compare_hybrid_retrieval_shadow_v1((K1,), bad).status, "failed")

    def test_legacy_completion_and_terminal_outcomes_clear_last_relevance(self):
        legacy = shadow.HybridRetrievalShadowReportV1(
            contract_version=shadow.HYBRID_SHADOW_CONTRACT_VERSION,
            status="completed", relation="both_empty",
        )
        for outcome in ("failed", "cancelled", "skipped", "legacy"):
            with self.subTest(outcome=outcome):
                tracker = observability.HybridShadowObservabilityV1()
                tracker.record_report(empty_report())
                if outcome == "failed":
                    tracker.record_report(shadow.HybridRetrievalShadowReportV1.failed())
                elif outcome == "cancelled":
                    tracker.record_cancelled()
                elif outcome == "skipped":
                    tracker.record_skipped("busy")
                else:
                    tracker.record_report(legacy)
                payload = status(tracker)
                self.assertIsNone(payload["last"]["relevance"])
                self.assertEqual(payload["relevance"]["evaluated"], 1)
                self.assertEqual(payload["relevance"]["rejected_total"], 1)

    def test_relevance_counters_saturate_and_new_tracker_resets_them(self):
        tracker = observability.HybridShadowObservabilityV1()
        for key in ("relevance_evaluated", "relevance_empty", "relevance_admitted",
                    "relevance_rejected"):
            tracker._counts[key] = observability.MAX_COUNTER
        tracker.record_report(empty_report())
        for value in status(tracker)["relevance"].values():
            self.assertEqual(value, observability.MAX_COUNTER)
        fresh = status(observability.HybridShadowObservabilityV1())
        self.assertEqual(fresh["relevance"], {
            "evaluated": 0, "empty": 0, "admitted_total": 0, "rejected_total": 0,
        })
        self.assertIsNone(fresh["last"]["relevance"])


if __name__ == "__main__":
    unittest.main()
