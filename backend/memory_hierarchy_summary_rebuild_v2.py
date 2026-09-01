"""Unwired v2 summary-cache rebuild composition for Phase 4D-B6D.

One invocation proves the complete authoritative active Atomic snapshot, loads
and re-proves the current content-free hierarchy (including B4/B5 guards), then
materializes only missing/stale Topic, Episode, and Canonical-State summaries in
the separate v2 derived-text cache.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from backend import (
    memory_hierarchy_projection as hierarchy,
    memory_hierarchy_projection_store as hierarchy_store,
    memory_hierarchy_snapshot,
    memory_hierarchy_summary as summary,
    memory_hierarchy_summary_extractor_v2 as summary_extractor,
    memory_hierarchy_summary_store_v2 as summary_store,
)


SUMMARY_REBUILD_CONTRACT_VERSION: Final = "memory-hierarchy-summary-rebuild-v2"

_ERROR_CATEGORIES: Final = frozenset({
    "hierarchy_summary_cache_invalid",
    "hierarchy_summary_projection_invalid",
    "hierarchy_summary_rebuild_configuration_invalid",
    "hierarchy_summary_rebuild_failed",
    "hierarchy_summary_source_invalid",
})


class MemoryHierarchySummaryRebuildV2Error(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "hierarchy_summary_rebuild_failed"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "hierarchy_summary_rebuild_failed"

    def __repr__(self) -> str:
        return f"MemoryHierarchySummaryRebuildV2Error({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryHierarchySummaryRebuildV2Error(category)


@dataclass(frozen=True, slots=True, repr=False)
class HierarchySummaryRebuildReceiptV2:
    contract_version: str
    status: str
    target_count: int
    cache_hit_count: int
    generated_count: int
    failed_count: int
    pruned_count: int
    provider_call_count: int

    def __repr__(self) -> str:
        return (
            "<HierarchySummaryRebuildReceiptV2 "
            f"status={self.status!r} targets={self.target_count} "
            f"hits={self.cache_hit_count} generated={self.generated_count} "
            f"failed={self.failed_count} pruned={self.pruned_count} "
            f"provider_calls={self.provider_call_count}>"
        )


def _validated_paths(
    reader: memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
    hierarchy_sidecar_path: object,
    summary_store_path: object,
) -> tuple[Path, Path, Path]:
    try:
        authority = Path(reader._database_path).resolve(strict=False)
        hierarchy_path = Path(hierarchy_sidecar_path).resolve(strict=False)
        summary_path = Path(summary_store_path).resolve(strict=False)
    except (AttributeError, OSError, TypeError, ValueError):
        _raise("hierarchy_summary_rebuild_configuration_invalid")
    if len({authority, hierarchy_path, summary_path}) != 3:
        _raise("hierarchy_summary_rebuild_configuration_invalid")
    return authority, hierarchy_path, summary_path


def _plan_from_sidecar_snapshot(
    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
    snapshot: object,
) -> hierarchy.HierarchyProjectionPlanV1:
    if type(snapshot) is not hierarchy_store.ProjectionStoreSnapshotV1:
        _raise("hierarchy_summary_projection_invalid")
    if (
        snapshot.projection_contract_version != hierarchy.PROJECTION_CONTRACT_VERSION
        or type(snapshot.nodes) is not tuple
    ):
        _raise("hierarchy_summary_projection_invalid")
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
            _raise("hierarchy_summary_projection_invalid")
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
        summary._reprove_plan(atomics, plan)
        return plan
    except MemoryHierarchySummaryRebuildV2Error:
        raise
    except summary.MemoryHierarchySummaryError:
        _raise("hierarchy_summary_projection_invalid")
    except Exception:
        _raise("hierarchy_summary_projection_invalid")


def _summary_targets(
    plan: hierarchy.HierarchyProjectionPlanV1,
) -> tuple[hierarchy.ProjectionNodePlanV1, ...]:
    targets = tuple(
        node
        for node in plan.nodes
        if node.node_type in {"topic", "episode", "canonical_state"}
    )
    if any(not node.atomic_keys for node in targets):
        _raise("hierarchy_summary_projection_invalid")
    return targets


async def rebuild_current_hierarchy_summaries_v2(
    reader: object,
    hierarchy_sidecar_path: object,
    summary_store_path: object,
    generation_callable,
    *,
    provider_model: object,
    provider_prompt_contract_version: object,
) -> HierarchySummaryRebuildReceiptV2:
    """Refresh only current cache misses/stale Topic/Episode/State revisions."""

    if type(reader) is not memory_hierarchy_snapshot.MemoryHierarchySnapshotReader:
        _raise("hierarchy_summary_rebuild_configuration_invalid")
    if not callable(generation_callable):
        _raise("hierarchy_summary_rebuild_configuration_invalid")
    authority_path, hierarchy_path, cache_path = _validated_paths(
        reader,
        hierarchy_sidecar_path,
        summary_store_path,
    )

    try:
        authority_snapshot = reader.load_active_snapshot()
    except memory_hierarchy_snapshot.MemoryHierarchySnapshotError:
        _raise("hierarchy_summary_source_invalid")
    except Exception:
        _raise("hierarchy_summary_source_invalid")

    try:
        sidecar_snapshot = hierarchy_store.load_projection_snapshot(hierarchy_path)
        plan = _plan_from_sidecar_snapshot(
            authority_snapshot.atomics,
            sidecar_snapshot,
        )
    except MemoryHierarchySummaryRebuildV2Error:
        raise
    except hierarchy_store.MemoryHierarchyProjectionStoreError:
        _raise("hierarchy_summary_projection_invalid")
    except Exception:
        _raise("hierarchy_summary_projection_invalid")

    targets = _summary_targets(plan)
    try:
        summary_store.initialize_summary_store(
            cache_path,
            forbidden_paths=(authority_path, hierarchy_path),
        )
        pruned = summary_store.prune_stale_summaries(cache_path, targets)
    except summary_store.MemoryHierarchySummaryStoreError:
        _raise("hierarchy_summary_cache_invalid")
    except Exception:
        _raise("hierarchy_summary_cache_invalid")

    cache_hits = 0
    generated = 0
    failed = 0
    provider_calls = 0

    async def counted_generation(*args, **kwargs):
        nonlocal provider_calls
        provider_calls += 1
        return await generation_callable(*args, **kwargs)

    for node in targets:
        try:
            cached = summary_store.load_current_summary(cache_path, node)
        except summary_store.MemoryHierarchySummaryStoreError:
            _raise("hierarchy_summary_cache_invalid")
        if cached is not None:
            cache_hits += 1
            continue

        try:
            derived = await summary_extractor.extract_node_summary_v2(
                counted_generation,
                authority_snapshot.atomics,
                plan,
                node.node_key,
                provider_model=provider_model,
                provider_prompt_contract_version=provider_prompt_contract_version,
            )
        except asyncio.CancelledError:
            raise
        except summary_extractor.MemoryHierarchySummaryExtractorV2Error:
            failed += 1
            continue
        except Exception:
            failed += 1
            continue

        try:
            summary_store.store_summary(cache_path, derived, node)
        except summary_store.MemoryHierarchySummaryStoreError:
            _raise("hierarchy_summary_cache_invalid")
        except Exception:
            _raise("hierarchy_summary_cache_invalid")
        generated += 1

    return HierarchySummaryRebuildReceiptV2(
        contract_version=SUMMARY_REBUILD_CONTRACT_VERSION,
        status=("completed" if failed == 0 else "completed_with_failures"),
        target_count=len(targets),
        cache_hit_count=cache_hits,
        generated_count=generated,
        failed_count=failed,
        pruned_count=pruned.removed_count,
        provider_call_count=provider_calls,
    )
