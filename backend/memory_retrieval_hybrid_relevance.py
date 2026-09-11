"""Pure, uncalibrated relevance admission for Hybrid Retrieval.

It accepts the same already-proved
Atomic snapshot and channel results as fusion, and admits only existing exact
or lexical matches. BM25 can contribute a rank for an admitted key only after
its matched terms are independently checked against that snapshot and query.
CJK unigram-only and low-information word matches cannot contribute a rank.

Vector hits remain raw observations. There is no threshold/profile input in
this first policy: a separately reviewed calibration contract is required
before vector evidence can admit a key or contribute to RRF.

The plan contains keys and structural counts, never query/Atomic text, terms,
scores or vectors. Channel tuples preserve channel order; admitted_keys is a
sorted set, not a fused ranking. No top-K or character budget is applied here.
An empty plan is valid. Invalid inputs still raise fixed-category errors.

This module owns no I/O, provider call, index repair, runtime gate or Memory
authority. In particular, local term checks do not prove sidecar freshness or
BM25 score provenance; the caller must retain the existing same-revision proof.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Final

from backend import memory_hierarchy_projection as hierarchy
from backend import memory_retrieval_bm25 as bm25
from backend import memory_retrieval_hybrid_fusion as fusion
from backend import memory_retrieval_v2 as lexical_v2
from backend import memory_retrieval_vector as vector


HYBRID_RELEVANCE_CONTRACT_VERSION: Final = "memory-retrieval-hybrid-relevance-v1"
HYBRID_RELEVANCE_POLICY_VERSION: Final = "exact-lexical-uncalibrated-v1"
HYBRID_RELEVANCE_SUMMARY_VERSION: Final = "memory-retrieval-hybrid-relevance-summary-v1"
_ERROR_CATEGORIES: Final = frozenset({
    "invalid_atomics",
    "invalid_bm25_result",
    "invalid_query",
    "invalid_vector_result",
    "invalid_relevance_summary",
    "memory_retrieval_hybrid_relevance_error",
})


class MemoryRetrievalHybridRelevanceError(ValueError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_retrieval_hybrid_relevance_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        return object.__getattribute__(self, "category")

    def __repr__(self) -> str:
        return f"MemoryRetrievalHybridRelevanceError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryRetrievalHybridRelevanceError(category) from None


@dataclass(frozen=True, slots=True, repr=False)
class HybridRelevancePlanV1:
    contract_version: str
    policy_version: str
    exact_matches: tuple[tuple[str, int], ...] = field(repr=False)
    lexical_keys: tuple[str, ...] = field(repr=False)
    qualified_bm25_keys: tuple[str, ...] = field(repr=False)
    admitted_keys: tuple[str, ...] = field(repr=False)
    eligible_atomic_count: int
    raw_candidate_count: int
    raw_bm25_hit_count: int
    raw_vector_hit_count: int
    bm25_available: bool
    vector_available: bool

    @property
    def raw_exact_hit_count(self) -> int:
        return len(self.exact_matches)

    @property
    def raw_lexical_hit_count(self) -> int:
        return len(self.lexical_keys)

    @property
    def admitted_count(self) -> int:
        return len(self.admitted_keys)

    @property
    def rejected_count(self) -> int:
        return self.raw_candidate_count - self.admitted_count

    @property
    def qualified_bm25_hit_count(self) -> int:
        return len(self.qualified_bm25_keys)

    @property
    def excluded_bm25_hit_count(self) -> int:
        return self.raw_bm25_hit_count - self.qualified_bm25_hit_count

    @property
    def qualified_vector_keys(self) -> tuple[str, ...]:
        return ()

    @property
    def semantic_admission_enabled(self) -> bool:
        return False

    @property
    def empty_reason(self) -> str:
        if self.admitted_keys:
            return "none"
        if self.eligible_atomic_count == 0:
            return "no_eligible_atomics"
        return "no_relevance_evidence"

    def __repr__(self) -> str:
        return (
            "<HybridRelevancePlanV1 "
            f"eligible={self.eligible_atomic_count} raw={self.raw_candidate_count} "
            f"admitted={self.admitted_count} rejected={self.rejected_count} "
            f"qualified_bm25={self.qualified_bm25_hit_count} "
            f"observed_vector={self.raw_vector_hit_count}>"
        )


@dataclass(frozen=True, slots=True, repr=False)
class HybridRelevanceSummaryV1:
    """Bounded counts only; safe to carry into Shadow observability."""

    contract_version: str
    policy_version: str
    eligible_atomic_count: int
    raw_candidate_count: int
    admitted_count: int
    rejected_count: int
    qualified_bm25_hit_count: int
    qualified_vector_hit_count: int
    selected_count: int
    truncated_count: int
    empty_reason: str

    def __post_init__(self) -> None:
        validate_hybrid_relevance_summary_v1(self)

    def __repr__(self) -> str:
        try:
            validate_hybrid_relevance_summary_v1(self)
            return (
                "<HybridRelevanceSummaryV1 "
                f"admitted={self.admitted_count} rejected={self.rejected_count} "
                f"selected={self.selected_count}>"
            )
        except Exception:
            return "<HybridRelevanceSummaryV1 invalid>"


def validate_hybrid_relevance_summary_v1(raw: object) -> HybridRelevanceSummaryV1:
    if type(raw) is not HybridRelevanceSummaryV1:
        _raise("invalid_relevance_summary")
    if (
        type(raw.contract_version) is not str
        or raw.contract_version != HYBRID_RELEVANCE_SUMMARY_VERSION
        or type(raw.policy_version) is not str
        or raw.policy_version != HYBRID_RELEVANCE_POLICY_VERSION
        or type(raw.empty_reason) is not str
    ):
        _raise("invalid_relevance_summary")
    for value in (
        raw.eligible_atomic_count, raw.raw_candidate_count, raw.admitted_count,
        raw.rejected_count, raw.qualified_bm25_hit_count, raw.qualified_vector_hit_count,
        raw.selected_count, raw.truncated_count,
    ):
        if type(value) is not int or not 0 <= value <= hierarchy.MAX_ATOMICS:
            _raise("invalid_relevance_summary")
    expected_empty = (
        "none" if raw.admitted_count else
        "no_relevance_evidence" if raw.eligible_atomic_count else
        "no_eligible_atomics"
    )
    if (
        raw.raw_candidate_count > raw.eligible_atomic_count
        or raw.admitted_count + raw.rejected_count != raw.raw_candidate_count
        or raw.selected_count + raw.truncated_count != raw.admitted_count
        or raw.selected_count > fusion.MAX_HITS
        or (raw.admitted_count > 0 and raw.selected_count == 0)
        or raw.qualified_bm25_hit_count > min(raw.admitted_count, bm25.MAX_HITS)
        or raw.qualified_vector_hit_count != 0
        or raw.empty_reason != expected_empty
    ):
        _raise("invalid_relevance_summary")
    return raw


def project_hybrid_relevance_summary_v1(raw: object) -> dict:
    summary = validate_hybrid_relevance_summary_v1(raw)
    return {
        "contract_version": summary.contract_version,
        "policy_version": summary.policy_version,
        "eligible_atomic_count": summary.eligible_atomic_count,
        "raw_candidate_count": summary.raw_candidate_count,
        "admitted_count": summary.admitted_count,
        "rejected_count": summary.rejected_count,
        "qualified_bm25_hit_count": summary.qualified_bm25_hit_count,
        "qualified_vector_hit_count": summary.qualified_vector_hit_count,
        "selected_count": summary.selected_count,
        "truncated_count": summary.truncated_count,
        "empty_reason": summary.empty_reason,
    }


def _validated_vector(
    raw: object,
    eligible_keys: frozenset[str],
) -> vector.VectorSearchResultV1 | None:
    if raw is None:
        return None
    # Check exact hit types before the older fusion validator accesses fields.
    if (
        type(raw) is not vector.VectorSearchResultV1
        or type(raw.hits) is not tuple
        or len(raw.hits) > vector.MAX_VECTOR_HITS
        or any(type(hit) is not vector.VectorSearchHitV1 for hit in raw.hits)
    ):
        _raise("invalid_vector_result")
    try:
        validated = fusion._validated_vector(raw, eligible_keys)
        if len(raw.hits) > raw.indexed_document_count:
            _raise("invalid_vector_result")
        return validated
    except Exception:
        _raise("invalid_vector_result")


def _usable_bm25_term(term: str) -> bool:
    # Terms are produced locally by the existing versioned tokenizer. Keep its
    # single-character postings intact, while requiring usable rank evidence.
    return term.startswith("b:") or (
        term.startswith("a:") and lexical_v2._is_usable_alphanumeric(term[2:])
    )


def plan_hybrid_relevance_v1(
    atomics: object,
    *,
    query_text: object,
    bm25_result: object = None,
    vector_result: object = None,
) -> HybridRelevancePlanV1:
    """Plan admission before fusion/budgets, with semantic admission withheld."""

    try:
        eligible, by_key = fusion._validated_atomics(atomics)
    except Exception:
        _raise("invalid_atomics")
    try:
        query = fusion._validated_query(query_text)
        query_terms = frozenset(bm25.tokenize_lexical_terms_v1(query, query=True))
        exact = fusion._exact_channel(eligible, query)
        lexical = fusion._lexical_channel(eligible, query)
    except Exception:
        _raise("invalid_query")

    eligible_keys = frozenset(by_key)
    try:
        sparse = fusion._validated_bm25(bm25_result, eligible_keys)
        if sparse is not None and (
            len(sparse.hits) > sparse.indexed_document_count
            or sparse.query_term_count != len(query_terms)
        ):
            _raise("invalid_bm25_result")
    except Exception:
        _raise("invalid_bm25_result")
    semantic = _validated_vector(vector_result, eligible_keys)

    admitted = frozenset(key for key, _count in exact) | frozenset(lexical)
    qualified_bm25: list[str] = []
    sparse_keys: set[str] = set()
    if sparse is not None:
        for hit in sparse.hits:
            sparse_keys.add(hit.memory_key)
            try:
                content_terms = frozenset(bm25.tokenize_lexical_terms_v1(
                    by_key[hit.memory_key].normalized_content,
                ))
            except Exception:
                _raise("invalid_atomics")
            matched_terms = query_terms.intersection(content_terms)
            # Reported overlap counts are not evidence. Re-prove the exact
            # count as well, so a mismatched query/result is an error, not empty.
            if len(matched_terms) != hit.matched_term_count:
                _raise("invalid_bm25_result")
            if hit.memory_key in admitted and any(
                _usable_bm25_term(term) for term in matched_terms
            ):
                qualified_bm25.append(hit.memory_key)

    vector_keys = (
        {hit.memory_key for hit in semantic.hits}
        if semantic is not None else set()
    )
    raw_keys = admitted | sparse_keys | vector_keys
    return HybridRelevancePlanV1(
        contract_version=HYBRID_RELEVANCE_CONTRACT_VERSION,
        policy_version=HYBRID_RELEVANCE_POLICY_VERSION,
        exact_matches=exact,
        lexical_keys=lexical,
        qualified_bm25_keys=tuple(qualified_bm25),
        admitted_keys=tuple(sorted(admitted)),
        eligible_atomic_count=len(eligible),
        raw_candidate_count=len(raw_keys),
        raw_bm25_hit_count=len(sparse.hits) if sparse is not None else 0,
        raw_vector_hit_count=len(semantic.hits) if semantic is not None else 0,
        bm25_available=sparse is not None,
        vector_available=semantic is not None,
    )


def fuse_hybrid_retrieval_with_relevance_v1(
    atomics: object,
    *,
    query_text: object,
    bm25_result: object,
    vector_result: object,
    reference_time: object,
    touch_hints: object = (),
    max_hits: object = fusion.MAX_HITS,
) -> tuple[fusion.HybridFusionResultV1, HybridRelevanceSummaryV1]:
    """Apply admission before RRF/top-K, retaining the original raw counts."""

    admission = plan_hybrid_relevance_v1(
        atomics, query_text=query_text,
        bm25_result=bm25_result, vector_result=vector_result,
    )
    qualified_keys = frozenset(admission.qualified_bm25_keys)
    qualified_sparse = (
        replace(bm25_result, hits=tuple(
            hit for hit in bm25_result.hits if hit.memory_key in qualified_keys
        )) if bm25_result is not None else None
    )
    qualified_vector = (
        replace(vector_result, hits=()) if vector_result is not None else None
    )
    # The existing exact/lexical channels enumerate all admitted keys. Filtering
    # the new channels before this call prevents weak hits from taking top-K
    # slots or receiving metadata boosts. Channel ranks are recomputed normally.
    ranked = fusion.fuse_hybrid_retrieval_v1(
        atomics, query_text=query_text,
        bm25_result=qualified_sparse, vector_result=qualified_vector,
        reference_time=reference_time, touch_hints=touch_hints, max_hits=max_hits,
    )
    result = replace(
        ranked,
        bm25_hit_count=admission.raw_bm25_hit_count,
        vector_hit_count=admission.raw_vector_hit_count,
    )
    summary = HybridRelevanceSummaryV1(
        contract_version=HYBRID_RELEVANCE_SUMMARY_VERSION,
        policy_version=HYBRID_RELEVANCE_POLICY_VERSION,
        eligible_atomic_count=admission.eligible_atomic_count,
        raw_candidate_count=admission.raw_candidate_count,
        admitted_count=admission.admitted_count,
        rejected_count=admission.rejected_count,
        qualified_bm25_hit_count=admission.qualified_bm25_hit_count,
        qualified_vector_hit_count=0,
        selected_count=len(result.hits),
        truncated_count=admission.admitted_count - len(result.hits),
        empty_reason=admission.empty_reason,
    )
    return result, summary


__all__ = (
    "HYBRID_RELEVANCE_CONTRACT_VERSION",
    "HYBRID_RELEVANCE_POLICY_VERSION",
    "HYBRID_RELEVANCE_SUMMARY_VERSION",
    "HybridRelevancePlanV1",
    "HybridRelevanceSummaryV1",
    "MemoryRetrievalHybridRelevanceError",
    "fuse_hybrid_retrieval_with_relevance_v1",
    "plan_hybrid_relevance_v1",
    "project_hybrid_relevance_summary_v1",
    "validate_hybrid_relevance_summary_v1",
)
