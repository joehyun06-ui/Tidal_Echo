"""Unwired baseline hierarchy rebuild composition for Phase 4D-B3.

One invocation reads a complete authoritative active snapshot through the
mode=ro snapshot reader and materializes the deterministic baseline hierarchy in
the disposable sidecar.  The authoritative relay database is never opened for
write by this composition.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend import (
    memory_hierarchy_baseline,
    memory_hierarchy_projection_store,
    memory_hierarchy_snapshot,
)


_ERROR_CATEGORIES = frozenset({
    "hierarchy_rebuild_configuration_invalid",
    "hierarchy_rebuild_failed",
})


class MemoryHierarchyRebuildError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "hierarchy_rebuild_failed"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "hierarchy_rebuild_failed"

    def __repr__(self) -> str:
        return f"MemoryHierarchyRebuildError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryHierarchyRebuildError(category)


@dataclass(frozen=True, slots=True, repr=False)
class HierarchyBaselineRebuildReceiptV1:
    generation: int
    atomic_count: int
    topic_count: int
    node_count: int
    dirty_node_count: int

    def __repr__(self) -> str:
        return (
            "<HierarchyBaselineRebuildReceiptV1 "
            f"generation={self.generation} atomics={self.atomic_count} "
            f"topics={self.topic_count} nodes={self.node_count} "
            f"dirty={self.dirty_node_count}>"
        )


def rebuild_baseline_hierarchy_v1(
    reader: object,
    sidecar_path: object,
) -> HierarchyBaselineRebuildReceiptV1:
    """Rebuild one deterministic baseline hierarchy without Memory authority."""

    if type(reader) is not memory_hierarchy_snapshot.MemoryHierarchySnapshotReader:
        _raise("hierarchy_rebuild_configuration_invalid")
    try:
        authority = Path(reader._database_path).resolve()
        sidecar = Path(sidecar_path).resolve()
        if authority == sidecar:
            _raise("hierarchy_rebuild_configuration_invalid")
    except MemoryHierarchyRebuildError:
        raise
    except (OSError, TypeError, ValueError):
        _raise("hierarchy_rebuild_configuration_invalid")

    try:
        # Authority is proved first. A failed source snapshot must not create or
        # advance any projection sidecar state.
        snapshot = reader.load_active_snapshot()
        memory_hierarchy_projection_store.initialize_projection_store(sidecar)
        previous = memory_hierarchy_projection_store.load_projection_receipts(
            sidecar
        )
        plan = memory_hierarchy_baseline.build_baseline_hierarchy_plan_v1(
            snapshot.atomics,
            previous_nodes=previous,
        )
        stored = memory_hierarchy_projection_store.apply_projection_plan(
            sidecar,
            plan,
        )
        if (
            stored.atomic_snapshot_digest != plan.atomic_snapshot_digest
            or stored.receipts() != plan.receipts()
        ):
            _raise("hierarchy_rebuild_failed")
        topic_count = sum(1 for node in plan.nodes if node.node_type == "topic")
        return HierarchyBaselineRebuildReceiptV1(
            generation=stored.generation,
            atomic_count=snapshot.count,
            topic_count=topic_count,
            node_count=len(stored.nodes),
            dirty_node_count=len(stored.dirty_node_keys),
        )
    except MemoryHierarchyRebuildError:
        raise
    except (
        memory_hierarchy_snapshot.MemoryHierarchySnapshotError,
        memory_hierarchy_baseline.MemoryHierarchyBaselineError,
        memory_hierarchy_projection_store.MemoryHierarchyProjectionStoreError,
    ) as error:
        raise MemoryHierarchyRebuildError("hierarchy_rebuild_failed") from error
    except Exception:
        _raise("hierarchy_rebuild_failed")
