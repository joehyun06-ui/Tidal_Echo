"""Content-free semantic Topic refinement contract for Phase 4D-B4.

The model/caller may propose only partitions of authoritative Atomic Memory keys.
It cannot author Topic labels, summaries, state text, entity metadata, or Memory
content.  Baseline broad domains remain server-owned and proposals may never
cross those domains.  Refined Topic keys are deterministically derived from the
baseline domain plus sorted membership, making the result rebuildable and
content-free.

An empty proposal set means "no confident refinement" and returns the baseline
Topics unchanged.  Any non-empty proposal set must be a complete, disjoint
partition of every active atomic or the refinement fails closed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Final

from backend import (
    memory_hierarchy_baseline as baseline,
    memory_hierarchy_projection as hierarchy,
)


REFINEMENT_CONTRACT_VERSION: Final = "memory-hierarchy-refinement-v1"
REFINED_KEY_DIGEST_CHARS: Final = 16

_ERROR_CATEGORIES: Final = frozenset({
    "cross_domain_group",
    "derived_key_collision",
    "duplicate_atomic_membership",
    "duplicate_topic_group",
    "empty_topic_group",
    "incomplete_topic_partition",
    "invalid_atomics",
    "invalid_topic_proposal",
    "invalid_topic_proposals",
    "memory_hierarchy_refinement_error",
    "too_many_topic_groups",
    "unknown_atomic_key",
})


class MemoryHierarchyRefinementError(ValueError):
    """Stable, data-free Topic-refinement failure."""

    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_hierarchy_refinement_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "memory_hierarchy_refinement_error"

    def __repr__(self) -> str:
        return f"MemoryHierarchyRefinementError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryHierarchyRefinementError(category)


@dataclass(frozen=True, slots=True, repr=False)
class TopicMembershipProposalV1:
    """One unlabeled semantic group expressed only as Atomic Memory keys."""

    atomic_keys: tuple[str, ...] = field(repr=False)

    def __repr__(self) -> str:
        return f"<TopicMembershipProposalV1 members={len(self.atomic_keys)}>"


@dataclass(frozen=True, slots=True, repr=False)
class TopicRefinementResultV1:
    """Server-derived Topic partition; no model-authored text survives."""

    contract_version: str
    topics: tuple[hierarchy.TopicGroupingV1, ...] = field(repr=False)
    applied: bool

    def __repr__(self) -> str:
        return (
            "<TopicRefinementResultV1 "
            f"topics={len(self.topics)} applied={self.applied!r}>"
        )


def _validated_atomics(atomics: object) -> tuple[
    tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
    dict[str, hierarchy.AtomicMemoryProjectionInputV1],
]:
    try:
        validated, _ = hierarchy._validate_atomics(atomics)
    except hierarchy.MemoryHierarchyProjectionError:
        _raise("invalid_atomics")
    return validated, {item.memory_key: item for item in validated}


def _baseline_topics(
    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
) -> tuple[hierarchy.TopicGroupingV1, ...]:
    try:
        return baseline.group_baseline_topics_v1(atomics)
    except baseline.MemoryHierarchyBaselineError:
        _raise("invalid_atomics")


def _domain_for_atomic(
    atomic: hierarchy.AtomicMemoryProjectionInputV1,
) -> str:
    topic_key = baseline.TOPIC_BY_KIND.get(atomic.kind)
    if topic_key is None:
        _raise("invalid_atomics")
    return topic_key


def _derived_topic_key(
    domain_topic_key: str,
    atomic_keys: tuple[str, ...],
) -> str:
    encoded = "\x1f".join(atomic_keys).encode("ascii")
    digest = hashlib.sha256(encoded).hexdigest()[:REFINED_KEY_DIGEST_CHARS]
    key = f"{domain_topic_key}.{digest}"
    if len(key) > hierarchy.MAX_NODE_KEY_CHARS:
        _raise("derived_key_collision")
    return key


def _canonical_proposals(
    raw_proposals: object,
    atomics_by_key: dict[str, hierarchy.AtomicMemoryProjectionInputV1],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if type(raw_proposals) not in (list, tuple):
        _raise("invalid_topic_proposals")
    if len(raw_proposals) > hierarchy.MAX_TOPICS:
        _raise("too_many_topic_groups")
    if not raw_proposals:
        return ()

    seen_members: set[str] = set()
    seen_groups: set[tuple[str, ...]] = set()
    groups: list[tuple[str, tuple[str, ...]]] = []
    for raw in raw_proposals:
        if type(raw) is not TopicMembershipProposalV1:
            _raise("invalid_topic_proposal")
        if type(raw.atomic_keys) is not tuple:
            _raise("invalid_topic_proposal")
        if not raw.atomic_keys:
            _raise("empty_topic_group")
        members = tuple(sorted(raw.atomic_keys))
        if len(set(members)) != len(members):
            _raise("duplicate_atomic_membership")
        if members in seen_groups:
            _raise("duplicate_topic_group")
        seen_groups.add(members)

        domains: set[str] = set()
        for memory_key in members:
            atomic = atomics_by_key.get(memory_key)
            if atomic is None:
                _raise("unknown_atomic_key")
            if memory_key in seen_members:
                _raise("duplicate_atomic_membership")
            seen_members.add(memory_key)
            domains.add(_domain_for_atomic(atomic))
        if len(domains) != 1:
            _raise("cross_domain_group")
        groups.append((next(iter(domains)), members))

    if seen_members != set(atomics_by_key):
        _raise("incomplete_topic_partition")
    return tuple(sorted(groups, key=lambda item: (item[0], item[1])))


def refine_topics_v1(
    atomics: object,
    proposals: object,
) -> TopicRefinementResultV1:
    """Apply one complete unlabeled partition inside server-owned domains."""

    validated, atomics_by_key = _validated_atomics(atomics)
    baseline_topics = _baseline_topics(validated)
    canonical = _canonical_proposals(proposals, atomics_by_key)
    if not canonical:
        return TopicRefinementResultV1(
            contract_version=REFINEMENT_CONTRACT_VERSION,
            topics=baseline_topics,
            applied=False,
        )

    baseline_by_domain = {
        topic.topic_key: topic.atomic_keys
        for topic in baseline_topics
    }
    groups_by_domain: dict[str, list[tuple[str, ...]]] = {}
    for domain, members in canonical:
        groups_by_domain.setdefault(domain, []).append(members)

    result: list[hierarchy.TopicGroupingV1] = []
    seen_topic_keys: set[str] = set()
    for domain, baseline_members in sorted(baseline_by_domain.items()):
        groups = sorted(groups_by_domain.get(domain, []))
        if not groups:
            _raise("incomplete_topic_partition")
        if len(groups) == 1 and groups[0] == baseline_members:
            topic_key = domain
            if topic_key in seen_topic_keys:
                _raise("derived_key_collision")
            seen_topic_keys.add(topic_key)
            result.append(hierarchy.TopicGroupingV1(topic_key, baseline_members))
            continue
        for members in groups:
            topic_key = _derived_topic_key(domain, members)
            if topic_key in seen_topic_keys:
                _raise("derived_key_collision")
            seen_topic_keys.add(topic_key)
            result.append(hierarchy.TopicGroupingV1(topic_key, members))

    result_tuple = tuple(sorted(result, key=lambda topic: topic.topic_key))
    # Reuse the hierarchy planner's exact partition constraints as a final proof.
    try:
        hierarchy.plan_hierarchy_projection_v1(validated, result_tuple, ())
    except hierarchy.MemoryHierarchyProjectionError:
        _raise("invalid_topic_proposals")
    applied = result_tuple != baseline_topics
    return TopicRefinementResultV1(
        contract_version=REFINEMENT_CONTRACT_VERSION,
        topics=result_tuple,
        applied=applied,
    )


def build_refined_hierarchy_plan_v1(
    atomics: object,
    proposals: object,
    *,
    previous_nodes: object = (),
) -> hierarchy.HierarchyProjectionPlanV1:
    """Build Topic + Canonical-State hierarchy from a proved refinement."""

    refined = refine_topics_v1(atomics, proposals)
    return hierarchy.plan_hierarchy_projection_v1(
        atomics,
        refined.topics,
        (),
        previous_nodes=previous_nodes,
    )
