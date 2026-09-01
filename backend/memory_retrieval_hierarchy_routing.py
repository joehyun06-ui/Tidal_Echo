"""Hierarchy-only Atomic candidate expansion for Phase 4D-C2.

This module never retrieves or emits hierarchy summary text.  It accepts a
current, fully re-proved hierarchy plus a bounded C1 BM25 result and expands
lexical seed Memory keys to normal global-user Atomic siblings in the same
Episode first, then the same Topic.  The output contains only Atomic Memory keys
and structural routing metadata; Atomic content remains authoritative elsewhere.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Final

from backend import memory_hierarchy_projection as hierarchy
from backend import memory_hierarchy_summary as hierarchy_summary
from backend import memory_retrieval_bm25 as bm25


HIERARCHY_ROUTING_CONTRACT_VERSION: Final = "memory-retrieval-hierarchy-routing-v1"
MAX_BM25_SEEDS: Final = bm25.MAX_HITS
MAX_ROUTED_ATOMICS: Final = 64
_ROUTE_KINDS: Final = frozenset({
    "bm25_seed",
    "episode_neighbor",
    "topic_neighbor",
})
_MEMORY_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")

_ERROR_CATEGORIES: Final = frozenset({
    "invalid_atomics",
    "invalid_bm25_result",
    "invalid_hierarchy",
    "memory_retrieval_hierarchy_routing_error",
})


class MemoryRetrievalHierarchyRoutingError(ValueError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_retrieval_hierarchy_routing_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "memory_retrieval_hierarchy_routing_error"

    def __repr__(self) -> str:
        return f"MemoryRetrievalHierarchyRoutingError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryRetrievalHierarchyRoutingError(category)


@dataclass(frozen=True, slots=True, repr=False)
class HierarchyRoutedAtomicV1:
    memory_key: str = field(repr=False)
    route_kind: str
    best_seed_rank: int
    support_seed_count: int

    def __repr__(self) -> str:
        return (
            "<HierarchyRoutedAtomicV1 "
            f"route={self.route_kind!r} best_seed_rank={self.best_seed_rank} "
            f"support_seeds={self.support_seed_count}>"
        )


@dataclass(frozen=True, slots=True, repr=False)
class HierarchyRoutingResultV1:
    contract_version: str
    items: tuple[HierarchyRoutedAtomicV1, ...] = field(repr=False)
    eligible_atomic_count: int
    seed_count: int
    episode_neighbor_count: int
    topic_neighbor_count: int
    truncated_count: int

    @property
    def routed_count(self) -> int:
        return len(self.items)

    def __repr__(self) -> str:
        return (
            "<HierarchyRoutingResultV1 "
            f"routed={self.routed_count} eligible={self.eligible_atomic_count} "
            f"seeds={self.seed_count} episode_neighbors={self.episode_neighbor_count} "
            f"topic_neighbors={self.topic_neighbor_count} truncated={self.truncated_count}>"
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


def _reproved_hierarchy(
    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
    raw_plan: object,
) -> hierarchy.HierarchyProjectionPlanV1:
    try:
        return hierarchy_summary._reprove_plan(atomics, raw_plan)
    except hierarchy_summary.MemoryHierarchySummaryError:
        _raise("invalid_hierarchy")
    except Exception:
        _raise("invalid_hierarchy")


def _validated_bm25_result(raw: object) -> bm25.BM25SearchResultV1:
    if type(raw) is not bm25.BM25SearchResultV1:
        _raise("invalid_bm25_result")
    if (
        type(raw.hits) is not tuple
        or len(raw.hits) > MAX_BM25_SEEDS
        or type(raw.query_term_count) is not int
        or isinstance(raw.query_term_count, bool)
        or raw.query_term_count < 0
        or type(raw.indexed_document_count) is not int
        or isinstance(raw.indexed_document_count, bool)
        or not 0 <= raw.indexed_document_count <= hierarchy.MAX_ATOMICS
        or (raw.hits and raw.query_term_count == 0)
    ):
        _raise("invalid_bm25_result")

    seen: set[str] = set()
    canonical: list[tuple[float, int, str]] = []
    for hit in raw.hits:
        if (
            type(hit) is not bm25.BM25SearchHitV1
            or type(hit.memory_key) is not str
            or _MEMORY_KEY_PATTERN.fullmatch(hit.memory_key) is None
            or hit.memory_key in seen
            or type(hit.score) is not float
            or not math.isfinite(hit.score)
            or hit.score <= 0.0
            or type(hit.matched_term_count) is not int
            or isinstance(hit.matched_term_count, bool)
            or not 1 <= hit.matched_term_count <= raw.query_term_count
        ):
            _raise("invalid_bm25_result")
        seen.add(hit.memory_key)
        canonical.append((hit.score, hit.matched_term_count, hit.memory_key))
    expected = sorted(canonical, key=lambda item: (-item[0], -item[1], item[2]))
    if canonical != expected:
        _raise("invalid_bm25_result")
    return raw


def _eligible_atomic(
    atomic: hierarchy.AtomicMemoryProjectionInputV1,
) -> bool:
    return (
        atomic.status == "active"
        and atomic.scope_type == "global_user"
        and atomic.scope_ref == ""
        and atomic.sensitivity == "normal"
    )


def route_hierarchy_candidates_v1(
    atomics: object,
    hierarchy_plan: object,
    bm25_result: object,
) -> HierarchyRoutingResultV1:
    """Expand current BM25 Atomic seeds through current Episode/Topic structure."""

    validated_atomics, atomics_by_key = _validated_atomics(atomics)
    plan = _reproved_hierarchy(validated_atomics, hierarchy_plan)
    lexical = _validated_bm25_result(bm25_result)

    eligible = {
        memory_key: atomic
        for memory_key, atomic in atomics_by_key.items()
        if _eligible_atomic(atomic)
    }
    if lexical.indexed_document_count > len(eligible):
        _raise("invalid_bm25_result")

    topic_by_atomic: dict[str, str] = {}
    episode_by_atomic: dict[str, str] = {}
    episode_members: dict[str, tuple[str, ...]] = {}
    for node in plan.nodes:
        if node.node_type == "topic":
            for memory_key in node.atomic_keys:
                topic_by_atomic[memory_key] = node.node_key
        elif node.node_type == "episode":
            episode_members[node.node_key] = node.atomic_keys
            for memory_key in node.atomic_keys:
                episode_by_atomic[memory_key] = node.node_key

    seed_rank: dict[str, int] = {}
    seed_score: dict[str, float] = {}
    for rank, hit in enumerate(lexical.hits, start=1):
        if hit.memory_key not in eligible or hit.memory_key not in topic_by_atomic:
            _raise("invalid_bm25_result")
        seed_rank[hit.memory_key] = rank
        seed_score[hit.memory_key] = hit.score

    routed: list[HierarchyRoutedAtomicV1] = [
        HierarchyRoutedAtomicV1(
            memory_key=hit.memory_key,
            route_kind="bm25_seed",
            best_seed_rank=seed_rank[hit.memory_key],
            support_seed_count=1,
        )
        for hit in lexical.hits
    ]
    seed_keys = set(seed_rank)

    seeds_by_topic: dict[str, list[str]] = {}
    seeds_by_episode: dict[str, list[str]] = {}
    for memory_key in seed_keys:
        topic_key = topic_by_atomic[memory_key]
        seeds_by_topic.setdefault(topic_key, []).append(memory_key)
        episode_key = episode_by_atomic.get(memory_key)
        if episode_key is not None:
            seeds_by_episode.setdefault(episode_key, []).append(memory_key)

    episode_neighbors: list[HierarchyRoutedAtomicV1] = []
    topic_neighbors: list[HierarchyRoutedAtomicV1] = []
    for memory_key in sorted(eligible):
        if memory_key in seed_keys:
            continue
        episode_key = episode_by_atomic.get(memory_key)
        episode_support = (
            seeds_by_episode.get(episode_key, []) if episode_key is not None else []
        )
        if episode_support:
            ranks = tuple(seed_rank[key] for key in episode_support)
            episode_neighbors.append(HierarchyRoutedAtomicV1(
                memory_key=memory_key,
                route_kind="episode_neighbor",
                best_seed_rank=min(ranks),
                support_seed_count=len(ranks),
            ))
            continue
        topic_key = topic_by_atomic.get(memory_key)
        topic_support = seeds_by_topic.get(topic_key, []) if topic_key is not None else []
        if topic_support:
            ranks = tuple(seed_rank[key] for key in topic_support)
            topic_neighbors.append(HierarchyRoutedAtomicV1(
                memory_key=memory_key,
                route_kind="topic_neighbor",
                best_seed_rank=min(ranks),
                support_seed_count=len(ranks),
            ))

    ordering = lambda item: (
        item.best_seed_rank,
        -item.support_seed_count,
        item.memory_key,
    )
    episode_neighbors.sort(key=ordering)
    topic_neighbors.sort(key=ordering)
    all_items = [*routed, *episode_neighbors, *topic_neighbors]
    truncated = max(0, len(all_items) - MAX_ROUTED_ATOMICS)
    selected = tuple(all_items[:MAX_ROUTED_ATOMICS])

    episode_count = sum(
        1 for item in selected if item.route_kind == "episode_neighbor"
    )
    topic_count = sum(
        1 for item in selected if item.route_kind == "topic_neighbor"
    )
    result = HierarchyRoutingResultV1(
        contract_version=HIERARCHY_ROUTING_CONTRACT_VERSION,
        items=selected,
        eligible_atomic_count=len(eligible),
        seed_count=len(lexical.hits),
        episode_neighbor_count=episode_count,
        topic_neighbor_count=topic_count,
        truncated_count=truncated,
    )
    return validate_hierarchy_routing_result_v1(result)


def validate_hierarchy_routing_result_v1(
    raw: object,
) -> HierarchyRoutingResultV1:
    if type(raw) is not HierarchyRoutingResultV1:
        _raise("invalid_hierarchy")
    if (
        raw.contract_version != HIERARCHY_ROUTING_CONTRACT_VERSION
        or type(raw.items) is not tuple
        or len(raw.items) > MAX_ROUTED_ATOMICS
        or any(
            type(value) is not int or isinstance(value, bool) or value < 0
            for value in (
                raw.eligible_atomic_count,
                raw.seed_count,
                raw.episode_neighbor_count,
                raw.topic_neighbor_count,
                raw.truncated_count,
            )
        )
        or raw.seed_count > MAX_BM25_SEEDS
        or len(raw.items) > raw.eligible_atomic_count
        or raw.seed_count > len(raw.items)
        or raw.seed_count + raw.episode_neighbor_count + raw.topic_neighbor_count
        != len(raw.items)
    ):
        _raise("invalid_hierarchy")

    seen: set[str] = set()
    phase = 0
    seed_rank_expected = 1
    for item in raw.items:
        if (
            type(item) is not HierarchyRoutedAtomicV1
            or type(item.memory_key) is not str
            or _MEMORY_KEY_PATTERN.fullmatch(item.memory_key) is None
            or item.memory_key in seen
            or item.route_kind not in _ROUTE_KINDS
            or type(item.best_seed_rank) is not int
            or isinstance(item.best_seed_rank, bool)
            or not 1 <= item.best_seed_rank <= max(1, raw.seed_count)
            or type(item.support_seed_count) is not int
            or isinstance(item.support_seed_count, bool)
            or not 1 <= item.support_seed_count <= max(1, raw.seed_count)
        ):
            _raise("invalid_hierarchy")
        seen.add(item.memory_key)
        item_phase = {
            "bm25_seed": 0,
            "episode_neighbor": 1,
            "topic_neighbor": 2,
        }[item.route_kind]
        if item_phase < phase:
            _raise("invalid_hierarchy")
        phase = item_phase
        if item.route_kind == "bm25_seed":
            if (
                item.best_seed_rank != seed_rank_expected
                or item.support_seed_count != 1
            ):
                _raise("invalid_hierarchy")
            seed_rank_expected += 1
    if seed_rank_expected - 1 != raw.seed_count:
        _raise("invalid_hierarchy")
    return raw
