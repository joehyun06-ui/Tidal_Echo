"""Deterministic zero-inference baseline grouping for Phase 4D-B3.

The baseline uses only authoritative Atomic Memory ``kind`` and ``memory_key``.
It never inspects content and never invents Episode membership.  The purpose is
to guarantee a complete rebuildable hierarchy before any future semantic/entity
grouper is allowed to refine it.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from backend import memory_hierarchy_projection as hierarchy


BASELINE_GROUPING_CONTRACT_VERSION: Final = "memory-hierarchy-baseline-v1"

TOPIC_BY_KIND: Final = MappingProxyType({
    "project": "topic.project",
    "decision": "topic.project",
    "task_or_progress": "topic.project",
    "user_profile": "topic.user",
    "user_preference": "topic.user",
    "relationship": "topic.relationship",
    "shared_episode": "topic.relationship",
    "assistant_experience": "topic.assistant",
})

_ERROR_CATEGORIES: Final = frozenset({
    "invalid_atomics",
    "unmapped_atomic_kind",
    "memory_hierarchy_baseline_error",
})


class MemoryHierarchyBaselineError(ValueError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_hierarchy_baseline_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "memory_hierarchy_baseline_error"

    def __repr__(self) -> str:
        return f"MemoryHierarchyBaselineError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryHierarchyBaselineError(category)


def group_baseline_topics_v1(
    atomics: object,
) -> tuple[hierarchy.TopicGroupingV1, ...]:
    """Assign every validated atomic to exactly one stable broad Topic."""

    try:
        validated, _ = hierarchy._validate_atomics(atomics)
    except hierarchy.MemoryHierarchyProjectionError:
        _raise("invalid_atomics")
    groups: dict[str, list[str]] = {}
    for atomic in validated:
        topic_key = TOPIC_BY_KIND.get(atomic.kind)
        if topic_key is None:
            _raise("unmapped_atomic_kind")
        groups.setdefault(topic_key, []).append(atomic.memory_key)
    return tuple(
        hierarchy.TopicGroupingV1(
            topic_key,
            tuple(sorted(memory_keys)),
        )
        for topic_key, memory_keys in sorted(groups.items())
    )


def build_baseline_hierarchy_plan_v1(
    atomics: object,
    *,
    previous_nodes: object = (),
) -> hierarchy.HierarchyProjectionPlanV1:
    """Build complete Topic + Canonical-State projection, with no Episodes."""

    topics = group_baseline_topics_v1(atomics)
    return hierarchy.plan_hierarchy_projection_v1(
        atomics,
        topics,
        (),
        previous_nodes=previous_nodes,
    )
