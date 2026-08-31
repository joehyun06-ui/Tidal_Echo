"""Source-bound derived text contract for Phase 4D-B6.

Hierarchy summaries are disposable routing/compression text, never Memory truth.
The caller/model may author only clause text plus supporting Atomic Memory keys.
The server independently re-proves the hierarchy plan, binds the target node and
projection digest, requires complete Atomic coverage, validates every clause
through MemoryPolicy, and derives the summary digest itself.

Only Topic and Canonical-State nodes are summarizable in this phase.  Episode
structure may be supplied as organization hints for Topic summaries, but Episode
text is not generated here.  A summary whose projection digest no longer matches
its hierarchy node is stale by definition and must not be used.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Final

from backend import (
    memory_hierarchy_projection as hierarchy,
    memory_policy,
)


SUMMARY_CONTRACT_VERSION: Final = "memory-hierarchy-summary-v1"
SUMMARY_AUTHORITY: Final = "derived_routing_only"
MAX_SUMMARY_ATOMICS: Final = 32
MAX_SUMMARY_CLAUSES: Final = 12
MAX_CLAUSE_CHARS: Final = 400
MAX_TOTAL_SUMMARY_CHARS: Final = 1_600
_SUPPORTED_NODE_TYPES: Final = frozenset({"topic", "canonical_state"})
_DIGEST_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")

_ERROR_CATEGORIES: Final = frozenset({
    "duplicate_summary_clause",
    "incomplete_summary_coverage",
    "invalid_atomics",
    "invalid_hierarchy_plan",
    "invalid_summary_clause",
    "invalid_summary_clauses",
    "invalid_summary_target",
    "memory_hierarchy_summary_error",
    "sensitive_summary_disabled",
    "summary_policy_rejected",
    "summary_too_long",
    "too_many_summary_atomics",
    "too_many_summary_clauses",
    "unknown_summary_support",
})


class MemoryHierarchySummaryError(ValueError):
    """Stable, data-free derived-summary failure."""

    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_hierarchy_summary_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "memory_hierarchy_summary_error"

    def __repr__(self) -> str:
        return f"MemoryHierarchySummaryError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryHierarchySummaryError(category)


@dataclass(frozen=True, slots=True, repr=False)
class SummaryClauseProposalV1:
    """One model-authored clause bound only to existing Atomic Memory keys."""

    atomic_keys: tuple[str, ...] = field(repr=False)
    text: str = field(repr=False)

    def __repr__(self) -> str:
        return f"<SummaryClauseProposalV1 supports={len(self.atomic_keys)}>"


@dataclass(frozen=True, slots=True, repr=False)
class SummaryTargetV1:
    """Server-proved current hierarchy node plus its authoritative Atomics."""

    node_type: str
    node_key: str
    projection_digest: str = field(repr=False)
    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...] = field(repr=False)
    episode_groups: tuple[tuple[str, ...], ...] = field(repr=False)

    def __repr__(self) -> str:
        return (
            "<SummaryTargetV1 "
            f"type={self.node_type!r} atomics={len(self.atomics)} "
            f"episodes={len(self.episode_groups)}>"
        )


@dataclass(frozen=True, slots=True, repr=False)
class DerivedNodeSummaryV1:
    """Validated routing-only text bound to one exact projection revision."""

    contract_version: str
    authority: str
    node_type: str
    node_key: str
    projection_digest: str = field(repr=False)
    summary_digest: str = field(repr=False)
    clauses: tuple[SummaryClauseProposalV1, ...] = field(repr=False)

    def __repr__(self) -> str:
        return (
            "<DerivedNodeSummaryV1 "
            f"type={self.node_type!r} clauses={len(self.clauses)} "
            f"authority={self.authority!r}>"
        )

    @property
    def text(self) -> str:
        return "\n".join(clause.text for clause in self.clauses)

    @property
    def support_keys(self) -> tuple[str, ...]:
        return tuple(sorted({
            memory_key
            for clause in self.clauses
            for memory_key in clause.atomic_keys
        }))


def _validated_atomics(atomics: object) -> tuple[
    tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
    dict[str, hierarchy.AtomicMemoryProjectionInputV1],
]:
    try:
        validated, _ = hierarchy._validate_atomics(atomics)
    except hierarchy.MemoryHierarchyProjectionError:
        _raise("invalid_atomics")
    return validated, {item.memory_key: item for item in validated}


def _node_signature(node: hierarchy.ProjectionNodePlanV1) -> tuple:
    return (
        node.node_type,
        node.node_key,
        node.parent_key,
        node.atomic_keys,
        node.projection_digest,
    )


def _reprove_plan(
    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
    raw_plan: object,
) -> hierarchy.HierarchyProjectionPlanV1:
    if type(raw_plan) is not hierarchy.HierarchyProjectionPlanV1:
        _raise("invalid_hierarchy_plan")
    if raw_plan.contract_version != hierarchy.PROJECTION_CONTRACT_VERSION:
        _raise("invalid_hierarchy_plan")
    if type(raw_plan.nodes) is not tuple:
        _raise("invalid_hierarchy_plan")

    topics: list[hierarchy.TopicGroupingV1] = []
    episodes: list[hierarchy.EpisodeGroupingV1] = []
    for node in raw_plan.nodes:
        if type(node) is not hierarchy.ProjectionNodePlanV1:
            _raise("invalid_hierarchy_plan")
        if node.node_type == "topic":
            topics.append(hierarchy.TopicGroupingV1(node.node_key, node.atomic_keys))
        elif node.node_type == "episode":
            episodes.append(
                hierarchy.EpisodeGroupingV1(
                    node.node_key,
                    node.parent_key,
                    node.atomic_keys,
                )
            )
        elif node.node_type != "canonical_state":
            _raise("invalid_hierarchy_plan")
    try:
        rebuilt = hierarchy.plan_hierarchy_projection_v1(
            atomics,
            tuple(topics),
            tuple(episodes),
        )
    except hierarchy.MemoryHierarchyProjectionError:
        _raise("invalid_hierarchy_plan")
    if (
        raw_plan.atomic_snapshot_digest != rebuilt.atomic_snapshot_digest
        or tuple(_node_signature(node) for node in raw_plan.nodes)
        != tuple(_node_signature(node) for node in rebuilt.nodes)
    ):
        _raise("invalid_hierarchy_plan")
    return raw_plan


def prepare_summary_target_v1(
    atomics: object,
    plan: object,
    node_key: object,
) -> SummaryTargetV1:
    """Re-prove one current Topic/Canonical-State target before model access."""

    validated_atomics, atomics_by_key = _validated_atomics(atomics)
    proved = _reprove_plan(validated_atomics, plan)
    if type(node_key) is not str or not node_key:
        _raise("invalid_summary_target")
    target = next((node for node in proved.nodes if node.node_key == node_key), None)
    if target is None or target.node_type not in _SUPPORTED_NODE_TYPES:
        _raise("invalid_summary_target")
    if len(target.atomic_keys) > MAX_SUMMARY_ATOMICS:
        _raise("too_many_summary_atomics")
    if not target.atomic_keys:
        _raise("invalid_summary_target")

    members: list[hierarchy.AtomicMemoryProjectionInputV1] = []
    for memory_key in target.atomic_keys:
        atomic = atomics_by_key.get(memory_key)
        if atomic is None:
            _raise("invalid_hierarchy_plan")
        if atomic.sensitivity != "normal":
            _raise("sensitive_summary_disabled")
        members.append(atomic)

    episode_groups: tuple[tuple[str, ...], ...] = ()
    if target.node_type == "topic":
        episode_groups = tuple(
            node.atomic_keys
            for node in proved.nodes
            if node.node_type == "episode" and node.parent_key == target.node_key
        )
    return SummaryTargetV1(
        node_type=target.node_type,
        node_key=target.node_key,
        projection_digest=target.projection_digest,
        atomics=tuple(members),
        episode_groups=episode_groups,
    )


def _summary_digest(
    target: SummaryTargetV1,
    clauses: tuple[SummaryClauseProposalV1, ...],
) -> str:
    payload = {
        "contract_version": SUMMARY_CONTRACT_VERSION,
        "node_key": target.node_key,
        "node_type": target.node_type,
        "projection_digest": target.projection_digest,
        "clauses": [
            {"atomic_keys": list(clause.atomic_keys), "text": clause.text}
            for clause in clauses
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_summary_clauses_v1(
    target: object,
    clauses: object,
) -> DerivedNodeSummaryV1:
    """Validate full Atomic coverage and policy-safe routing-only clauses."""

    if type(target) is not SummaryTargetV1:
        _raise("invalid_summary_target")
    if type(clauses) not in (list, tuple):
        _raise("invalid_summary_clauses")
    if not clauses:
        _raise("incomplete_summary_coverage")
    if len(clauses) > MAX_SUMMARY_CLAUSES:
        _raise("too_many_summary_clauses")

    target_keys = {item.memory_key for item in target.atomics}
    policy = memory_policy.MemoryPolicy(
        max_item_chars=MAX_CLAUSE_CHARS,
        sensitive_storage_enabled=False,
    )
    covered: set[str] = set()
    seen_clauses: set[tuple[tuple[str, ...], str]] = set()
    normalized_clauses: list[SummaryClauseProposalV1] = []

    for raw in clauses:
        if type(raw) is not SummaryClauseProposalV1 or type(raw.atomic_keys) is not tuple:
            _raise("invalid_summary_clause")
        if not raw.atomic_keys or len(set(raw.atomic_keys)) != len(raw.atomic_keys):
            _raise("invalid_summary_clause")
        keys = tuple(sorted(raw.atomic_keys))
        if any(memory_key not in target_keys for memory_key in keys):
            _raise("unknown_summary_support")
        try:
            text = policy.validate_content(raw.text, "normal")
        except memory_policy.MemoryPolicyError:
            _raise("summary_policy_rejected")
        identity = (keys, text)
        if identity in seen_clauses:
            _raise("duplicate_summary_clause")
        seen_clauses.add(identity)
        covered.update(keys)
        normalized_clauses.append(SummaryClauseProposalV1(keys, text))

    if covered != target_keys:
        _raise("incomplete_summary_coverage")
    canonical = tuple(sorted(
        normalized_clauses,
        key=lambda clause: (clause.atomic_keys, clause.text),
    ))
    total_chars = sum(len(clause.text) for clause in canonical) + max(0, len(canonical) - 1)
    if total_chars > MAX_TOTAL_SUMMARY_CHARS:
        _raise("summary_too_long")
    digest = _summary_digest(target, canonical)
    if _DIGEST_PATTERN.fullmatch(digest) is None:
        _raise("invalid_summary_clauses")
    return DerivedNodeSummaryV1(
        contract_version=SUMMARY_CONTRACT_VERSION,
        authority=SUMMARY_AUTHORITY,
        node_type=target.node_type,
        node_key=target.node_key,
        projection_digest=target.projection_digest,
        summary_digest=digest,
        clauses=canonical,
    )
