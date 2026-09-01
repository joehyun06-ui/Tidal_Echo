"""Bounded in-process observability for Phase 4D-D3B3 Hybrid Retrieval shadow.

This module accepts only already-structural shadow reports and fixed runtime
outcomes. It has no API for query text, Memory keys, Atomic plaintext, vectors,
provider payloads, paths, models, or credentials. State is process-local and
intentionally non-durable: a restart resets all counters. Nothing here
participates in Memory truth, readiness, or retrieval authority.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Final

from backend import memory_retrieval_hybrid_shadow as hybrid_shadow


OBSERVABILITY_CONTRACT_VERSION: Final = "memory-retrieval-hybrid-observability-v1"
MAX_COUNTER: Final = 1_000_000

_RELATIONS: Final = (
    "both_empty",
    "identical",
    "reordered",
    "hybrid_subset",
    "hybrid_superset",
    "mixed",
)
_SKIP_REASONS: Final = (
    "busy",
    "authority_keys_unavailable",
    "loop_unavailable",
    "shadow_unavailable",
)
_LAST_STATUSES: Final = frozenset({"none", "completed", "failed", "skipped", "cancelled"})


def _inc(value: int) -> int:
    return min(MAX_COUNTER, value + 1)


@dataclass(frozen=True, slots=True, repr=False)
class HybridShadowObservabilitySnapshotV1:
    contract_version: str
    attempt_count: int
    started_count: int
    completed_count: int
    failed_count: int
    cancelled_count: int
    skipped_busy_count: int
    skipped_authority_keys_unavailable_count: int
    skipped_loop_unavailable_count: int
    skipped_shadow_unavailable_count: int
    relation_both_empty_count: int
    relation_identical_count: int
    relation_reordered_count: int
    relation_hybrid_subset_count: int
    relation_hybrid_superset_count: int
    relation_mixed_count: int
    bm25_available_count: int
    vector_available_count: int
    query_embedding_performed_count: int
    last_status: str
    last_skip_reason: str
    last_relation: str
    last_authority_selected_count: int
    last_hybrid_selected_count: int
    last_overlap_count: int
    last_exact_hit_count: int
    last_lexical_hit_count: int
    last_bm25_hit_count: int
    last_vector_hit_count: int
    last_bm25_available: bool
    last_vector_available: bool
    last_query_embedding_performed: bool

    def __post_init__(self) -> None:
        _validate_snapshot(self)

    def __repr__(self) -> str:
        try:
            _validate_snapshot(self)
            return (
                "<HybridShadowObservabilitySnapshotV1 "
                f"attempts={self.attempt_count} completed={self.completed_count} "
                f"failed={self.failed_count}>"
            )
        except BaseException:
            return "<HybridShadowObservabilitySnapshotV1 invalid>"


def _validate_snapshot(snapshot: object) -> None:
    if type(snapshot) is not HybridShadowObservabilitySnapshotV1:
        raise ValueError("invalid_hybrid_shadow_observability_snapshot")
    if snapshot.contract_version != OBSERVABILITY_CONTRACT_VERSION:
        raise ValueError("invalid_hybrid_shadow_observability_snapshot")
    integer_names = (
        "attempt_count", "started_count", "completed_count", "failed_count",
        "cancelled_count", "skipped_busy_count",
        "skipped_authority_keys_unavailable_count", "skipped_loop_unavailable_count",
        "skipped_shadow_unavailable_count", "relation_both_empty_count",
        "relation_identical_count", "relation_reordered_count",
        "relation_hybrid_subset_count", "relation_hybrid_superset_count",
        "relation_mixed_count", "bm25_available_count", "vector_available_count",
        "query_embedding_performed_count", "last_authority_selected_count",
        "last_hybrid_selected_count", "last_overlap_count", "last_exact_hit_count",
        "last_lexical_hit_count", "last_bm25_hit_count", "last_vector_hit_count",
    )
    for name in integer_names:
        value = getattr(snapshot, name)
        if type(value) is not int or isinstance(value, bool) or not 0 <= value <= MAX_COUNTER:
            raise ValueError("invalid_hybrid_shadow_observability_snapshot")
    if snapshot.last_status not in _LAST_STATUSES:
        raise ValueError("invalid_hybrid_shadow_observability_snapshot")
    if snapshot.last_skip_reason not in {*_SKIP_REASONS, ""}:
        raise ValueError("invalid_hybrid_shadow_observability_snapshot")
    if snapshot.last_relation not in {*_RELATIONS, ""}:
        raise ValueError("invalid_hybrid_shadow_observability_snapshot")
    for name in (
        "last_bm25_available",
        "last_vector_available",
        "last_query_embedding_performed",
    ):
        if type(getattr(snapshot, name)) is not bool:
            raise ValueError("invalid_hybrid_shadow_observability_snapshot")
    if snapshot.last_status != "skipped" and snapshot.last_skip_reason:
        raise ValueError("invalid_hybrid_shadow_observability_snapshot")
    if snapshot.last_status != "completed":
        if (
            snapshot.last_relation
            or any((
                snapshot.last_authority_selected_count,
                snapshot.last_hybrid_selected_count,
                snapshot.last_overlap_count,
                snapshot.last_exact_hit_count,
                snapshot.last_lexical_hit_count,
                snapshot.last_bm25_hit_count,
                snapshot.last_vector_hit_count,
            ))
            or snapshot.last_bm25_available
            or snapshot.last_vector_available
            or snapshot.last_query_embedding_performed
        ):
            raise ValueError("invalid_hybrid_shadow_observability_snapshot")


class HybridShadowObservabilityV1:
    """Thread-safe bounded counters with no identity-bearing input surface."""

    __slots__ = ("_lock", "_counts", "_relations", "_last")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts = {
            "attempt": 0,
            "started": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
            "skip_busy": 0,
            "skip_authority_keys_unavailable": 0,
            "skip_loop_unavailable": 0,
            "skip_shadow_unavailable": 0,
            "bm25_available": 0,
            "vector_available": 0,
            "query_embedding_performed": 0,
        }
        self._relations = {relation: 0 for relation in _RELATIONS}
        self._last = {
            "status": "none",
            "skip_reason": "",
            "relation": "",
            "authority": 0,
            "hybrid": 0,
            "overlap": 0,
            "exact": 0,
            "lexical": 0,
            "bm25": 0,
            "vector": 0,
            "bm25_available": False,
            "vector_available": False,
            "embedding": False,
        }

    def __repr__(self) -> str:
        return "<HybridShadowObservabilityV1>"

    def _clear_last_comparison(self) -> None:
        self._last.update({
            "relation": "", "authority": 0, "hybrid": 0, "overlap": 0,
            "exact": 0, "lexical": 0, "bm25": 0, "vector": 0,
            "bm25_available": False, "vector_available": False, "embedding": False,
        })

    def record_attempt(self) -> None:
        with self._lock:
            self._counts["attempt"] = _inc(self._counts["attempt"])

    def record_started(self) -> None:
        with self._lock:
            self._counts["started"] = _inc(self._counts["started"])

    def record_skipped(self, reason: object) -> None:
        safe = reason if type(reason) is str and reason in _SKIP_REASONS else "shadow_unavailable"
        with self._lock:
            key = "skip_" + safe
            self._counts[key] = _inc(self._counts[key])
            self._last["status"] = "skipped"
            self._last["skip_reason"] = safe
            self._clear_last_comparison()

    def record_cancelled(self) -> None:
        with self._lock:
            self._counts["cancelled"] = _inc(self._counts["cancelled"])
            self._last["status"] = "cancelled"
            self._last["skip_reason"] = ""
            self._clear_last_comparison()

    def record_report(self, report: object) -> None:
        try:
            values = hybrid_shadow._validated_report(report)
        except BaseException:
            values = None
        with self._lock:
            if values is None or values[1] == "failed":
                self._counts["failed"] = _inc(self._counts["failed"])
                self._last["status"] = "failed"
                self._last["skip_reason"] = ""
                self._clear_last_comparison()
                return
            relation = values[2]
            self._counts["completed"] = _inc(self._counts["completed"])
            self._relations[relation] = _inc(self._relations[relation])
            if values[12]:
                self._counts["bm25_available"] = _inc(self._counts["bm25_available"])
            if values[13]:
                self._counts["vector_available"] = _inc(self._counts["vector_available"])
            if values[14]:
                self._counts["query_embedding_performed"] = _inc(
                    self._counts["query_embedding_performed"]
                )
            self._last.update({
                "status": "completed",
                "skip_reason": "",
                "relation": relation,
                "authority": values[3],
                "hybrid": values[4],
                "overlap": values[5],
                "exact": values[8],
                "lexical": values[9],
                "bm25": values[10],
                "vector": values[11],
                "bm25_available": values[12],
                "vector_available": values[13],
                "embedding": values[14],
            })

    def snapshot(self) -> HybridShadowObservabilitySnapshotV1:
        with self._lock:
            snapshot = HybridShadowObservabilitySnapshotV1(
                contract_version=OBSERVABILITY_CONTRACT_VERSION,
                attempt_count=self._counts["attempt"],
                started_count=self._counts["started"],
                completed_count=self._counts["completed"],
                failed_count=self._counts["failed"],
                cancelled_count=self._counts["cancelled"],
                skipped_busy_count=self._counts["skip_busy"],
                skipped_authority_keys_unavailable_count=self._counts["skip_authority_keys_unavailable"],
                skipped_loop_unavailable_count=self._counts["skip_loop_unavailable"],
                skipped_shadow_unavailable_count=self._counts["skip_shadow_unavailable"],
                relation_both_empty_count=self._relations["both_empty"],
                relation_identical_count=self._relations["identical"],
                relation_reordered_count=self._relations["reordered"],
                relation_hybrid_subset_count=self._relations["hybrid_subset"],
                relation_hybrid_superset_count=self._relations["hybrid_superset"],
                relation_mixed_count=self._relations["mixed"],
                bm25_available_count=self._counts["bm25_available"],
                vector_available_count=self._counts["vector_available"],
                query_embedding_performed_count=self._counts["query_embedding_performed"],
                last_status=self._last["status"],
                last_skip_reason=self._last["skip_reason"],
                last_relation=self._last["relation"],
                last_authority_selected_count=self._last["authority"],
                last_hybrid_selected_count=self._last["hybrid"],
                last_overlap_count=self._last["overlap"],
                last_exact_hit_count=self._last["exact"],
                last_lexical_hit_count=self._last["lexical"],
                last_bm25_hit_count=self._last["bm25"],
                last_vector_hit_count=self._last["vector"],
                last_bm25_available=self._last["bm25_available"],
                last_vector_available=self._last["vector_available"],
                last_query_embedding_performed=self._last["embedding"],
            )
        _validate_snapshot(snapshot)
        return snapshot


def project_status_payload_v1(
    snapshot: object,
    *,
    enabled: object,
    installed: object,
    in_flight: object,
    observability_available: object,
) -> dict:
    """Project only bounded structural rollout state for an authenticated route."""

    _validate_snapshot(snapshot)
    if any(type(value) is not bool for value in (
        enabled, installed, in_flight, observability_available,
    )):
        raise ValueError("invalid_hybrid_shadow_observability_state")
    return {
        "contract_version": OBSERVABILITY_CONTRACT_VERSION,
        "enabled": enabled,
        "installed": installed,
        "observability_available": observability_available,
        "in_flight": in_flight,
        "attempts": snapshot.attempt_count,
        "started": snapshot.started_count,
        "outcomes": {
            "completed": snapshot.completed_count,
            "failed": snapshot.failed_count,
            "cancelled": snapshot.cancelled_count,
            "skipped": {
                "busy": snapshot.skipped_busy_count,
                "authority_keys_unavailable": snapshot.skipped_authority_keys_unavailable_count,
                "loop_unavailable": snapshot.skipped_loop_unavailable_count,
                "shadow_unavailable": snapshot.skipped_shadow_unavailable_count,
            },
        },
        "relations": {
            "both_empty": snapshot.relation_both_empty_count,
            "identical": snapshot.relation_identical_count,
            "reordered": snapshot.relation_reordered_count,
            "hybrid_subset": snapshot.relation_hybrid_subset_count,
            "hybrid_superset": snapshot.relation_hybrid_superset_count,
            "mixed": snapshot.relation_mixed_count,
        },
        "channels": {
            "bm25_available": snapshot.bm25_available_count,
            "vector_available": snapshot.vector_available_count,
            "query_embedding_performed": snapshot.query_embedding_performed_count,
        },
        "last": {
            "status": snapshot.last_status,
            "skip_reason": snapshot.last_skip_reason,
            "relation": snapshot.last_relation,
            "authority_selected": snapshot.last_authority_selected_count,
            "hybrid_selected": snapshot.last_hybrid_selected_count,
            "overlap": snapshot.last_overlap_count,
            "exact_hits": snapshot.last_exact_hit_count,
            "lexical_hits": snapshot.last_lexical_hit_count,
            "bm25_hits": snapshot.last_bm25_hit_count,
            "vector_hits": snapshot.last_vector_hit_count,
            "bm25_available": snapshot.last_bm25_available,
            "vector_available": snapshot.last_vector_available,
            "query_embedding_performed": snapshot.last_query_embedding_performed,
        },
    }


__all__ = (
    "HybridShadowObservabilitySnapshotV1",
    "HybridShadowObservabilityV1",
    "MAX_COUNTER",
    "OBSERVABILITY_CONTRACT_VERSION",
    "project_status_payload_v1",
)
