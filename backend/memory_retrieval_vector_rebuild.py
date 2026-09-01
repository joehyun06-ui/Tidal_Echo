"""Authoritative-snapshot vector index rebuild for Phase 4D-C3."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from backend import (
    memory_hierarchy_baseline,
    memory_hierarchy_snapshot,
    memory_retrieval_vector as vector,
    memory_retrieval_vector_store as vector_store,
)


VECTOR_REBUILD_CONTRACT_VERSION: Final = "memory-retrieval-vector-rebuild-v1"

_ERROR_CATEGORIES: Final = frozenset({
    "vector_rebuild_configuration_invalid",
    "vector_rebuild_embedding_failed",
    "vector_rebuild_failed",
    "vector_rebuild_source_invalid",
})


class MemoryRetrievalVectorRebuildError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "vector_rebuild_failed"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "vector_rebuild_failed"

    def __repr__(self) -> str:
        return f"MemoryRetrievalVectorRebuildError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryRetrievalVectorRebuildError(category)


@dataclass(frozen=True, slots=True, repr=False)
class VectorRebuildReceiptV1:
    contract_version: str
    generation: int
    source_atomic_count: int
    indexed_document_count: int
    dimensions: int
    provider_call_count: int

    def __repr__(self) -> str:
        return (
            "<VectorRebuildReceiptV1 "
            f"generation={self.generation} source_atomics={self.source_atomic_count} "
            f"documents={self.indexed_document_count} dimensions={self.dimensions} "
            f"provider_calls={self.provider_call_count}>"
        )


async def rebuild_vector_index_v1(
    reader: object,
    index_path: object,
    embedding_callable: object,
    *,
    embedding_model: object,
    dimensions: object,
) -> VectorRebuildReceiptV1:
    """Prove source, embed eligible Atomics, then atomically materialize sidecar."""

    if type(reader) is not memory_hierarchy_snapshot.MemoryHierarchySnapshotReader:
        _raise("vector_rebuild_configuration_invalid")
    if not callable(embedding_callable):
        _raise("vector_rebuild_configuration_invalid")
    try:
        authority = Path(reader._database_path).resolve(strict=False)
        sidecar = Path(index_path).resolve(strict=False)
    except (AttributeError, OSError, TypeError, ValueError):
        _raise("vector_rebuild_configuration_invalid")
    if authority == sidecar:
        _raise("vector_rebuild_configuration_invalid")

    try:
        snapshot = reader.load_active_snapshot()
        baseline_plan = memory_hierarchy_baseline.build_baseline_hierarchy_plan_v1(
            snapshot.atomics
        )
    except (
        memory_hierarchy_snapshot.MemoryHierarchySnapshotError,
        memory_hierarchy_baseline.MemoryHierarchyBaselineError,
    ):
        _raise("vector_rebuild_source_invalid")
    except Exception:
        _raise("vector_rebuild_source_invalid")

    try:
        build = await vector.build_vector_index_v1(
            embedding_callable,
            snapshot.atomics,
            source_snapshot_digest=baseline_plan.atomic_snapshot_digest,
            embedding_model=embedding_model,
            dimensions=dimensions,
        )
    except vector.MemoryRetrievalVectorError:
        _raise("vector_rebuild_embedding_failed")
    except Exception:
        _raise("vector_rebuild_embedding_failed")

    try:
        vector_store.initialize_vector_store(
            sidecar,
            forbidden_paths=(authority,),
        )
        stored = vector_store.apply_vector_index_plan(sidecar, build.plan)
        if stored.plan != build.plan:
            _raise("vector_rebuild_failed")
    except MemoryRetrievalVectorRebuildError:
        raise
    except vector_store.MemoryRetrievalVectorStoreError:
        _raise("vector_rebuild_failed")
    except Exception:
        _raise("vector_rebuild_failed")

    return VectorRebuildReceiptV1(
        contract_version=VECTOR_REBUILD_CONTRACT_VERSION,
        generation=stored.generation,
        source_atomic_count=snapshot.count,
        indexed_document_count=build.plan.document_count,
        dimensions=build.plan.dimensions,
        provider_call_count=build.provider_call_count,
    )
