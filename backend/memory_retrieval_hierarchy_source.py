"""Current-revision BM25 + hierarchy routing composition for Phase 4D-C2.

One call binds three disposable/read-only views to the same authoritative active
Atomic snapshot revision: the current hierarchy sidecar, the C1 BM25 sidecar,
and the query result produced from that BM25 sidecar.  It then delegates to the
pure C2 router.  Stale or forged hierarchy structure is rejected before routing.

The BM25 sidecar is also rebuilt in memory from the current authoritative
snapshot and the supplied term secret, then compared exactly with the stored
plan.  This turns a same-key-id/wrong-secret mistake into a fixed failure instead
of a silent zero-hit query.

No Atomic content, hierarchy summary text, or provider-visible Memory context is
returned by this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from backend import (
    memory_hierarchy_baseline,
    memory_hierarchy_projection as hierarchy,
    memory_hierarchy_projection_store as hierarchy_store,
    memory_hierarchy_snapshot,
    memory_hierarchy_summary,
    memory_retrieval_bm25 as bm25,
    memory_retrieval_bm25_store as bm25_store,
    memory_retrieval_hierarchy_routing as routing,
)


HIERARCHY_SOURCE_CONTRACT_VERSION: Final = "memory-retrieval-hierarchy-source-v1"

_ERROR_CATEGORIES: Final = frozenset({
    "hierarchy_source_configuration_invalid",
    "hierarchy_source_index_invalid",
    "hierarchy_source_projection_invalid",
    "hierarchy_source_stale",
    "hierarchy_source_unavailable",
})


class MemoryRetrievalHierarchySourceError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "hierarchy_source_unavailable"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "hierarchy_source_unavailable"

    def __repr__(self) -> str:
        return f"MemoryRetrievalHierarchySourceError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryRetrievalHierarchySourceError(category)


@dataclass(frozen=True, slots=True, repr=False)
class HierarchyRoutingSourceResultV1:
    contract_version: str
    hierarchy_generation: int
    bm25_generation: int
    source_atomic_count: int
    routing_result: routing.HierarchyRoutingResultV1 = field(repr=False)

    def __repr__(self) -> str:
        return (
            "<HierarchyRoutingSourceResultV1 "
            f"hierarchy_generation={self.hierarchy_generation} "
            f"bm25_generation={self.bm25_generation} "
            f"source_atomics={self.source_atomic_count} "
            f"routed={self.routing_result.routed_count}>"
        )


def _validated_paths(
    reader: memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
    hierarchy_path: object,
    bm25_path: object,
) -> tuple[Path, Path, Path]:
    try:
        authority = Path(reader._database_path).resolve(strict=False)
        hierarchy_sidecar = Path(hierarchy_path).resolve(strict=False)
        bm25_sidecar = Path(bm25_path).resolve(strict=False)
    except (AttributeError, OSError, TypeError, ValueError):
        _raise("hierarchy_source_configuration_invalid")
    if len({authority, hierarchy_sidecar, bm25_sidecar}) != 3:
        _raise("hierarchy_source_configuration_invalid")
    return authority, hierarchy_sidecar, bm25_sidecar


def _plan_from_hierarchy_snapshot(
    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
    snapshot: object,
) -> hierarchy.HierarchyProjectionPlanV1:
    if type(snapshot) is not hierarchy_store.ProjectionStoreSnapshotV1:
        _raise("hierarchy_source_projection_invalid")
    if (
        snapshot.projection_contract_version != hierarchy.PROJECTION_CONTRACT_VERSION
        or type(snapshot.nodes) is not tuple
    ):
        _raise("hierarchy_source_projection_invalid")
    try:
        nodes = [
            hierarchy.ProjectionNodePlanV1(
                node_type=item.node_type,
                node_key=item.node_key,
                parent_key=item.parent_key,
                atomic_keys=item.atomic_keys,
                projection_digest=item.projection_digest,
                dirty=item.dirty,
            )
            for item in snapshot.nodes
        ]
        order = {"topic": 0, "episode": 1, "canonical_state": 2}
        if any(node.node_type not in order for node in nodes):
            _raise("hierarchy_source_projection_invalid")
        nodes.sort(
            key=lambda node: (
                node.parent_key,
                order[node.node_type],
                node.node_key,
            )
        )
        plan = hierarchy.HierarchyProjectionPlanV1(
            contract_version=hierarchy.PROJECTION_CONTRACT_VERSION,
            atomic_snapshot_digest=snapshot.atomic_snapshot_digest,
            nodes=tuple(nodes),
            obsolete_node_keys=(),
        )
        return memory_hierarchy_summary._reprove_plan(atomics, plan)
    except MemoryRetrievalHierarchySourceError:
        raise
    except memory_hierarchy_summary.MemoryHierarchySummaryError:
        _raise("hierarchy_source_projection_invalid")
    except Exception:
        _raise("hierarchy_source_projection_invalid")


def route_current_hierarchy_candidates_v1(
    reader: object,
    hierarchy_sidecar_path: object,
    bm25_sidecar_path: object,
    query_text: object,
    *,
    term_key_id: object,
    term_hmac_secret: object,
    max_bm25_hits: object = bm25.MAX_HITS,
) -> HierarchyRoutingSourceResultV1:
    """Search and route only when authority, BM25, and hierarchy share one revision."""

    if type(reader) is not memory_hierarchy_snapshot.MemoryHierarchySnapshotReader:
        _raise("hierarchy_source_configuration_invalid")
    _validated_paths(reader, hierarchy_sidecar_path, bm25_sidecar_path)

    try:
        authority_snapshot = reader.load_active_snapshot()
        baseline_plan = memory_hierarchy_baseline.build_baseline_hierarchy_plan_v1(
            authority_snapshot.atomics
        )
        current_digest = baseline_plan.atomic_snapshot_digest
    except (
        memory_hierarchy_snapshot.MemoryHierarchySnapshotError,
        memory_hierarchy_baseline.MemoryHierarchyBaselineError,
    ):
        _raise("hierarchy_source_unavailable")
    except Exception:
        _raise("hierarchy_source_unavailable")

    try:
        hierarchy_snapshot = hierarchy_store.load_projection_snapshot(
            hierarchy_sidecar_path
        )
        if hierarchy_snapshot.atomic_snapshot_digest != current_digest:
            _raise("hierarchy_source_stale")
        hierarchy_plan = _plan_from_hierarchy_snapshot(
            authority_snapshot.atomics,
            hierarchy_snapshot,
        )
        if hierarchy_plan.atomic_snapshot_digest != current_digest:
            _raise("hierarchy_source_stale")
    except MemoryRetrievalHierarchySourceError:
        raise
    except hierarchy_store.MemoryHierarchyProjectionStoreError:
        _raise("hierarchy_source_projection_invalid")
    except Exception:
        _raise("hierarchy_source_projection_invalid")

    try:
        index_snapshot = bm25_store.load_bm25_store_snapshot(bm25_sidecar_path)
        if index_snapshot.plan.source_snapshot_digest != current_digest:
            _raise("hierarchy_source_stale")
        expected_index = bm25.build_bm25_index_v1(
            authority_snapshot.atomics,
            source_snapshot_digest=current_digest,
            term_key_id=term_key_id,
            term_hmac_secret=term_hmac_secret,
        )
        if index_snapshot.plan != expected_index:
            _raise("hierarchy_source_index_invalid")
        lexical = bm25_store.search_bm25_store(
            bm25_sidecar_path,
            query_text,
            term_key_id=term_key_id,
            term_hmac_secret=term_hmac_secret,
            expected_source_snapshot_digest=current_digest,
            max_hits=max_bm25_hits,
        )
    except MemoryRetrievalHierarchySourceError:
        raise
    except (
        bm25.MemoryRetrievalBM25Error,
        bm25_store.MemoryRetrievalBM25StoreError,
    ):
        _raise("hierarchy_source_index_invalid")
    except Exception:
        _raise("hierarchy_source_index_invalid")

    try:
        routed = routing.route_hierarchy_candidates_v1(
            authority_snapshot.atomics,
            hierarchy_plan,
            lexical,
        )
    except routing.MemoryRetrievalHierarchyRoutingError as error:
        if error.category == "invalid_bm25_result":
            _raise("hierarchy_source_index_invalid")
        _raise("hierarchy_source_projection_invalid")
    except Exception:
        _raise("hierarchy_source_unavailable")

    return HierarchyRoutingSourceResultV1(
        contract_version=HIERARCHY_SOURCE_CONTRACT_VERSION,
        hierarchy_generation=hierarchy_snapshot.generation,
        bm25_generation=index_snapshot.generation,
        source_atomic_count=authority_snapshot.count,
        routing_result=routed,
    )
