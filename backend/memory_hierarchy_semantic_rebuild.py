"""Semantic hierarchy rebuild composition for Phase 4D-B6F.

One invocation reads the authoritative active Atomic snapshot, optionally refines
server-owned broad Topics through the B4 key-only extractor, optionally derives
B5 evidence-backed Episodes, then atomically replaces the disposable content-free
hierarchy sidecar.  Provider failures fall back to current server-owned baseline
structure; Memory truth is never changed.

Sensitive or restricted Atomic plaintext is never sent to semantic providers.
Topic refinement is skipped unless every active Atomic is normal. Episode
refinement is skipped when any Atomic that would be eligible for the Episode
extractor is non-normal.  Skipping never filters a provider payload into a partial
view of the same semantic problem.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from backend import (
    memory_hierarchy_baseline as baseline,
    memory_hierarchy_episode_refinement as episode_refinement,
    memory_hierarchy_episode_refinement_extractor as episode_extractor,
    memory_hierarchy_projection as hierarchy,
    memory_hierarchy_projection_store as hierarchy_store,
    memory_hierarchy_refinement as refinement,
    memory_hierarchy_refinement_extractor as topic_extractor,
    memory_hierarchy_snapshot,
)


SEMANTIC_REBUILD_CONTRACT_VERSION: Final = "memory-hierarchy-semantic-rebuild-v1"

TOPIC_MODE_BASELINE: Final = "baseline"
TOPIC_MODE_APPLIED: Final = "applied"
TOPIC_MODE_SKIPPED_SENSITIVE: Final = "skipped_sensitive"
TOPIC_MODE_SKIPPED_BUDGET: Final = "skipped_budget"
TOPIC_MODE_PROVIDER_FAILED: Final = "provider_failed"

EPISODE_MODE_NONE: Final = "none"
EPISODE_MODE_APPLIED: Final = "applied"
EPISODE_MODE_SKIPPED_SENSITIVE: Final = "skipped_sensitive"
EPISODE_MODE_SKIPPED_BUDGET: Final = "skipped_budget"
EPISODE_MODE_PROVIDER_FAILED: Final = "provider_failed"

_PROVIDER_FAILED_MODES: Final = frozenset({
    TOPIC_MODE_PROVIDER_FAILED,
    EPISODE_MODE_PROVIDER_FAILED,
})

_ERROR_CATEGORIES: Final = frozenset({
    "semantic_rebuild_configuration_invalid",
    "semantic_rebuild_source_invalid",
    "semantic_rebuild_projection_invalid",
    "semantic_rebuild_failed",
})


class MemoryHierarchySemanticRebuildError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "semantic_rebuild_failed"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "semantic_rebuild_failed"

    def __repr__(self) -> str:
        return f"MemoryHierarchySemanticRebuildError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryHierarchySemanticRebuildError(category)


@dataclass(frozen=True, slots=True, repr=False)
class HierarchySemanticRebuildReceiptV1:
    contract_version: str
    generation: int
    atomic_snapshot_digest: str = field(repr=False)
    atomic_count: int
    topic_count: int
    episode_count: int
    node_count: int
    dirty_node_count: int
    topic_mode: str
    topic_provider_call_count: int
    episode_mode: str
    episode_provider_call_count: int

    def __repr__(self) -> str:
        return (
            "<HierarchySemanticRebuildReceiptV1 "
            f"atomics={self.atomic_count} topics={self.topic_count} "
            f"episodes={self.episode_count} nodes={self.node_count} "
            f"topic_mode={self.topic_mode!r} episode_mode={self.episode_mode!r}>"
        )

    @property
    def provider_failed(self) -> bool:
        return (
            self.topic_mode in _PROVIDER_FAILED_MODES
            or self.episode_mode in _PROVIDER_FAILED_MODES
        )


def _validated_paths(
    reader: memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
    sidecar_path: object,
) -> tuple[Path, Path]:
    try:
        authority = Path(reader._database_path).resolve(strict=False)
        sidecar = Path(sidecar_path).resolve(strict=False)
    except (AttributeError, OSError, TypeError, ValueError):
        _raise("semantic_rebuild_configuration_invalid")
    if authority == sidecar:
        _raise("semantic_rebuild_configuration_invalid")
    return authority, sidecar


def _baseline_topics(
    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
) -> tuple[hierarchy.TopicGroupingV1, ...]:
    try:
        return baseline.group_baseline_topics_v1(atomics)
    except baseline.MemoryHierarchyBaselineError:
        _raise("semantic_rebuild_source_invalid")


def _episode_eligible_atomics(
    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
    topics: tuple[hierarchy.TopicGroupingV1, ...],
) -> tuple[hierarchy.AtomicMemoryProjectionInputV1, ...]:
    owner: dict[str, str] = {}
    for topic in topics:
        for memory_key in topic.atomic_keys:
            owner[memory_key] = topic.topic_key
    counts: dict[str, int] = {}
    event_atomics: list[hierarchy.AtomicMemoryProjectionInputV1] = []
    for item in atomics:
        if item.kind not in episode_refinement.EVENT_CAPABLE_KINDS:
            continue
        topic_key = owner.get(item.memory_key)
        if topic_key is None:
            _raise("semantic_rebuild_projection_invalid")
        event_atomics.append(item)
        counts[topic_key] = counts.get(topic_key, 0) + 1
    return tuple(
        item
        for item in event_atomics
        if counts.get(owner[item.memory_key], 0) >= 2
    )


async def _topic_partition(
    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
    generation_callable,
    *,
    provider_model: object,
    provider_prompt_contract_version: object,
) -> tuple[tuple[hierarchy.TopicGroupingV1, ...], str, int]:
    broad = _baseline_topics(atomics)
    if not atomics:
        return broad, TOPIC_MODE_BASELINE, 0
    if any(item.sensitivity != "normal" for item in atomics):
        return broad, TOPIC_MODE_SKIPPED_SENSITIVE, 0
    if len(atomics) > topic_extractor.MAX_REFINEMENT_ATOMICS:
        return broad, TOPIC_MODE_SKIPPED_BUDGET, 0

    calls = 0

    async def counted_generation(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await generation_callable(*args, **kwargs)

    try:
        extraction = await topic_extractor.extract_topic_refinement_v1(
            counted_generation,
            atomics,
            provider_model=provider_model,
            provider_prompt_contract_version=provider_prompt_contract_version,
        )
        result = refinement.refine_topics_v1(atomics, extraction.proposals)
        return (
            result.topics,
            TOPIC_MODE_APPLIED if result.applied else TOPIC_MODE_BASELINE,
            calls,
        )
    except asyncio.CancelledError:
        raise
    except (
        topic_extractor.MemoryHierarchyRefinementExtractorError,
        refinement.MemoryHierarchyRefinementError,
    ):
        return broad, TOPIC_MODE_PROVIDER_FAILED, calls
    except Exception:
        return broad, TOPIC_MODE_PROVIDER_FAILED, calls


async def _episodes(
    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
    topics: tuple[hierarchy.TopicGroupingV1, ...],
    generation_callable,
    *,
    provider_model: object,
    provider_prompt_contract_version: object,
) -> tuple[tuple[hierarchy.EpisodeGroupingV1, ...], str, int]:
    eligible = _episode_eligible_atomics(atomics, topics)
    if len(eligible) < 2:
        return (), EPISODE_MODE_NONE, 0
    if any(item.sensitivity != "normal" for item in eligible):
        return (), EPISODE_MODE_SKIPPED_SENSITIVE, 0
    if len(eligible) > episode_extractor.MAX_EXTRACTOR_ATOMICS:
        return (), EPISODE_MODE_SKIPPED_BUDGET, 0

    calls = 0

    async def counted_generation(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await generation_callable(*args, **kwargs)

    try:
        extraction = await episode_extractor.extract_episode_refinement_v1(
            counted_generation,
            atomics,
            topics,
            provider_model=provider_model,
            provider_prompt_contract_version=provider_prompt_contract_version,
        )
        result = episode_refinement.refine_episodes_v1(
            atomics,
            topics,
            extraction.proposals,
        )
        return (
            result.episodes,
            EPISODE_MODE_APPLIED if result.applied else EPISODE_MODE_NONE,
            calls,
        )
    except asyncio.CancelledError:
        raise
    except (
        episode_extractor.MemoryHierarchyEpisodeRefinementExtractorError,
        episode_refinement.MemoryHierarchyEpisodeRefinementError,
    ):
        return (), EPISODE_MODE_PROVIDER_FAILED, calls
    except Exception:
        return (), EPISODE_MODE_PROVIDER_FAILED, calls


async def rebuild_semantic_hierarchy_v1(
    reader: object,
    sidecar_path: object,
    generation_callable,
    *,
    provider_model: object,
    provider_prompt_contract_version: object,
) -> HierarchySemanticRebuildReceiptV1:
    """Build current semantic Topic/Episode structure into the disposable sidecar."""

    if type(reader) is not memory_hierarchy_snapshot.MemoryHierarchySnapshotReader:
        _raise("semantic_rebuild_configuration_invalid")
    if not callable(generation_callable):
        _raise("semantic_rebuild_configuration_invalid")
    _authority_path, sidecar = _validated_paths(reader, sidecar_path)

    try:
        snapshot = reader.load_active_snapshot()
    except memory_hierarchy_snapshot.MemoryHierarchySnapshotError:
        _raise("semantic_rebuild_source_invalid")
    except Exception:
        _raise("semantic_rebuild_source_invalid")

    try:
        hierarchy_store.initialize_projection_store(sidecar)
        previous = hierarchy_store.load_projection_receipts(sidecar)
    except hierarchy_store.MemoryHierarchyProjectionStoreError:
        _raise("semantic_rebuild_projection_invalid")
    except Exception:
        _raise("semantic_rebuild_projection_invalid")

    topics, topic_mode, topic_calls = await _topic_partition(
        snapshot.atomics,
        generation_callable,
        provider_model=provider_model,
        provider_prompt_contract_version=provider_prompt_contract_version,
    )
    episodes, episode_mode, episode_calls = await _episodes(
        snapshot.atomics,
        topics,
        generation_callable,
        provider_model=provider_model,
        provider_prompt_contract_version=provider_prompt_contract_version,
    )

    try:
        plan = hierarchy.plan_hierarchy_projection_v1(
            snapshot.atomics,
            topics,
            episodes,
            previous_nodes=previous,
        )
        stored = hierarchy_store.apply_projection_plan(sidecar, plan)
        if (
            stored.atomic_snapshot_digest != plan.atomic_snapshot_digest
            or stored.receipts() != plan.receipts()
        ):
            _raise("semantic_rebuild_projection_invalid")
    except MemoryHierarchySemanticRebuildError:
        raise
    except (
        hierarchy.MemoryHierarchyProjectionError,
        hierarchy_store.MemoryHierarchyProjectionStoreError,
    ):
        _raise("semantic_rebuild_projection_invalid")
    except Exception:
        _raise("semantic_rebuild_failed")

    return HierarchySemanticRebuildReceiptV1(
        contract_version=SEMANTIC_REBUILD_CONTRACT_VERSION,
        generation=stored.generation,
        atomic_snapshot_digest=stored.atomic_snapshot_digest,
        atomic_count=snapshot.count,
        topic_count=len(topics),
        episode_count=len(episodes),
        node_count=len(stored.nodes),
        dirty_node_count=len(stored.dirty_node_keys),
        topic_mode=topic_mode,
        topic_provider_call_count=topic_calls,
        episode_mode=episode_mode,
        episode_provider_call_count=episode_calls,
    )
