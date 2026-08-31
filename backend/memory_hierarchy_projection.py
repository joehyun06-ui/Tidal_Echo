"""Pure rebuildable hierarchy projection planning for Phase 4D-B.

This module never owns Memory truth.  It accepts already-read active atomic
Memory snapshots plus opaque grouping proposals and produces only structural
Topic / Episode / Canonical-State manifests.  It performs no I/O, persistence,
provider calls, summarization, retrieval, or mutation of canonical Memory.

Projection nodes contain Memory keys and digests only; normalized atomic content
is used transiently to compute revision digests and is never copied into a node.
Any projection database built from this plan is therefore disposable and may be
reconstructed from authoritative ``memory_items`` at any time.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Final


PROJECTION_CONTRACT_VERSION: Final = "memory-hierarchy-projection-v1"
DIGEST_VERSION: Final = 1
MAX_ATOMICS: Final = 256
MAX_TOPICS: Final = 64
MAX_EPISODES: Final = 128
MAX_ATOMIC_CONTENT_CHARS: Final = 4096
MAX_SCOPE_REF_CHARS: Final = 256
MAX_NODE_KEY_CHARS: Final = 128

_KINDS: Final = frozenset({
    "user_preference",
    "user_profile",
    "relationship",
    "shared_episode",
    "project",
    "decision",
    "task_or_progress",
    "assistant_experience",
})
_SCOPE_TYPES: Final = frozenset({"global_user", "channel", "session", "project"})
_EXPLICITNESS: Final = frozenset({"explicit", "inferred"})
_SENSITIVITIES: Final = frozenset({"normal", "sensitive", "restricted"})
_NODE_TYPES: Final = frozenset({"topic", "episode", "canonical_state"})
_MEMORY_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_NODE_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")

_ERROR_CATEGORIES: Final = frozenset({
    "duplicate_atomic",
    "duplicate_episode",
    "duplicate_previous_node",
    "duplicate_topic",
    "episode_membership_conflict",
    "episode_topic_mismatch",
    "invalid_atomics",
    "invalid_episode",
    "invalid_groupings",
    "invalid_previous_nodes",
    "invalid_topic",
    "memory_hierarchy_projection_error",
    "orphan_episode_member",
    "too_many_atomics",
    "too_many_episodes",
    "too_many_topics",
    "topic_membership_conflict",
    "unassigned_atomic",
})


class MemoryHierarchyProjectionError(ValueError):
    """Stable, data-free projection-planning failure."""

    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_hierarchy_projection_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "memory_hierarchy_projection_error"

    def __repr__(self) -> str:
        return f"MemoryHierarchyProjectionError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryHierarchyProjectionError(category)


def _valid_text(value: object, *, maximum: int, allow_empty: bool = False) -> str:
    if type(value) is not str or len(value) > maximum or (not allow_empty and not value):
        _raise("invalid_atomics")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        _raise("invalid_atomics")
    return value


def _valid_node_key(value: object, category: str) -> str:
    if (
        type(value) is not str
        or len(value) > MAX_NODE_KEY_CHARS
        or _NODE_KEY_PATTERN.fullmatch(value) is None
    ):
        _raise(category)
    return value


def _valid_memory_key(value: object) -> str:
    if type(value) is not str or _MEMORY_KEY_PATTERN.fullmatch(value) is None:
        _raise("invalid_atomics")
    return value


def _json_digest(domain: str, payload: object) -> str:
    encoded = json.dumps(
        {
            "domain": domain,
            "digest_version": DIGEST_VERSION,
            "payload": payload,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class AtomicMemoryProjectionInputV1:
    """Authority-relevant active atomic snapshot consumed transiently."""

    memory_key: str
    kind: str
    scope_type: str
    scope_ref: str = field(repr=False)
    normalized_content: str = field(repr=False)
    fingerprint_version: int
    status: str
    explicitness: str
    confidence: float
    sensitivity: str
    first_observed_at: str
    last_confirmed_at: str
    updated_at: str

    def __repr__(self) -> str:
        return "<AtomicMemoryProjectionInputV1>"


@dataclass(frozen=True, slots=True, repr=False)
class TopicGroupingV1:
    """Opaque topic membership proposal; carries no label or summary text."""

    topic_key: str
    atomic_keys: tuple[str, ...] = field(repr=False)

    def __repr__(self) -> str:
        return "<TopicGroupingV1>"


@dataclass(frozen=True, slots=True, repr=False)
class EpisodeGroupingV1:
    """Opaque episode membership constrained to one already-proposed topic."""

    episode_key: str
    topic_key: str
    atomic_keys: tuple[str, ...] = field(repr=False)

    def __repr__(self) -> str:
        return "<EpisodeGroupingV1>"


@dataclass(frozen=True, slots=True, repr=False)
class ProjectionNodeReceiptV1:
    """Persistable content-free receipt from a prior projection build."""

    node_type: str
    node_key: str
    parent_key: str
    projection_digest: str

    def __repr__(self) -> str:
        return "<ProjectionNodeReceiptV1>"


@dataclass(frozen=True, slots=True, repr=False)
class ProjectionNodePlanV1:
    """One rebuildable hierarchy node containing references, never summaries."""

    node_type: str
    node_key: str
    parent_key: str
    atomic_keys: tuple[str, ...] = field(repr=False)
    projection_digest: str = field(repr=False)
    dirty: bool

    def __repr__(self) -> str:
        return (
            "<ProjectionNodePlanV1 "
            f"type={self.node_type!r} key={self.node_key!r} dirty={self.dirty!r}>"
        )

    def receipt(self) -> ProjectionNodeReceiptV1:
        return ProjectionNodeReceiptV1(
            node_type=self.node_type,
            node_key=self.node_key,
            parent_key=self.parent_key,
            projection_digest=self.projection_digest,
        )


@dataclass(frozen=True, slots=True, repr=False)
class HierarchyProjectionPlanV1:
    """Complete disposable hierarchy manifest for one active atomic snapshot."""

    contract_version: str
    atomic_snapshot_digest: str = field(repr=False)
    nodes: tuple[ProjectionNodePlanV1, ...] = field(repr=False)
    obsolete_node_keys: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            "<HierarchyProjectionPlanV1 "
            f"nodes={len(self.nodes)} obsolete={len(self.obsolete_node_keys)}>"
        )

    @property
    def dirty_node_keys(self) -> tuple[str, ...]:
        return tuple(node.node_key for node in self.nodes if node.dirty)

    def receipts(self) -> tuple[ProjectionNodeReceiptV1, ...]:
        return tuple(node.receipt() for node in self.nodes)


def _validate_atomic(raw: object) -> AtomicMemoryProjectionInputV1:
    if type(raw) is not AtomicMemoryProjectionInputV1:
        _raise("invalid_atomics")
    memory_key = _valid_memory_key(raw.memory_key)
    if raw.kind not in _KINDS or raw.scope_type not in _SCOPE_TYPES:
        _raise("invalid_atomics")
    scope_ref = _valid_text(
        raw.scope_ref,
        maximum=MAX_SCOPE_REF_CHARS,
        allow_empty=True,
    )
    if (raw.scope_type == "global_user") != (scope_ref == ""):
        _raise("invalid_atomics")
    content = _valid_text(raw.normalized_content, maximum=MAX_ATOMIC_CONTENT_CHARS)
    if type(raw.fingerprint_version) is not int or raw.fingerprint_version <= 0:
        _raise("invalid_atomics")
    if raw.status != "active":
        _raise("invalid_atomics")
    if raw.explicitness not in _EXPLICITNESS or raw.sensitivity not in _SENSITIVITIES:
        _raise("invalid_atomics")
    if (
        isinstance(raw.confidence, bool)
        or not isinstance(raw.confidence, (int, float))
        or not math.isfinite(float(raw.confidence))
        or not 0.0 <= float(raw.confidence) <= 1.0
    ):
        _raise("invalid_atomics")
    observed = _valid_text(raw.first_observed_at, maximum=128)
    confirmed = _valid_text(raw.last_confirmed_at, maximum=128)
    updated = _valid_text(raw.updated_at, maximum=128)
    return AtomicMemoryProjectionInputV1(
        memory_key=memory_key,
        kind=raw.kind,
        scope_type=raw.scope_type,
        scope_ref=scope_ref,
        normalized_content=content,
        fingerprint_version=raw.fingerprint_version,
        status="active",
        explicitness=raw.explicitness,
        confidence=float(raw.confidence),
        sensitivity=raw.sensitivity,
        first_observed_at=observed,
        last_confirmed_at=confirmed,
        updated_at=updated,
    )


def _atomic_revision_digest(item: AtomicMemoryProjectionInputV1) -> str:
    return _json_digest(
        "atomic-revision-v1",
        {
            "memory_key": item.memory_key,
            "kind": item.kind,
            "scope_type": item.scope_type,
            "scope_ref": item.scope_ref,
            "normalized_content": item.normalized_content,
            "fingerprint_version": item.fingerprint_version,
            "status": item.status,
            "explicitness": item.explicitness,
            "confidence": item.confidence,
            "sensitivity": item.sensitivity,
            "first_observed_at": item.first_observed_at,
            "last_confirmed_at": item.last_confirmed_at,
            "updated_at": item.updated_at,
        },
    )


def _validate_atomics(raw_atomics: object) -> tuple[
    tuple[AtomicMemoryProjectionInputV1, ...],
    dict[str, str],
]:
    if type(raw_atomics) not in (list, tuple):
        _raise("invalid_atomics")
    if len(raw_atomics) > MAX_ATOMICS:
        _raise("too_many_atomics")
    items: list[AtomicMemoryProjectionInputV1] = []
    revisions: dict[str, str] = {}
    for raw in raw_atomics:
        item = _validate_atomic(raw)
        if item.memory_key in revisions:
            _raise("duplicate_atomic")
        revisions[item.memory_key] = _atomic_revision_digest(item)
        items.append(item)
    items.sort(key=lambda item: item.memory_key)
    return tuple(items), revisions


def _validate_topics(
    raw_topics: object,
    atomic_keys: frozenset[str],
) -> tuple[TopicGroupingV1, ...]:
    if type(raw_topics) not in (list, tuple):
        _raise("invalid_groupings")
    if len(raw_topics) > MAX_TOPICS:
        _raise("too_many_topics")
    topics: list[TopicGroupingV1] = []
    seen_topics: set[str] = set()
    owner_by_atomic: dict[str, str] = {}
    for raw in raw_topics:
        if type(raw) is not TopicGroupingV1 or type(raw.atomic_keys) is not tuple:
            _raise("invalid_topic")
        topic_key = _valid_node_key(raw.topic_key, "invalid_topic")
        if topic_key in seen_topics:
            _raise("duplicate_topic")
        seen_topics.add(topic_key)
        if not raw.atomic_keys:
            _raise("invalid_topic")
        members = tuple(sorted(raw.atomic_keys))
        if len(set(members)) != len(members):
            _raise("invalid_topic")
        for memory_key in members:
            _valid_memory_key(memory_key)
            if memory_key not in atomic_keys:
                _raise("invalid_topic")
            if memory_key in owner_by_atomic:
                _raise("topic_membership_conflict")
            owner_by_atomic[memory_key] = topic_key
        topics.append(TopicGroupingV1(topic_key, members))
    if set(owner_by_atomic) != set(atomic_keys):
        _raise("unassigned_atomic")
    topics.sort(key=lambda topic: topic.topic_key)
    return tuple(topics)


def _validate_episodes(
    raw_episodes: object,
    topic_by_atomic: dict[str, str],
    topic_keys: frozenset[str],
) -> tuple[EpisodeGroupingV1, ...]:
    if type(raw_episodes) not in (list, tuple):
        _raise("invalid_groupings")
    if len(raw_episodes) > MAX_EPISODES:
        _raise("too_many_episodes")
    episodes: list[EpisodeGroupingV1] = []
    seen_episode_keys: set[str] = set()
    episode_owner_by_atomic: dict[str, str] = {}
    for raw in raw_episodes:
        if type(raw) is not EpisodeGroupingV1 or type(raw.atomic_keys) is not tuple:
            _raise("invalid_episode")
        episode_key = _valid_node_key(raw.episode_key, "invalid_episode")
        topic_key = _valid_node_key(raw.topic_key, "invalid_episode")
        if episode_key in seen_episode_keys:
            _raise("duplicate_episode")
        if episode_key in topic_keys or topic_key not in topic_keys:
            _raise("invalid_episode")
        seen_episode_keys.add(episode_key)
        members = tuple(sorted(raw.atomic_keys))
        if len(members) < 2 or len(set(members)) != len(members):
            _raise("invalid_episode")
        for memory_key in members:
            _valid_memory_key(memory_key)
            owner_topic = topic_by_atomic.get(memory_key)
            if owner_topic is None:
                _raise("orphan_episode_member")
            if owner_topic != topic_key:
                _raise("episode_topic_mismatch")
            if memory_key in episode_owner_by_atomic:
                _raise("episode_membership_conflict")
            episode_owner_by_atomic[memory_key] = episode_key
        episodes.append(EpisodeGroupingV1(episode_key, topic_key, members))
    episodes.sort(key=lambda episode: (episode.topic_key, episode.episode_key))
    return tuple(episodes)


def _validate_previous_nodes(
    raw_previous: object,
) -> dict[str, ProjectionNodeReceiptV1]:
    if raw_previous is None:
        return {}
    if type(raw_previous) not in (list, tuple):
        _raise("invalid_previous_nodes")
    previous: dict[str, ProjectionNodeReceiptV1] = {}
    for raw in raw_previous:
        if type(raw) is not ProjectionNodeReceiptV1:
            _raise("invalid_previous_nodes")
        if raw.node_type not in _NODE_TYPES:
            _raise("invalid_previous_nodes")
        node_key = _valid_node_key(raw.node_key, "invalid_previous_nodes")
        if node_key in previous:
            _raise("duplicate_previous_node")
        parent = raw.parent_key
        if type(parent) is not str:
            _raise("invalid_previous_nodes")
        if parent:
            _valid_node_key(parent, "invalid_previous_nodes")
        if type(raw.projection_digest) is not str or _DIGEST_PATTERN.fullmatch(
            raw.projection_digest
        ) is None:
            _raise("invalid_previous_nodes")
        previous[node_key] = raw
    return previous


def _node_digest(
    *,
    node_type: str,
    node_key: str,
    parent_key: str,
    atomic_keys: tuple[str, ...],
    revisions: dict[str, str],
    child_episode_digests: tuple[tuple[str, str], ...] = (),
) -> str:
    return _json_digest(
        f"hierarchy-node-{node_type}-v1",
        {
            "node_type": node_type,
            "node_key": node_key,
            "parent_key": parent_key,
            "atomics": [
                {"memory_key": key, "revision": revisions[key]}
                for key in atomic_keys
            ],
            "episodes": [
                {"episode_key": key, "projection_digest": digest}
                for key, digest in child_episode_digests
            ],
        },
    )


def _planned_node(
    previous: dict[str, ProjectionNodeReceiptV1],
    *,
    node_type: str,
    node_key: str,
    parent_key: str,
    atomic_keys: tuple[str, ...],
    projection_digest: str,
) -> ProjectionNodePlanV1:
    old = previous.get(node_key)
    dirty = (
        old is None
        or old.node_type != node_type
        or old.parent_key != parent_key
        or old.projection_digest != projection_digest
    )
    return ProjectionNodePlanV1(
        node_type=node_type,
        node_key=node_key,
        parent_key=parent_key,
        atomic_keys=atomic_keys,
        projection_digest=projection_digest,
        dirty=dirty,
    )


def plan_hierarchy_projection_v1(
    atomics: object,
    topics: object,
    episodes: object = (),
    *,
    previous_nodes: object = (),
) -> HierarchyProjectionPlanV1:
    """Plan a complete rebuildable hierarchy from authoritative active atomics.

    Every active atomic must belong to exactly one topic.  Episode membership is
    optional but an atomic may belong to at most one episode and that episode
    must be under the atomic's topic.  Canonical-state nodes are structural
    manifests over the topic's active atomics; this function never summarizes
    or rewrites their content.
    """

    items, revisions = _validate_atomics(atomics)
    atomic_keys = frozenset(revisions)
    validated_topics = _validate_topics(topics, atomic_keys)
    topic_by_atomic = {
        memory_key: topic.topic_key
        for topic in validated_topics
        for memory_key in topic.atomic_keys
    }
    topic_keys = frozenset(topic.topic_key for topic in validated_topics)
    validated_episodes = _validate_episodes(
        episodes,
        topic_by_atomic,
        topic_keys,
    )
    previous = _validate_previous_nodes(previous_nodes)

    nodes: list[ProjectionNodePlanV1] = []
    episode_digest_by_key: dict[str, str] = {}
    episodes_by_topic: dict[str, list[EpisodeGroupingV1]] = {
        key: [] for key in topic_keys
    }
    for episode in validated_episodes:
        digest = _node_digest(
            node_type="episode",
            node_key=episode.episode_key,
            parent_key=episode.topic_key,
            atomic_keys=episode.atomic_keys,
            revisions=revisions,
        )
        episode_digest_by_key[episode.episode_key] = digest
        episodes_by_topic[episode.topic_key].append(episode)
        nodes.append(_planned_node(
            previous,
            node_type="episode",
            node_key=episode.episode_key,
            parent_key=episode.topic_key,
            atomic_keys=episode.atomic_keys,
            projection_digest=digest,
        ))

    for topic in validated_topics:
        child_digests = tuple(sorted(
            (
                episode.episode_key,
                episode_digest_by_key[episode.episode_key],
            )
            for episode in episodes_by_topic[topic.topic_key]
        ))
        topic_digest = _node_digest(
            node_type="topic",
            node_key=topic.topic_key,
            parent_key="",
            atomic_keys=topic.atomic_keys,
            revisions=revisions,
            child_episode_digests=child_digests,
        )
        nodes.append(_planned_node(
            previous,
            node_type="topic",
            node_key=topic.topic_key,
            parent_key="",
            atomic_keys=topic.atomic_keys,
            projection_digest=topic_digest,
        ))

        state_key = f"state:{topic.topic_key}"
        if len(state_key) > MAX_NODE_KEY_CHARS or _NODE_KEY_PATTERN.fullmatch(state_key) is None:
            _raise("invalid_topic")
        state_digest = _node_digest(
            node_type="canonical_state",
            node_key=state_key,
            parent_key=topic.topic_key,
            atomic_keys=topic.atomic_keys,
            revisions=revisions,
        )
        nodes.append(_planned_node(
            previous,
            node_type="canonical_state",
            node_key=state_key,
            parent_key=topic.topic_key,
            atomic_keys=topic.atomic_keys,
            projection_digest=state_digest,
        ))

    order = {"topic": 0, "episode": 1, "canonical_state": 2}
    nodes.sort(key=lambda node: (node.parent_key, order[node.node_type], node.node_key))
    current_keys = {node.node_key for node in nodes}
    obsolete = tuple(sorted(set(previous) - current_keys))
    atomic_snapshot_digest = _json_digest(
        "active-atomic-snapshot-v1",
        [
            {"memory_key": item.memory_key, "revision": revisions[item.memory_key]}
            for item in items
        ],
    )
    return HierarchyProjectionPlanV1(
        contract_version=PROJECTION_CONTRACT_VERSION,
        atomic_snapshot_digest=atomic_snapshot_digest,
        nodes=tuple(nodes),
        obsolete_node_keys=obsolete,
    )
