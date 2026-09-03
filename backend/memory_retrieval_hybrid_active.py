"""Same-revision Hybrid Retrieval active-selection foundation for Phase 4D-D3C1.

This module is deliberately *unwired*.  It defines the authority boundary that a
future active Hybrid rollout must cross before any Hybrid-ranked Atomic Memory may
become provider-visible context.

The existing Hybrid query runner proves BM25/vector sidecars against one
Authoritative Atomic snapshot.  Active use needs one extra guarantee: the
plaintext rendered after the asynchronous query/embedding work must still belong
to that exact revision.  D3C1 therefore re-reads the authoritative snapshot after
the query and re-proves the exact BM25/vector sidecar generations used by the
query result before mapping ranked keys back to Atomic plaintext.

No result from this module is installed into the relay, no environment gate is
introduced here, and no provider-visible Memory authority changes in this phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from backend import (
    memory_context,
    memory_hierarchy_projection as hierarchy,
    memory_hierarchy_snapshot,
    memory_retrieval_bm25 as bm25,
    memory_retrieval_hybrid_fusion as fusion,
    memory_retrieval_hybrid_query as hybrid_query,
    memory_retrieval_hybrid_runtime_composition as runtime_composition,
    memory_retrieval_hybrid_source as source,
)


HYBRID_ACTIVE_CONTRACT_VERSION: Final = "memory-retrieval-hybrid-active-v1"
ACTIVE_MAX_ITEMS: Final = memory_context.DEFAULT_MAX_ITEMS
ACTIVE_CHARACTER_BUDGET: Final = memory_context.DEFAULT_CHARACTER_BUDGET

_ERROR_CATEGORIES: Final = frozenset({
    "hybrid_active_channels_unavailable",
    "hybrid_active_configuration_invalid",
    "hybrid_active_query_failed",
    "hybrid_active_render_failed",
    "hybrid_active_selection_invalid",
    "hybrid_active_stale",
    "memory_retrieval_hybrid_active_error",
})


class MemoryRetrievalHybridActiveError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_retrieval_hybrid_active_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except BaseException:
            return "memory_retrieval_hybrid_active_error"

    def __repr__(self) -> str:
        return f"MemoryRetrievalHybridActiveError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryRetrievalHybridActiveError(category)


@dataclass(frozen=True, slots=True, repr=False, init=False)
class HybridActiveSelectionV1:
    """Bounded same-revision Atomic selection; plaintext is intentionally hidden."""

    contract_version: str
    source_atomic_count: int
    selected_count: int
    total_chars: int
    query_embedding_performed: bool
    memory_keys: tuple[str, ...] = field(repr=False)
    items: tuple[dict, ...] = field(repr=False)

    def __new__(cls, *_args: object, **_kwargs: object):
        _raise("hybrid_active_selection_invalid")

    def __repr__(self) -> str:
        try:
            return (
                "<HybridActiveSelectionV1 "
                f"source_atomics={object.__getattribute__(self, 'source_atomic_count')} "
                f"selected={object.__getattribute__(self, 'selected_count')} "
                f"total_chars={object.__getattribute__(self, 'total_chars')} "
                f"embedding={object.__getattribute__(self, 'query_embedding_performed')}>"
            )
        except BaseException:
            return "<HybridActiveSelectionV1 invalid>"


def _validated_query(value: object) -> str:
    try:
        return fusion._validated_query(value)
    except fusion.MemoryRetrievalHybridFusionError:
        _raise("hybrid_active_configuration_invalid")
    except BaseException:
        _raise("hybrid_active_configuration_invalid")


def _validated_budget(
    max_items: object,
    character_budget: object,
) -> tuple[int, int]:
    if (
        type(max_items) is not int
        or isinstance(max_items, bool)
        or not 1 <= max_items <= ACTIVE_MAX_ITEMS
        or type(character_budget) is not int
        or isinstance(character_budget, bool)
        or not 1 <= character_budget <= ACTIVE_CHARACTER_BUDGET
    ):
        _raise("hybrid_active_configuration_invalid")
    return max_items, character_budget


def _validated_runner(
    runner: object,
) -> runtime_composition.HybridRetrievalShadowRunnerV1:
    if type(runner) is not runtime_composition.HybridRetrievalShadowRunnerV1:
        _raise("hybrid_active_configuration_invalid")
    try:
        config = runner.config
        reader = runner.reader
        if (
            type(config) is not runtime_composition.HybridRuntimeConfigV1
            or type(reader)
            is not memory_hierarchy_snapshot.MemoryHierarchySnapshotReader
        ):
            _raise("hybrid_active_configuration_invalid")
        authority = Path(reader._database_path).resolve(strict=False)
        if authority != Path(config.authority_path).resolve(strict=False):
            _raise("hybrid_active_configuration_invalid")
        if not runtime_composition._paths_are_separate(
            config.authority_path,
            config.bm25_path,
            config.vector_path,
            config.persistent_root,
        ):
            _raise("hybrid_active_configuration_invalid")
        return runner
    except MemoryRetrievalHybridActiveError:
        raise
    except BaseException:
        _raise("hybrid_active_configuration_invalid")


def _validated_query_result(
    raw: object,
) -> hybrid_query.HybridQueryResultV1:
    if type(raw) is not hybrid_query.HybridQueryResultV1:
        _raise("hybrid_active_query_failed")
    try:
        fused = raw.fusion_result
        if (
            raw.contract_version != hybrid_query.HYBRID_QUERY_CONTRACT_VERSION
            or type(raw.source_atomic_count) is not int
            or isinstance(raw.source_atomic_count, bool)
            or raw.source_atomic_count < 0
            or type(raw.bm25_generation) is not int
            or isinstance(raw.bm25_generation, bool)
            or raw.bm25_generation < 1
            or type(raw.vector_generation) is not int
            or isinstance(raw.vector_generation, bool)
            or raw.vector_generation < 1
            or type(raw.query_embedding_performed) is not bool
            or type(fused) is not fusion.HybridFusionResultV1
            or fused.contract_version != fusion.HYBRID_FUSION_CONTRACT_VERSION
            or fused.bm25_available is not True
            or fused.vector_available is not True
        ):
            _raise("hybrid_active_channels_unavailable")
        return raw
    except MemoryRetrievalHybridActiveError:
        raise
    except BaseException:
        _raise("hybrid_active_query_failed")


def _reprove_query_revision(
    runner: runtime_composition.HybridRetrievalShadowRunnerV1,
    query_text: str,
    result: hybrid_query.HybridQueryResultV1,
) -> memory_hierarchy_snapshot.HierarchyAtomicSnapshotV1:
    config = runner.config
    try:
        snapshot, digest = source._load_authoritative_snapshot(runner.reader)
        if snapshot.count != result.source_atomic_count:
            _raise("hybrid_active_stale")

        sparse, bm25_generation = source._load_current_bm25(
            snapshot.atomics,
            digest,
            config.bm25_path,
            query_text,
            term_key_id=config.term_key_id,
            term_hmac_secret=config.term_hmac_secret,
            max_hits=bm25.MAX_HITS,
        )
        vector_snapshot = hybrid_query._load_current_vector_plan(
            snapshot.atomics,
            digest,
            config.vector_path,
        )
        if (
            bm25_generation != result.bm25_generation
            or vector_snapshot.generation != result.vector_generation
            or len(sparse.hits) != result.fusion_result.bm25_hit_count
        ):
            _raise("hybrid_active_stale")

        exact = fusion._exact_channel(snapshot.atomics, query_text)
        lexical = fusion._lexical_channel(snapshot.atomics, query_text)
        if (
            len(exact) != result.fusion_result.exact_hit_count
            or len(lexical) != result.fusion_result.lexical_hit_count
        ):
            _raise("hybrid_active_stale")
        return snapshot
    except MemoryRetrievalHybridActiveError:
        raise
    except (
        source.MemoryRetrievalHybridSourceError,
        hybrid_query.MemoryRetrievalHybridQueryError,
        fusion.MemoryRetrievalHybridFusionError,
    ):
        _raise("hybrid_active_stale")
    except BaseException:
        _raise("hybrid_active_stale")


def _atomic_context_item(
    item: hierarchy.AtomicMemoryProjectionInputV1,
) -> dict:
    return {
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
        "created_at": item.first_observed_at,
        "updated_at": item.updated_at,
        "provenance": [],
    }


def _selection_from_result(
    snapshot: memory_hierarchy_snapshot.HierarchyAtomicSnapshotV1,
    result: hybrid_query.HybridQueryResultV1,
    *,
    max_items: int,
    character_budget: int,
) -> HybridActiveSelectionV1:
    try:
        _eligible, by_key = fusion._validated_atomics(snapshot.atomics)
        hits = result.fusion_result.hits
        if type(hits) is not tuple or len(hits) > fusion.MAX_HITS:
            _raise("hybrid_active_selection_invalid")

        selected_items: list[dict] = []
        selected_keys: list[str] = []
        seen: set[str] = set()
        total_chars = 0
        for hit in hits:
            if type(hit) is not fusion.HybridFusionHitV1:
                _raise("hybrid_active_selection_invalid")
            key = hit.memory_key
            if key in seen or key not in by_key:
                _raise("hybrid_active_selection_invalid")
            seen.add(key)
            if len(selected_items) >= max_items:
                break
            atomic = by_key[key]
            next_chars = total_chars + len(atomic.normalized_content)
            if next_chars > character_budget:
                break
            selected_keys.append(key)
            selected_items.append(_atomic_context_item(atomic))
            total_chars = next_chars

        bundle = memory_context.build_memory_context_bundle(
            tuple(selected_items),
            scope_type="global_user",
            max_items=max_items,
            character_budget=character_budget,
        )
        if (
            bundle.item_count != len(selected_items)
            or bundle.total_chars != total_chars
        ):
            _raise("hybrid_active_selection_invalid")

        selection = object.__new__(HybridActiveSelectionV1)
        object.__setattr__(
            selection,
            "contract_version",
            HYBRID_ACTIVE_CONTRACT_VERSION,
        )
        object.__setattr__(selection, "source_atomic_count", snapshot.count)
        object.__setattr__(selection, "selected_count", len(selected_items))
        object.__setattr__(selection, "total_chars", total_chars)
        object.__setattr__(
            selection,
            "query_embedding_performed",
            result.query_embedding_performed,
        )
        object.__setattr__(selection, "memory_keys", tuple(selected_keys))
        object.__setattr__(selection, "items", tuple(selected_items))
        return selection
    except MemoryRetrievalHybridActiveError:
        raise
    except (fusion.MemoryRetrievalHybridFusionError, memory_context.MemoryContextError):
        _raise("hybrid_active_selection_invalid")
    except BaseException:
        _raise("hybrid_active_selection_invalid")


async def plan_hybrid_active_selection_v1(
    runner: object,
    *,
    query_text: object,
    max_items: object = ACTIVE_MAX_ITEMS,
    character_budget: object = ACTIVE_CHARACTER_BUDGET,
) -> HybridActiveSelectionV1:
    """Run server-owned Hybrid retrieval and bind ranked plaintext to its revision."""

    active_runner = _validated_runner(runner)
    query = _validated_query(query_text)
    max_items, character_budget = _validated_budget(max_items, character_budget)
    try:
        raw_result = await active_runner(query_text=query)
    except Exception:
        _raise("hybrid_active_query_failed")
    result = _validated_query_result(raw_result)
    snapshot = _reprove_query_revision(active_runner, query, result)
    return _selection_from_result(
        snapshot,
        result,
        max_items=max_items,
        character_budget=character_budget,
    )


def render_hybrid_active_developer_message_v1(
    selection: object,
) -> dict[str, str] | None:
    """Render only a D3C1-created bounded selection using the existing Memory envelope."""

    if type(selection) is not HybridActiveSelectionV1:
        _raise("hybrid_active_render_failed")
    try:
        if (
            selection.contract_version != HYBRID_ACTIVE_CONTRACT_VERSION
            or type(selection.selected_count) is not int
            or not 0 <= selection.selected_count <= ACTIVE_MAX_ITEMS
            or type(selection.total_chars) is not int
            or not 0 <= selection.total_chars <= ACTIVE_CHARACTER_BUDGET
            or type(selection.query_embedding_performed) is not bool
            or type(selection.memory_keys) is not tuple
            or len(selection.memory_keys) != selection.selected_count
            or len(set(selection.memory_keys)) != len(selection.memory_keys)
            or type(selection.items) is not tuple
            or len(selection.items) != selection.selected_count
        ):
            _raise("hybrid_active_render_failed")
        return memory_context.render_memory_developer_message(
            selection.items,
            scope_type="global_user",
            max_items=ACTIVE_MAX_ITEMS,
            character_budget=ACTIVE_CHARACTER_BUDGET,
        )
    except MemoryRetrievalHybridActiveError:
        raise
    except memory_context.MemoryContextError:
        _raise("hybrid_active_render_failed")
    except BaseException:
        _raise("hybrid_active_render_failed")


__all__ = (
    "ACTIVE_CHARACTER_BUDGET",
    "ACTIVE_MAX_ITEMS",
    "HYBRID_ACTIVE_CONTRACT_VERSION",
    "HybridActiveSelectionV1",
    "MemoryRetrievalHybridActiveError",
    "plan_hybrid_active_selection_v1",
    "render_hybrid_active_developer_message_v1",
)
