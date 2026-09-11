from __future__ import annotations

import json
import unittest
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, asdict, replace
from unittest.mock import patch

from backend import (
    memory_hierarchy_projection as hierarchy,
    memory_retrieval_bm25 as bm25,
    memory_retrieval_hybrid_fusion as fusion,
    memory_retrieval_hybrid_relevance as relevance,
    memory_retrieval_vector as vector,
)


K1 = "relevance_atomic_000001"
K2 = "relevance_atomic_000002"
SECRET = "Relevance-Test-HMAC-0123456789-AbCd!"
KEY_ID = "relevance-test-v1"
CONTENT = "该项目今后保持 Render Auto-Deploy 关闭"
EXACT_QUERY = (
    "不要记忆这条测试问题。该项目对 Render Auto-Deploy 的既有决定是什么？"
    "只根据已有记忆简短回答。"
)
PARAPHRASE_QUERY = (
    "不要记忆这条测试问题。这个项目是否允许代码推送后自动上线？"
    "只根据已有决定简短回答。"
)
UNRELATED_QUERY = (
    "不要记忆这条测试问题。已有记忆中，蓝莓蛋糕的烘焙温度是多少？"
    "没有依据就说明没有相关记忆。"
)


def atomic(key=K1, content=CONTENT, **changes):
    return replace(hierarchy.AtomicMemoryProjectionInputV1(
        memory_key=key,
        kind="decision",
        scope_type="global_user",
        scope_ref="",
        normalized_content=content,
        fingerprint_version=1,
        status="active",
        explicitness="explicit",
        confidence=1.0,
        sensitivity="normal",
        first_observed_at="2026-08-01T00:00:00+00:00",
        last_confirmed_at="2026-08-14T15:50:47.010058+00:00",
        updated_at="2026-08-14T15:50:47.010058+00:00",
    ), **changes)


def sparse_result(items, query):
    # Use the real pure tokenizer/index/search; no sidecar or database is opened.
    plan = bm25.build_bm25_index_v1(
        items,
        source_snapshot_digest="a" * 64,
        term_key_id=KEY_ID,
        term_hmac_secret=SECRET,
    )
    return bm25.search_bm25_index_v1(
        plan, query, term_key_id=KEY_ID, term_hmac_secret=SECRET,
    )


def semantic_result(*hits, documents=1):
    # Synthetic scores exercise admission only, not provider quality/calibration.
    return vector.VectorSearchResultV1(
        hits=tuple(sorted(
            (vector.VectorSearchHitV1(key, score) for key, score in hits),
            key=lambda hit: (-hit.similarity, hit.memory_key),
        )),
        indexed_document_count=documents,
    )


def plan(items, query, semantic=None):
    return relevance.plan_hybrid_relevance_v1(
        items,
        query_text=query,
        bm25_result=sparse_result(items, query),
        vector_result=semantic,
    )


class HybridRelevanceRegressionTests(unittest.TestCase):
    def test_three_smoke_queries_keep_baseline_matches_and_abstain_on_control(self):
        item = atomic()
        for query, expected_count, exact_count in (
            (EXACT_QUERY, 1, 1),
            (PARAPHRASE_QUERY, 1, 0),
            (UNRELATED_QUERY, 0, 0),
        ):
            with self.subTest(query=query):
                result = plan((item,), query, semantic_result((K1, 0.5)))
                self.assertEqual(result.admitted_count, expected_count)
                self.assertEqual(result.raw_exact_hit_count, exact_count)
                self.assertEqual(result.raw_bm25_hit_count, 1)
                self.assertEqual(result.raw_vector_hit_count, 1)
                self.assertEqual(result.qualified_bm25_hit_count, expected_count)
                self.assertEqual(result.qualified_vector_keys, ())
                self.assertFalse(result.semantic_admission_enabled)

    def test_two_available_channels_with_hits_can_produce_a_valid_empty_plan(self):
        result = plan((atomic(),), UNRELATED_QUERY, semantic_result((K1, 1.0)))
        self.assertTrue(result.bm25_available)
        self.assertTrue(result.vector_available)
        self.assertEqual(result.raw_candidate_count, 1)
        self.assertEqual(result.rejected_count, 1)
        self.assertEqual(result.excluded_bm25_hit_count, 1)
        self.assertEqual(result.admitted_keys, ())
        self.assertEqual(result.empty_reason, "no_relevance_evidence")

    def test_multiple_unigram_overlaps_do_not_become_strong_evidence(self):
        item = atomic(content="关闭服务项目")
        query = "关于目录"
        sparse = sparse_result((item,), query)
        self.assertEqual(sparse.hits[0].matched_term_count, 2)
        result = relevance.plan_hybrid_relevance_v1(
            (item,), query_text=query, bm25_result=sparse,
            vector_result=semantic_result((K1, 1.0)),
        )
        self.assertEqual(result.admitted_keys, ())
        self.assertEqual(result.qualified_bm25_keys, ())

    def test_single_character_and_low_information_terms_are_withheld(self):
        for content, query in (
            ("王负责部署", "王"),
            ("R engine", "R"),
            ("the release remains disabled", "the"),
        ):
            with self.subTest(content=content, query=query):
                result = plan((atomic(content=content),), query)
                # Raw postings still work; this policy does not delete tokens.
                self.assertEqual(result.raw_bm25_hit_count, 1)
                self.assertEqual(result.admitted_keys, ())
                self.assertEqual(result.qualified_bm25_keys, ())

    def test_valid_words_bigrams_and_numbers_can_contribute_bm25_ranks(self):
        for content, query in (
            ("Render release disabled", "RENDER"),
            ("归汀记忆可追溯", "归汀"),
            ("数据库版本 16", "16"),
        ):
            with self.subTest(content=content, query=query):
                result = plan((atomic(content=content),), query)
                self.assertEqual(result.admitted_keys, (K1,))
                self.assertEqual(result.qualified_bm25_keys, (K1,))

    def test_high_scores_on_weak_channels_cannot_expand_admission(self):
        items = (atomic(content="Render deployment"), atomic(K2, "关闭服务"))
        query = "Render 相关"
        sparse = sparse_result(items, query)
        # The weak hit's score is deliberately overwhelming. The local term
        # proof remains valid, while a rank/score-only admission would fail.
        sparse = replace(sparse, hits=tuple(sorted(
            (replace(hit, score=1_000_000.0) if hit.memory_key == K2 else hit
             for hit in sparse.hits),
            key=lambda hit: (-hit.score, -hit.matched_term_count, hit.memory_key),
        )))
        result = relevance.plan_hybrid_relevance_v1(
            items, query_text=query, bm25_result=sparse,
            vector_result=semantic_result((K2, 1.0), (K1, 0.1), documents=2),
        )
        self.assertEqual(result.admitted_keys, (K1,))
        self.assertEqual(result.qualified_bm25_keys, (K1,))
        self.assertEqual(result.raw_candidate_count, 2)
        self.assertEqual(result.rejected_count, 1)
        self.assertEqual(result.qualified_vector_keys, ())

    def test_vector_only_positive_is_withheld_until_separately_calibrated(self):
        result = plan((atomic(),), "提交变更后会自行发布上线吗？",
                      semantic_result((K1, 1.0)))
        self.assertEqual(result.raw_lexical_hit_count, 0)
        self.assertEqual(result.raw_vector_hit_count, 1)
        self.assertEqual(result.admitted_keys, ())

    def test_generic_project_overlap_remains_a_documented_lexical_limitation(self):
        result = plan((atomic(),), "这个项目的蓝莓蛋糕烘焙温度是多少？")
        self.assertEqual(result.raw_exact_hit_count, 0)
        self.assertEqual(result.lexical_keys, (K1,))
        self.assertEqual(result.admitted_keys, (K1,))

    def test_metadata_does_not_resurrect_an_excluded_key(self):
        for confidence, confirmed in (
            (0.01, "2020-01-01T00:00:00+00:00"),
            (1.0, "2030-01-01T00:00:00+00:00"),
        ):
            with self.subTest(confidence=confidence, confirmed=confirmed):
                item = atomic(confidence=confidence, last_confirmed_at=confirmed)
                result = plan((item,), UNRELATED_QUERY, semantic_result((K1, 1.0)))
                self.assertEqual(result.admitted_keys, ())

    def test_admission_precedes_top_k_and_preserves_channel_order(self):
        items = tuple(atomic(f"relevance_atomic_{number:06d}", "alpha memory")
                      for number in range(30))
        result = relevance.plan_hybrid_relevance_v1(items, query_text="alpha")
        self.assertGreater(result.admitted_count, fusion.MAX_HITS)
        self.assertEqual(result.admitted_count, 30)
        self.assertEqual(result.lexical_keys, fusion._lexical_channel(items, "alpha"))
        self.assertEqual(result.admitted_keys, tuple(sorted(item.memory_key for item in items)))

    def test_exact_identifier_evidence_keeps_complete_token_boundaries(self):
        for identifier in (
            "dep-daak91hf2nfc73ak97p0",
            "CODEX_GENERATION_ENABLED",
            "c652d094abc12345",
        ):
            with self.subTest(identifier=identifier):
                items = (atomic(content=f"Current literal {identifier}."),
                         atomic(K2, f"prefix-{identifier}-suffix"))
                result = plan(items, identifier)
                self.assertEqual(result.exact_matches, ((K1, 1),))
                self.assertIn(K1, result.admitted_keys)
                # Lexical may still see pieces in K2: it must not become exact.
                self.assertNotIn(K2, dict(result.exact_matches))

    def test_qualified_bm25_order_is_preserved_and_input_results_are_unchanged(self):
        items = (atomic(content="Render Render Render release"),
                 atomic(K2, "Render release"))
        sparse = sparse_result(items, "Render")
        semantic = semantic_result((K2, 0.9), (K1, 0.1), documents=2)
        before = (asdict(sparse), asdict(semantic), tuple(asdict(item) for item in items))
        result = relevance.plan_hybrid_relevance_v1(
            items, query_text="Render", bm25_result=sparse, vector_result=semantic,
        )
        self.assertEqual(result.qualified_bm25_keys, tuple(hit.memory_key for hit in sparse.hits))
        self.assertEqual(result.raw_candidate_count, 2)
        self.assertEqual(result.rejected_count, 0)
        self.assertEqual(before, (asdict(sparse), asdict(semantic),
                                  tuple(asdict(item) for item in items)))

    def test_empty_eligible_set_and_missing_channels_have_distinct_structural_state(self):
        empty = plan((), "Render", semantic_result(documents=0))
        self.assertEqual(empty.empty_reason, "no_eligible_atomics")
        self.assertTrue(empty.bm25_available)
        self.assertTrue(empty.vector_available)
        self.assertEqual(empty.raw_candidate_count, 0)
        missing = relevance.plan_hybrid_relevance_v1((atomic(),), query_text="Render")
        self.assertEqual(missing.admitted_keys, (K1,))
        self.assertFalse(missing.bm25_available)
        self.assertFalse(missing.vector_available)
        self.assertEqual(missing.empty_reason, "none")


class HybridRelevanceBoundaryTests(unittest.TestCase):
    def test_sensitive_and_scoped_atomics_are_never_admitted(self):
        items = (atomic(sensitivity="sensitive"),
                 atomic(K2, scope_type="project", scope_ref="test-project"))
        result = plan(items, "Render Auto-Deploy")
        self.assertEqual(result.eligible_atomic_count, 0)
        self.assertEqual(result.admitted_keys, ())

    def test_unknown_or_ineligible_channel_keys_fail_instead_of_becoming_empty(self):
        normal = atomic()
        sensitive = atomic(K2, sensitivity="sensitive")
        sparse = sparse_result((normal,), "Render")
        for key in (K2, "unknown_relevance_atomic"):
            for channel in ("bm25", "vector"):
                with self.subTest(key=key, channel=channel):
                    kwargs = (
                        {"bm25_result": replace(sparse, hits=(replace(sparse.hits[0], memory_key=key),))}
                        if channel == "bm25" else
                        {"vector_result": semantic_result((key, 1.0))}
                    )
                    with self.assertRaisesRegex(
                        relevance.MemoryRetrievalHybridRelevanceError,
                        f"^invalid_{channel}_result$",
                    ):
                        relevance.plan_hybrid_relevance_v1(
                            (normal, sensitive), query_text="Render", **kwargs,
                        )

    def test_bad_query_or_atomic_snapshot_does_not_report_successful_empty(self):
        for query in (None, "", "   ", "\ud800", "q" * 32_001):
            with self.subTest(query=repr(query)), self.assertRaisesRegex(
                relevance.MemoryRetrievalHybridRelevanceError, "^invalid_query$",
            ):
                relevance.plan_hybrid_relevance_v1((atomic(),), query_text=query)
        for items in ((atomic(), atomic()), (atomic(status="forgotten"),),
                      tuple(atomic(f"relevance_atomic_{n:06d}") for n in range(257))):
            with self.subTest(count=len(items)), self.assertRaisesRegex(
                relevance.MemoryRetrievalHybridRelevanceError, "^invalid_atomics$",
            ):
                relevance.plan_hybrid_relevance_v1(items, query_text="Render")

    def test_bm25_query_and_matched_counts_are_reproved_from_content(self):
        item = atomic()
        query = "Render unrelated"
        sparse = sparse_result((item,), query)
        self.assertEqual(sparse.hits[0].matched_term_count, 1)
        for malformed in (
            replace(sparse, query_term_count=sparse.query_term_count + 1),
            replace(sparse, hits=(replace(sparse.hits[0], matched_term_count=2),)),
            replace(sparse, indexed_document_count=0),
            replace(sparse, hits=(replace(sparse.hits[0], score=float("nan")),)),
        ):
            with self.subTest(result=repr(malformed)), self.assertRaisesRegex(
                relevance.MemoryRetrievalHybridRelevanceError, "^invalid_bm25_result$",
            ):
                relevance.plan_hybrid_relevance_v1(
                    (item,), query_text=query, bm25_result=malformed,
                )

    def test_equal_sized_wrong_query_cannot_forge_bm25_evidence(self):
        sparse = sparse_result((atomic(),), "Render")
        with self.assertRaisesRegex(
            relevance.MemoryRetrievalHybridRelevanceError, "^invalid_bm25_result$",
        ):
            relevance.plan_hybrid_relevance_v1(
                (atomic(),), query_text="blueberry", bm25_result=sparse,
            )

    def test_unobserved_bm25_hits_cannot_be_invented_from_lexical_matches(self):
        sparse = sparse_result((atomic(),), "Render")
        sparse = replace(sparse, hits=())
        result = relevance.plan_hybrid_relevance_v1(
            (atomic(),), query_text="Render", bm25_result=sparse,
        )
        self.assertEqual(result.admitted_keys, (K1,))
        self.assertEqual(result.raw_bm25_hit_count, 0)
        self.assertEqual(result.qualified_bm25_keys, ())

    def test_invalid_vector_results_fail_even_though_semantic_admission_is_off(self):
        item = atomic()
        for malformed in (
            semantic_result((K1, float("nan"))),
            semantic_result((K1, float("inf"))),
            semantic_result((K1, 0.0)),
            semantic_result((K1, True)),
            semantic_result((K1, 1.0), documents=0),
            semantic_result((K1, 1.0), (K1, 0.5)),
            semantic_result((K1, 1.0), documents=True),
        ):
            with self.subTest(result=repr(malformed)), self.assertRaisesRegex(
                relevance.MemoryRetrievalHybridRelevanceError, "^invalid_vector_result$",
            ):
                relevance.plan_hybrid_relevance_v1(
                    (item,), query_text=UNRELATED_QUERY, vector_result=malformed,
                )

    def test_vector_duck_type_hooks_are_not_called(self):
        class HostileHit:
            @property
            def memory_key(self):
                raise AssertionError("private payload must never be accessed")

        raw = vector.VectorSearchResultV1(hits=(HostileHit(),), indexed_document_count=1)
        with self.assertRaisesRegex(
            relevance.MemoryRetrievalHybridRelevanceError, "^invalid_vector_result$",
        ):
            relevance.plan_hybrid_relevance_v1(
                (atomic(),), query_text="Render", vector_result=raw,
            )

    def test_plan_is_immutable_and_does_not_retain_plaintext_terms_scores_or_vectors(self):
        item = atomic(content="Render private-only-fixture")
        result = plan((item,), "Render", semantic_result((K1, 0.7654321)))
        self.assertEqual(result.contract_version, relevance.HYBRID_RELEVANCE_CONTRACT_VERSION)
        self.assertEqual(result.policy_version, "exact-lexical-uncalibrated-v1")
        with self.assertRaises(FrozenInstanceError):
            result.admitted_keys = ()
        encoded = json.dumps(asdict(result), ensure_ascii=False)
        for value in (item.normalized_content, "Render", "a:render", "0.7654321"):
            self.assertNotIn(value, encoded)
        self.assertNotIn(K1, repr(result))
        self.assertNotIn(item.normalized_content, repr(result))

    def test_planner_performs_no_io_environment_read_or_index_rebuild(self):
        item = atomic()
        sparse = sparse_result((item,), EXACT_QUERY)
        semantic = semantic_result((K1, 0.5))
        with ExitStack() as stack:
            for target in (
                "builtins.open", "pathlib.Path.open", "sqlite3.connect",
                "socket.socket", "socket.create_connection", "urllib.request.urlopen",
                "os.getenv", "backend.memory_retrieval_bm25.build_bm25_index_v1",
            ):
                stack.enter_context(patch(target, side_effect=AssertionError("forbidden_io")))
            result = relevance.plan_hybrid_relevance_v1(
                (item,), query_text=EXACT_QUERY, bm25_result=sparse, vector_result=semantic,
            )
        self.assertEqual(result.admitted_keys, (K1,))


if __name__ == "__main__":
    unittest.main()
