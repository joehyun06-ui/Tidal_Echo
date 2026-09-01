"""Data-free structural comparison for Phase 4D-D3B hybrid retrieval shadowing.

The comparator receives the exact Memory keys selected by the provider-visible
retrieval authority plus one already-computed D3A hybrid query result.  It emits
counts and a set/order relation only.  No Memory key, Atomic plaintext, query
text, embedding vector, or provider payload is retained by the report or its
telemetry renderer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from backend import memory_retrieval_hybrid_query as hybrid_query


HYBRID_SHADOW_CONTRACT_VERSION: Final = "memory-retrieval-hybrid-shadow-v1"
SHADOW_FAILURE_CATEGORY: Final = "memory_hybrid_retrieval_shadow_unavailable"
MAX_SELECTED: Final = 10
_MEMORY_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_RELATIONS: Final = frozenset({
    "both_empty",
    "identical",
    "reordered",
    "hybrid_subset",
    "hybrid_superset",
    "mixed",
})


class _ShadowUnavailable(RuntimeError):
    __slots__ = ()

    def __init__(self):
        super().__init__(SHADOW_FAILURE_CATEGORY)


@dataclass(frozen=True, slots=True, repr=False)
class HybridRetrievalShadowReportV1:
    contract_version: str
    status: str
    relation: str = ""
    authority_selected_count: int = 0
    hybrid_selected_count: int = 0
    overlap_count: int = 0
    authority_only_count: int = 0
    hybrid_only_count: int = 0
    exact_hit_count: int = 0
    lexical_hit_count: int = 0
    bm25_hit_count: int = 0
    vector_hit_count: int = 0
    bm25_available: bool = False
    vector_available: bool = False
    query_embedding_performed: bool = False

    def __post_init__(self) -> None:
        _validated_report(self)

    @property
    def category(self) -> str:
        return SHADOW_FAILURE_CATEGORY if self.status == "failed" else ""

    @classmethod
    def failed(cls) -> "HybridRetrievalShadowReportV1":
        return cls(
            contract_version=HYBRID_SHADOW_CONTRACT_VERSION,
            status="failed",
        )

    def __repr__(self) -> str:
        try:
            values = _validated_report(self)
            if values[1] == "failed":
                return (
                    "<HybridRetrievalShadowReportV1 status=failed "
                    "category=memory_hybrid_retrieval_shadow_unavailable>"
                )
            return (
                "<HybridRetrievalShadowReportV1 "
                f"relation={values[2]} authority={values[3]} hybrid={values[4]} "
                f"overlap={values[5]} exact={values[8]} lexical={values[9]} "
                f"bm25={values[10]} vector={values[11]} "
                f"embedding={values[14]}>"
            )
        except BaseException:
            return "<HybridRetrievalShadowReportV1 invalid>"


def _validated_report(report: object) -> tuple:
    try:
        if type(report) is not HybridRetrievalShadowReportV1:
            raise _ShadowUnavailable()
        values = (
            object.__getattribute__(report, "contract_version"),
            object.__getattribute__(report, "status"),
            object.__getattribute__(report, "relation"),
            object.__getattribute__(report, "authority_selected_count"),
            object.__getattribute__(report, "hybrid_selected_count"),
            object.__getattribute__(report, "overlap_count"),
            object.__getattribute__(report, "authority_only_count"),
            object.__getattribute__(report, "hybrid_only_count"),
            object.__getattribute__(report, "exact_hit_count"),
            object.__getattribute__(report, "lexical_hit_count"),
            object.__getattribute__(report, "bm25_hit_count"),
            object.__getattribute__(report, "vector_hit_count"),
            object.__getattribute__(report, "bm25_available"),
            object.__getattribute__(report, "vector_available"),
            object.__getattribute__(report, "query_embedding_performed"),
        )
        (
            contract_version,
            status,
            relation,
            authority_count,
            hybrid_count,
            overlap,
            authority_only,
            hybrid_only,
            exact_count,
            lexical_count,
            bm25_count,
            vector_count,
            bm25_available,
            vector_available,
            embedding_performed,
        ) = values
        if (
            contract_version != HYBRID_SHADOW_CONTRACT_VERSION
            or type(status) is not str
            or status not in {"completed", "failed"}
            or type(relation) is not str
            or any(
                type(value) is not int or isinstance(value, bool) or value < 0
                for value in (
                    authority_count,
                    hybrid_count,
                    overlap,
                    authority_only,
                    hybrid_only,
                    exact_count,
                    lexical_count,
                    bm25_count,
                    vector_count,
                )
            )
            or any(
                type(value) is not bool
                for value in (bm25_available, vector_available, embedding_performed)
            )
        ):
            raise _ShadowUnavailable()
        if status == "failed":
            if (
                relation
                or any(values[3:12])
                or bm25_available
                or vector_available
                or embedding_performed
            ):
                raise _ShadowUnavailable()
            return values
        if (
            relation not in _RELATIONS
            or authority_count > MAX_SELECTED
            or hybrid_count > MAX_SELECTED
            or overlap > min(authority_count, hybrid_count)
            or authority_only != authority_count - overlap
            or hybrid_only != hybrid_count - overlap
            or not bm25_available and bm25_count != 0
            or not vector_available and vector_count != 0
            or embedding_performed and not vector_available
        ):
            raise _ShadowUnavailable()
        both_empty = authority_count == hybrid_count == overlap == 0
        same_set = (
            authority_count > 0
            and authority_count == hybrid_count == overlap
            and authority_only == hybrid_only == 0
        )
        hybrid_subset = (
            hybrid_count < authority_count
            and overlap == hybrid_count
            and hybrid_only == 0
            and authority_only > 0
        )
        hybrid_superset = (
            authority_count < hybrid_count
            and overlap == authority_count
            and authority_only == 0
            and hybrid_only > 0
        )
        if not (
            (relation == "both_empty" and both_empty)
            or (relation in {"identical", "reordered"} and same_set)
            or (relation == "hybrid_subset" and hybrid_subset)
            or (relation == "hybrid_superset" and hybrid_superset)
            or (
                relation == "mixed"
                and (authority_count > 0 or hybrid_count > 0)
                and not same_set
                and not hybrid_subset
                and not hybrid_superset
            )
        ):
            raise _ShadowUnavailable()
        return values
    except _ShadowUnavailable:
        raise
    except BaseException:
        raise _ShadowUnavailable() from None


def _validated_keys(raw: object) -> tuple[str, ...]:
    if type(raw) is not tuple or len(raw) > MAX_SELECTED:
        raise _ShadowUnavailable()
    seen: set[str] = set()
    result: list[str] = []
    for key in raw:
        if (
            type(key) is not str
            or _MEMORY_KEY_PATTERN.fullmatch(key) is None
            or key in seen
        ):
            raise _ShadowUnavailable()
        seen.add(key)
        result.append(key)
    return tuple(result)


def compare_hybrid_retrieval_shadow_v1(
    authoritative_memory_keys: object,
    hybrid_result: object,
) -> HybridRetrievalShadowReportV1:
    """Return a fail-soft, identity-free comparison against actual authority."""

    try:
        authority = _validated_keys(authoritative_memory_keys)
        if type(hybrid_result) is not hybrid_query.HybridQueryResultV1:
            raise _ShadowUnavailable()
        fusion = object.__getattribute__(hybrid_result, "fusion_result")
        hits = object.__getattribute__(fusion, "hits")
        if type(hits) is not tuple or len(hits) > MAX_SELECTED:
            raise _ShadowUnavailable()
        hybrid = _validated_keys(tuple(
            object.__getattribute__(hit, "memory_key") for hit in hits
        ))
        authority_set = set(authority)
        hybrid_set = set(hybrid)
        overlap = len(authority_set.intersection(hybrid_set))
        if not authority and not hybrid:
            relation = "both_empty"
        elif authority_set == hybrid_set:
            relation = "identical" if authority == hybrid else "reordered"
        elif hybrid_set < authority_set:
            relation = "hybrid_subset"
        elif authority_set < hybrid_set:
            relation = "hybrid_superset"
        else:
            relation = "mixed"
        report = HybridRetrievalShadowReportV1(
            contract_version=HYBRID_SHADOW_CONTRACT_VERSION,
            status="completed",
            relation=relation,
            authority_selected_count=len(authority),
            hybrid_selected_count=len(hybrid),
            overlap_count=overlap,
            authority_only_count=len(authority) - overlap,
            hybrid_only_count=len(hybrid) - overlap,
            exact_hit_count=object.__getattribute__(fusion, "exact_hit_count"),
            lexical_hit_count=object.__getattribute__(fusion, "lexical_hit_count"),
            bm25_hit_count=object.__getattribute__(fusion, "bm25_hit_count"),
            vector_hit_count=object.__getattribute__(fusion, "vector_hit_count"),
            bm25_available=object.__getattribute__(fusion, "bm25_available"),
            vector_available=object.__getattribute__(fusion, "vector_available"),
            query_embedding_performed=object.__getattribute__(
                hybrid_result, "query_embedding_performed"
            ),
        )
        _validated_report(report)
        return report
    except BaseException:
        return HybridRetrievalShadowReportV1.failed()


def render_hybrid_retrieval_shadow_telemetry_v1(report: object) -> str | None:
    """Render one bounded telemetry line containing no Memory identities."""

    try:
        values = _validated_report(report)
        if values[1] == "failed":
            return (
                "[memory-hybrid-retrieval-shadow] status=failed "
                "category=memory_hybrid_retrieval_shadow_unavailable"
            )
        return (
            "[memory-hybrid-retrieval-shadow] status=completed "
            f"relation={values[2]} authority={values[3]} hybrid={values[4]} "
            f"overlap={values[5]} authority_only={values[6]} "
            f"hybrid_only={values[7]} exact={values[8]} lexical={values[9]} "
            f"bm25={values[10]} vector={values[11]} "
            f"bm25_available={str(values[12]).lower()} "
            f"vector_available={str(values[13]).lower()} "
            f"embedding={str(values[14]).lower()}"
        )
    except BaseException:
        return None


__all__ = (
    "HYBRID_SHADOW_CONTRACT_VERSION",
    "HybridRetrievalShadowReportV1",
    "SHADOW_FAILURE_CATEGORY",
    "compare_hybrid_retrieval_shadow_v1",
    "render_hybrid_retrieval_shadow_telemetry_v1",
)
