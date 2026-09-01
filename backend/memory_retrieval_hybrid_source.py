"""Same-revision hybrid retrieval source composition for Phase 4D-D2.

D2 binds the authoritative active Atomic Memory snapshot and any present C1 BM25
or C3 vector sidecar to one exact Atomic snapshot revision before those channel
results may enter the pure D1 fusion contract.

Missing channels are explicit: pass no sidecar for that channel and D1 degrades
to the remaining channels. A configured/present sidecar is never silently
accepted when stale, corrupt, forged, or bound to a different Atomic revision.

Each sidecar is loaded exactly once per invocation. The exact immutable in-memory
plan that passes revision/integrity proof is also the plan used for search, so a
sidecar replacement cannot race between proof and ranking.

This module owns no Memory truth, writes, embedding provider, hierarchy expansion,
prompt rendering, runtime wiring, deployment gate, or retrieval authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from backend import (
    memory_hierarchy_baseline,
    memory_hierarchy_projection as hierarchy,
    memory_hierarchy_snapshot,
    memory_retrieval_bm25 as bm25,
    memory_retrieval_bm25_store as bm25_store,
    memory_retrieval_hybrid_fusion as fusion,
    memory_retrieval_vector as vector,
    memory_retrieval_vector_store as vector_store,
)


HYBRID_SOURCE_CONTRACT_VERSION: Final = "memory-retrieval-hybrid-source-v1"

_ERROR_CATEGORIES: Final = frozenset({
    "hybrid_source_authority_unavailable",
    "hybrid_source_bm25_invalid",
    "hybrid_source_configuration_invalid",
    "hybrid_source_fusion_invalid",
    "hybrid_source_stale",
    "hybrid_source_vector_invalid",
    "memory_retrieval_hybrid_source_error",
})


class MemoryRetrievalHybridSourceError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_retrieval_hybrid_source_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "memory_retrieval_hybrid_source_error"

    def __repr__(self) -> str:
        return f"MemoryRetrievalHybridSourceError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryRetrievalHybridSourceError(category)


@dataclass(frozen=True, slots=True, repr=False)
class HybridSourceResultV1:
    contract_version: str
    source_atomic_count: int
    bm25_generation: int | None
    vector_generation: int | None
    fusion_result: fusion.HybridFusionResultV1 = field(repr=False)

    def __repr__(self) -> str:
        return (
            "<HybridSourceResultV1 "
            f"source_atomics={self.source_atomic_count} "
            f"bm25_generation={self.bm25_generation!r} "
            f"vector_generation={self.vector_generation!r} "
            f"hits={len(self.fusion_result.hits)}>"
        )


def _validate_configuration(
    reader: object,
    bm25_sidecar_path: object,
    term_key_id: object,
    term_hmac_secret: object,
    vector_sidecar_path: object,
    query_vector: object,
) -> tuple[Path, Path | None, Path | None]:
    if type(reader) is not memory_hierarchy_snapshot.MemoryHierarchySnapshotReader:
        _raise("hybrid_source_configuration_invalid")

    bm25_enabled = bm25_sidecar_path is not None
    if bm25_enabled != (term_key_id is not None and term_hmac_secret is not None):
        _raise("hybrid_source_configuration_invalid")
    if not bm25_enabled and (term_key_id is not None or term_hmac_secret is not None):
        _raise("hybrid_source_configuration_invalid")

    vector_enabled = vector_sidecar_path is not None
    if vector_enabled != (query_vector is not None):
        _raise("hybrid_source_configuration_invalid")

    try:
        authority = Path(reader._database_path).resolve(strict=False)
        bm25_path = (
            Path(bm25_sidecar_path).resolve(strict=False)
            if bm25_enabled
            else None
        )
        vector_path = (
            Path(vector_sidecar_path).resolve(strict=False)
            if vector_enabled
            else None
        )
    except (AttributeError, OSError, TypeError, ValueError):
        _raise("hybrid_source_configuration_invalid")

    paths = [authority]
    if bm25_path is not None:
        paths.append(bm25_path)
    if vector_path is not None:
        paths.append(vector_path)
    if len(set(paths)) != len(paths):
        _raise("hybrid_source_configuration_invalid")
    return authority, bm25_path, vector_path


def _load_authoritative_snapshot(
    reader: memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
) -> tuple[
    memory_hierarchy_snapshot.HierarchyAtomicSnapshotV1,
    str,
]:
    try:
        snapshot = reader.load_active_snapshot()
        if type(snapshot) is not memory_hierarchy_snapshot.HierarchyAtomicSnapshotV1:
            _raise("hybrid_source_authority_unavailable")
        baseline = memory_hierarchy_baseline.build_baseline_hierarchy_plan_v1(
            snapshot.atomics
        )
        return snapshot, baseline.atomic_snapshot_digest
    except MemoryRetrievalHybridSourceError:
        raise
    except (
        memory_hierarchy_snapshot.MemoryHierarchySnapshotError,
        memory_hierarchy_baseline.MemoryHierarchyBaselineError,
    ):
        _raise("hybrid_source_authority_unavailable")
    except Exception:
        _raise("hybrid_source_authority_unavailable")


def _load_current_bm25(
    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
    current_digest: str,
    path: Path,
    query_text: object,
    *,
    term_key_id: object,
    term_hmac_secret: object,
    max_hits: object,
) -> tuple[bm25.BM25SearchResultV1, int]:
    try:
        stored = bm25_store.load_bm25_store_snapshot(path)
        if stored.plan.source_snapshot_digest != current_digest:
            _raise("hybrid_source_stale")
        expected = bm25.build_bm25_index_v1(
            atomics,
            source_snapshot_digest=current_digest,
            term_key_id=term_key_id,
            term_hmac_secret=term_hmac_secret,
        )
        if stored.plan != expected:
            _raise("hybrid_source_bm25_invalid")
        result = bm25.search_bm25_index_v1(
            stored.plan,
            query_text,
            term_key_id=term_key_id,
            term_hmac_secret=term_hmac_secret,
            max_hits=max_hits,
        )
        return result, stored.generation
    except MemoryRetrievalHybridSourceError:
        raise
    except (
        bm25.MemoryRetrievalBM25Error,
        bm25_store.MemoryRetrievalBM25StoreError,
    ):
        _raise("hybrid_source_bm25_invalid")
    except Exception:
        _raise("hybrid_source_bm25_invalid")


def _expected_vector_bindings(
    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
) -> tuple[tuple[str, str], ...]:
    try:
        validated, _ = hierarchy._validate_atomics(atomics)
    except hierarchy.MemoryHierarchyProjectionError:
        _raise("hybrid_source_authority_unavailable")
    eligible = tuple(
        item
        for item in validated
        if (
            item.status == "active"
            and item.scope_type == "global_user"
            and item.scope_ref == ""
            and item.sensitivity == "normal"
        )
    )
    return tuple(
        (
            item.memory_key,
            hierarchy._atomic_revision_digest(item),
        )
        for item in eligible
    )


def _load_current_vector(
    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
    current_digest: str,
    path: Path,
    query_vector: object,
    *,
    max_hits: object,
    minimum_similarity: object,
) -> tuple[vector.VectorSearchResultV1, int]:
    try:
        stored = vector_store.load_vector_store_snapshot(path)
        if stored.plan.source_snapshot_digest != current_digest:
            _raise("hybrid_source_stale")
        actual_bindings = tuple(
            (document.memory_key, document.atomic_revision_digest)
            for document in stored.plan.documents
        )
        if actual_bindings != _expected_vector_bindings(atomics):
            _raise("hybrid_source_vector_invalid")
        result = vector.search_vector_index_v1(
            stored.plan,
            query_vector,
            max_hits=max_hits,
            minimum_similarity=minimum_similarity,
        )
        return result, stored.generation
    except MemoryRetrievalHybridSourceError:
        raise
    except (
        vector.MemoryRetrievalVectorError,
        vector_store.MemoryRetrievalVectorStoreError,
    ):
        _raise("hybrid_source_vector_invalid")
    except Exception:
        _raise("hybrid_source_vector_invalid")


def fuse_current_hybrid_retrieval_v1(
    reader: object,
    *,
    query_text: object,
    reference_time: object,
    bm25_sidecar_path: object = None,
    term_key_id: object = None,
    term_hmac_secret: object = None,
    vector_sidecar_path: object = None,
    query_vector: object = None,
    touch_hints: object = (),
    max_hits: object = fusion.MAX_HITS,
    max_bm25_hits: object = bm25.MAX_HITS,
    max_vector_hits: object = vector.MAX_VECTOR_HITS,
    minimum_vector_similarity: object = 0.0,
) -> HybridSourceResultV1:
    """Fuse only channel results proved against one authoritative Atomic revision."""

    _, bm25_path, vector_path = _validate_configuration(
        reader,
        bm25_sidecar_path,
        term_key_id,
        term_hmac_secret,
        vector_sidecar_path,
        query_vector,
    )
    snapshot, current_digest = _load_authoritative_snapshot(reader)

    sparse: bm25.BM25SearchResultV1 | None = None
    bm25_generation: int | None = None
    if bm25_path is not None:
        sparse, bm25_generation = _load_current_bm25(
            snapshot.atomics,
            current_digest,
            bm25_path,
            query_text,
            term_key_id=term_key_id,
            term_hmac_secret=term_hmac_secret,
            max_hits=max_bm25_hits,
        )

    semantic: vector.VectorSearchResultV1 | None = None
    vector_generation: int | None = None
    if vector_path is not None:
        semantic, vector_generation = _load_current_vector(
            snapshot.atomics,
            current_digest,
            vector_path,
            query_vector,
            max_hits=max_vector_hits,
            minimum_similarity=minimum_vector_similarity,
        )

    try:
        result = fusion.fuse_hybrid_retrieval_v1(
            snapshot.atomics,
            query_text=query_text,
            bm25_result=sparse,
            vector_result=semantic,
            reference_time=reference_time,
            touch_hints=touch_hints,
            max_hits=max_hits,
        )
    except fusion.MemoryRetrievalHybridFusionError:
        _raise("hybrid_source_fusion_invalid")
    except Exception:
        _raise("hybrid_source_fusion_invalid")

    return HybridSourceResultV1(
        contract_version=HYBRID_SOURCE_CONTRACT_VERSION,
        source_atomic_count=snapshot.count,
        bm25_generation=bm25_generation,
        vector_generation=vector_generation,
        fusion_result=result,
    )
