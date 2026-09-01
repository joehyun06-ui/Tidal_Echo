"""Authoritative-snapshot to disposable BM25 index rebuild for Phase 4D-C1."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from backend import (
    memory_hierarchy_baseline,
    memory_hierarchy_snapshot,
    memory_retrieval_bm25 as bm25,
    memory_retrieval_bm25_store as bm25_store,
)


BM25_REBUILD_CONTRACT_VERSION: Final = "memory-retrieval-bm25-rebuild-v1"

_ERROR_CATEGORIES: Final = frozenset({
    "bm25_rebuild_configuration_invalid",
    "bm25_rebuild_failed",
    "bm25_rebuild_source_invalid",
})


class MemoryRetrievalBM25RebuildError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "bm25_rebuild_failed"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "bm25_rebuild_failed"

    def __repr__(self) -> str:
        return f"MemoryRetrievalBM25RebuildError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryRetrievalBM25RebuildError(category)


@dataclass(frozen=True, slots=True, repr=False)
class BM25RebuildReceiptV1:
    contract_version: str
    generation: int
    source_atomic_count: int
    indexed_document_count: int
    unique_term_count: int
    posting_count: int
    total_document_length: int

    def __repr__(self) -> str:
        return (
            "<BM25RebuildReceiptV1 "
            f"generation={self.generation} source_atomics={self.source_atomic_count} "
            f"documents={self.indexed_document_count} terms={self.unique_term_count} "
            f"postings={self.posting_count}>"
        )


def rebuild_bm25_index_v1(
    reader: object,
    index_path: object,
    *,
    term_key_id: object,
    term_hmac_secret: object,
) -> BM25RebuildReceiptV1:
    """Rebuild one complete normal/global-user BM25 index from Memory truth."""

    if type(reader) is not memory_hierarchy_snapshot.MemoryHierarchySnapshotReader:
        _raise("bm25_rebuild_configuration_invalid")
    try:
        authority_path = Path(reader._database_path).resolve(strict=False)
        sidecar_path = Path(index_path).resolve(strict=False)
    except (AttributeError, OSError, TypeError, ValueError):
        _raise("bm25_rebuild_configuration_invalid")
    if authority_path == sidecar_path:
        _raise("bm25_rebuild_configuration_invalid")

    try:
        bm25._validate_term_key(term_key_id, term_hmac_secret)
    except bm25.MemoryRetrievalBM25Error:
        _raise("bm25_rebuild_configuration_invalid")

    try:
        snapshot = reader.load_active_snapshot()
        hierarchy_plan = memory_hierarchy_baseline.build_baseline_hierarchy_plan_v1(
            snapshot.atomics
        )
    except (
        memory_hierarchy_snapshot.MemoryHierarchySnapshotError,
        memory_hierarchy_baseline.MemoryHierarchyBaselineError,
    ):
        _raise("bm25_rebuild_source_invalid")
    except Exception:
        _raise("bm25_rebuild_source_invalid")

    try:
        plan = bm25.build_bm25_index_v1(
            snapshot.atomics,
            source_snapshot_digest=hierarchy_plan.atomic_snapshot_digest,
            term_key_id=term_key_id,
            term_hmac_secret=term_hmac_secret,
        )
        bm25_store.initialize_bm25_store(
            sidecar_path,
            forbidden_paths=(authority_path,),
        )
        stored = bm25_store.apply_bm25_index_plan(sidecar_path, plan)
        if stored.plan != plan:
            _raise("bm25_rebuild_failed")
    except MemoryRetrievalBM25RebuildError:
        raise
    except (
        bm25.MemoryRetrievalBM25Error,
        bm25_store.MemoryRetrievalBM25StoreError,
    ):
        _raise("bm25_rebuild_failed")
    except Exception:
        _raise("bm25_rebuild_failed")

    return BM25RebuildReceiptV1(
        contract_version=BM25_REBUILD_CONTRACT_VERSION,
        generation=stored.generation,
        source_atomic_count=snapshot.count,
        indexed_document_count=plan.document_count,
        unique_term_count=plan.unique_term_count,
        posting_count=plan.posting_count,
        total_document_length=plan.total_document_length,
    )
