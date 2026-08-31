"""Evidence-backed Episode refinement for Phase 4D-B5.

Episodes are optional derived structure under an already-proved Topic partition.
Callers/models may propose only sets of existing Atomic Memory keys.  The server
accepts a group only when all members are event-capable, belong to the same
Topic, and were first observed within one bounded co-observation window.

The observation window is deliberately not treated as the real-world event time;
it is only a conservative consistency signal.  Episode keys are server-derived
from parent Topic + sorted membership.  No Episode title, summary, state text,
entity metadata, confidence, or new fact can enter this contract.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from backend import (
    memory_hierarchy_baseline as baseline,
    memory_hierarchy_projection as hierarchy,
)


EPISODE_REFINEMENT_CONTRACT_VERSION: Final = "memory-hierarchy-episode-refinement-v1"
EVENT_CAPABLE_KINDS: Final = frozenset({
    "shared_episode",
    "decision",
    "task_or_progress",
})
MAX_EPISODE_MEMBERS: Final = 16
MAX_OBSERVATION_SPAN_SECONDS: Final = 7 * 24 * 60 * 60
EPISODE_KEY_DIGEST_CHARS: Final = 24

_ERROR_CATEGORIES: Final = frozenset({
    "cross_topic_episode",
    "duplicate_episode_group",
    "duplicate_episode_membership",
    "episode_observation_window_exceeded",
    "invalid_atomics",
    "invalid_episode_proposal",
    "invalid_episode_proposals",
    "invalid_topics",
    "memory_hierarchy_episode_refinement_error",
    "non_event_atomic",
    "too_many_episode_groups",
    "too_many_episode_members",
    "unknown_atomic_key",
})


class MemoryHierarchyEpisodeRefinementError(ValueError):
    """Stable, data-free Episode-refinement failure."""

    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_hierarchy_episode_refinement_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "memory_hierarchy_episode_refinement_error"

    def __repr__(self) -> str:
        return f"MemoryHierarchyEpisodeRefinementError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryHierarchyEpisodeRefinementError(category)


@dataclass(frozen=True, slots=True, repr=False)
class EpisodeMembershipProposalV1:
    """One unlabeled event cluster expressed only as Atomic Memory keys."""

    atomic_keys: tuple[str, ...] = field(repr=False)

    def __repr__(self) -> str:
        return f"<EpisodeMembershipProposalV1 members={len(self.atomic_keys)}>"


@dataclass(frozen=True, slots=True, repr=False)
class EpisodeRefinementResultV1:
    contract_version: str
    episodes: tuple[hierarchy.EpisodeGroupingV1, ...] = field(repr=False)
    applied: bool

    def __repr__(self) -> str:
        return (
            "<EpisodeRefinementResultV1 "
            f"episodes={len(self.episodes)} applied={self.applied!r}>"
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


def _validated_topics(
    topics: object,
    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
) -> tuple[
    tuple[hierarchy.TopicGroupingV1, ...],
    dict[str, str],
]:
    try:
        validated = hierarchy._validate_topics(
            topics,
            frozenset(item.memory_key for item in atomics),
        )
    except hierarchy.MemoryHierarchyProjectionError:
        _raise("invalid_topics")
    atomic_by_key = {item.memory_key: item for item in atomics}
    owner: dict[str, str] = {}
    for topic in validated:
        broad_domains = {
            baseline.TOPIC_BY_KIND.get(atomic_by_key[memory_key].kind)
            for memory_key in topic.atomic_keys
        }
        if None in broad_domains or len(broad_domains) != 1:
            _raise("invalid_topics")
        for memory_key in topic.atomic_keys:
            owner[memory_key] = topic.topic_key
    return validated, owner


def _first_observed(value: object) -> datetime:
    if type(value) is not str or not value or len(value) > 128:
        _raise("invalid_atomics")
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError, OverflowError):
        _raise("invalid_atomics")
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        _raise("invalid_atomics")
    return parsed


def _episode_key(topic_key: str, members: tuple[str, ...]) -> str:
    payload = (topic_key + "\x1e" + "\x1f".join(members)).encode("ascii")
    digest = hashlib.sha256(payload).hexdigest()[:EPISODE_KEY_DIGEST_CHARS]
    key = f"episode.{digest}"
    if len(key) > hierarchy.MAX_NODE_KEY_CHARS:
        _raise("invalid_episode_proposal")
    return key


def refine_episodes_v1(
    atomics: object,
    topics: object,
    proposals: object,
) -> EpisodeRefinementResultV1:
    """Prove optional event clusters under an existing complete Topic partition."""

    validated_atomics, atomics_by_key = _validated_atomics(atomics)
    validated_topics, topic_by_atomic = _validated_topics(topics, validated_atomics)
    if type(proposals) not in (list, tuple):
        _raise("invalid_episode_proposals")
    if len(proposals) > hierarchy.MAX_EPISODES:
        _raise("too_many_episode_groups")
    if not proposals:
        return EpisodeRefinementResultV1(
            contract_version=EPISODE_REFINEMENT_CONTRACT_VERSION,
            episodes=(),
            applied=False,
        )

    seen_members: set[str] = set()
    seen_groups: set[tuple[str, ...]] = set()
    seen_episode_keys: set[str] = set()
    episodes: list[hierarchy.EpisodeGroupingV1] = []

    for raw in proposals:
        if type(raw) is not EpisodeMembershipProposalV1 or type(raw.atomic_keys) is not tuple:
            _raise("invalid_episode_proposal")
        if len(raw.atomic_keys) < 2:
            _raise("invalid_episode_proposal")
        if len(raw.atomic_keys) > MAX_EPISODE_MEMBERS:
            _raise("too_many_episode_members")
        members = tuple(sorted(raw.atomic_keys))
        if len(set(members)) != len(members):
            _raise("duplicate_episode_membership")
        if members in seen_groups:
            _raise("duplicate_episode_group")
        seen_groups.add(members)

        parent_topics: set[str] = set()
        observed: list[datetime] = []
        for memory_key in members:
            atomic = atomics_by_key.get(memory_key)
            if atomic is None:
                _raise("unknown_atomic_key")
            if memory_key in seen_members:
                _raise("duplicate_episode_membership")
            if atomic.kind not in EVENT_CAPABLE_KINDS:
                _raise("non_event_atomic")
            parent = topic_by_atomic.get(memory_key)
            if parent is None:
                _raise("invalid_topics")
            parent_topics.add(parent)
            observed.append(_first_observed(atomic.first_observed_at))
        if len(parent_topics) != 1:
            _raise("cross_topic_episode")

        earliest = min(observed)
        latest = max(observed)
        if (latest - earliest).total_seconds() > MAX_OBSERVATION_SPAN_SECONDS:
            _raise("episode_observation_window_exceeded")

        topic_key = next(iter(parent_topics))
        episode_key = _episode_key(topic_key, members)
        if episode_key in seen_episode_keys:
            _raise("duplicate_episode_group")
        seen_episode_keys.add(episode_key)
        seen_members.update(members)
        episodes.append(
            hierarchy.EpisodeGroupingV1(
                episode_key=episode_key,
                topic_key=topic_key,
                atomic_keys=members,
            )
        )

    episode_tuple = tuple(sorted(episodes, key=lambda item: (item.topic_key, item.episode_key)))
    try:
        hierarchy.plan_hierarchy_projection_v1(
            validated_atomics,
            validated_topics,
            episode_tuple,
        )
    except hierarchy.MemoryHierarchyProjectionError:
        _raise("invalid_episode_proposals")

    return EpisodeRefinementResultV1(
        contract_version=EPISODE_REFINEMENT_CONTRACT_VERSION,
        episodes=episode_tuple,
        applied=bool(episode_tuple),
    )


def build_hierarchy_plan_with_episodes_v1(
    atomics: object,
    topics: object,
    proposals: object,
    *,
    previous_nodes: object = (),
) -> hierarchy.HierarchyProjectionPlanV1:
    """Build one complete Topic/State hierarchy plus proved optional Episodes."""

    refined = refine_episodes_v1(atomics, topics, proposals)
    return hierarchy.plan_hierarchy_projection_v1(
        atomics,
        topics,
        refined.episodes,
        previous_nodes=previous_nodes,
    )
