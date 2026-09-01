"""Server-owned query embedding composition for Phase 4D-D3A.

D3A removes the arbitrary QueryVector injection boundary left intentionally open
by D2. Callers provide the exact query text and, only when a current vector
sidecar is configured, a server-owned/injected C3 EmbeddingCallable. The
embedding model and dimensions are taken from the already-proved current vector
sidecar; callers cannot choose a different vector identity for the query.

All local input, authority, and sidecar proof completes before query text is sent
to an embedding provider. The proved immutable in-memory vector plan is then
searched directly, so provider latency cannot open a proof/search sidecar race.
An empty current vector plan performs no query embedding because no semantic
candidate can be produced.

This module is still unwired: it owns no runtime/app route, prompt context,
deployment gate, Memory truth/write authority, hierarchy expansion, or provider
selection policy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from backend import (
    memory_hierarchy_projection as hierarchy,
    memory_hierarchy_snapshot,
    memory_retrieval_bm25 as bm25,
    memory_retrieval_hybrid_fusion as fusion,
    memory_retrieval_hybrid_source as source,
    memory_retrieval_vector as vector,
    memory_retrieval_vector_store as vector_store,
)


HYBRID_QUERY_CONTRACT_VERSION: Final = "memory-retrieval-hybrid-query-v1"

_ERROR_CATEGORIES: Final = frozenset({
    "hybrid_query_authority_unavailable",
    "hybrid_query_bm25_invalid",
    "hybrid_query_configuration_invalid",
    "hybrid_query_embedding_failed",
    "hybrid_query_fusion_invalid",
    "hybrid_query_input_invalid",
    "hybrid_query_stale",
    "hybrid_query_vector_invalid",
    "memory_retrieval_hybrid_query_error",
})

_SOURCE_ERROR_MAP: Final = {
    "hybrid_source_authority_unavailable": "hybrid_query_authority_unavailable",
    "hybrid_source_bm25_invalid": "hybrid_query_bm25_invalid",
    "hybrid_source_configuration_invalid": "hybrid_query_configuration_invalid",
    "hybrid_source_fusion_invalid": "hybrid_query_fusion_invalid",
    "hybrid_source_stale": "hybrid_query_stale",
    "hybrid_source_vector_invalid": "hybrid_query_vector_invalid",
}


class MemoryRetrievalHybridQueryError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_retrieval_hybrid_query_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "memory_retrieval_hybrid_query_error"

    def __repr__(self) -> str:
        return f"MemoryRetrievalHybridQueryError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryRetrievalHybridQueryError(category)


def _raise_source(error: source.MemoryRetrievalHybridSourceError) -> None:
    _raise(_SOURCE_ERROR_MAP.get(error.category, "memory_retrieval_hybrid_query_error"))


@dataclass(frozen=True, slots=True, repr=False)
class HybridQueryResultV1:
    contract_version: str
    source_atomic_count: int
    bm25_generation: int | None
    vector_generation: int | None
    query_embedding_performed: bool
    fusion_result: fusion.HybridFusionResultV1 = field(repr=False)

    def __repr__(self) -> str:
        return (
            "<HybridQueryResultV1 "
            f"source_atomics={self.source_atomic_count} "
            f"bm25_generation={self.bm25_generation!r} "
            f"vector_generation={self.vector_generation!r} "
            f"query_embedding_performed={self.query_embedding_performed!r} "
            f"hits={len(self.fusion_result.hits)}>"
        )


def _validate_input(
    query_text: object,
    reference_time: object,
    *,
    max_hits: object,
    max_bm25_hits: object,
    max_vector_hits: object,
    minimum_vector_similarity: object,
) -> str:
    try:
        query = fusion._validated_query(query_text)
        fusion._parse_reference_time(reference_time)
    except fusion.MemoryRetrievalHybridFusionError:
        _raise("hybrid_query_input_invalid")
    except Exception:
        _raise("hybrid_query_input_invalid")

    if (
        type(max_hits) is not int
        or isinstance(max_hits, bool)
        or not 1 <= max_hits <= fusion.MAX_HITS
        or type(max_bm25_hits) is not int
        or isinstance(max_bm25_hits, bool)
        or not 1 <= max_bm25_hits <= bm25.MAX_HITS
        or type(max_vector_hits) is not int
        or isinstance(max_vector_hits, bool)
        or not 1 <= max_vector_hits <= vector.MAX_VECTOR_HITS
        or isinstance(minimum_vector_similarity, bool)
        or not isinstance(minimum_vector_similarity, (int, float))
        or not math.isfinite(float(minimum_vector_similarity))
        or not -1.0 <= float(minimum_vector_similarity) <= 1.0
    ):
        _raise("hybrid_query_input_invalid")
    return query


def _validate_configuration(
    reader: object,
    bm25_sidecar_path: object,
    term_key_id: object,
    term_hmac_secret: object,
    vector_sidecar_path: object,
    embedding_callable: object,
) -> tuple[Path, Path | None, Path | None]:
    if type(reader) is not memory_hierarchy_snapshot.MemoryHierarchySnapshotReader:
        _raise("hybrid_query_configuration_invalid")

    bm25_enabled = bm25_sidecar_path is not None
    if bm25_enabled != (term_key_id is not None and term_hmac_secret is not None):
        _raise("hybrid_query_configuration_invalid")
    if not bm25_enabled and (term_key_id is not None or term_hmac_secret is not None):
        _raise("hybrid_query_configuration_invalid")

    vector_enabled = vector_sidecar_path is not None
    if vector_enabled:
        if not callable(embedding_callable):
            _raise("hybrid_query_configuration_invalid")
    elif embedding_callable is not None:
        _raise("hybrid_query_configuration_invalid")

    try:
        authority = Path(reader._database_path).resolve(strict=False)
        bm25_path = (
            Path(bm25_sidecar_path).resolve(strict=False)
            if bm25_enabled
            else None
        )
        vector_path = (
            Path(vector_sidecar_path).resolve(strict=False)
            if vector_enabled
            else None
        )
    except (AttributeError, OSError, TypeError, ValueError):
        _raise("hybrid_query_configuration_invalid")

    paths = [authority]
    if bm25_path is not None:
        paths.append(bm25_path)
    if vector_path is not None:
        paths.append(vector_path)
    if len(set(paths)) != len(paths):
        _raise("hybrid_query_configuration_invalid")
    return authority, bm25_path, vector_path


def _validate_touch_hints(
    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
    touch_hints: object,
) -> None:
    try:
        _, by_key = fusion._validated_atomics(atomics)
        fusion._validated_touch_hints(touch_hints, frozenset(by_key))
    except fusion.MemoryRetrievalHybridFusionError:
        _raise("hybrid_query_input_invalid")
    except Exception:
        _raise("hybrid_query_input_invalid")


def _load_current_vector_plan(
    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
    current_digest: str,
    path: Path,
) -> vector_store.VectorStoreSnapshotV1:
    try:
        stored = vector_store.load_vector_store_snapshot(path)
        if stored.plan.source_snapshot_digest != current_digest:
            _raise("hybrid_query_stale")
        actual_bindings = tuple(
            (document.memory_key, document.atomic_revision_digest)
            for document in stored.plan.documents
        )
        if actual_bindings != source._expected_vector_bindings(atomics):
            _raise("hybrid_query_vector_invalid")
        return stored
    except MemoryRetrievalHybridQueryError:
        raise
    except vector_store.MemoryRetrievalVectorStoreError:
        _raise("hybrid_query_vector_invalid")
    except source.MemoryRetrievalHybridSourceError as error:
        _raise_source(error)
    except Exception:
        _raise("hybrid_query_vector_invalid")


async def fuse_current_hybrid_query_v1(
    reader: object,
    embedding_callable: object = None,
    *,
    query_text: object,
    reference_time: object,
    bm25_sidecar_path: object = None,
    term_key_id: object = None,
    term_hmac_secret: object = None,
    vector_sidecar_path: object = None,
    touch_hints: object = (),
    max_hits: object = fusion.MAX_HITS,
    max_bm25_hits: object = bm25.MAX_HITS,
    max_vector_hits: object = vector.MAX_VECTOR_HITS,
    minimum_vector_similarity: object = 0.0,
) -> HybridQueryResultV1:
    """Embed the exact proved query server-side, then run same-revision fusion."""

    query = _validate_input(
        query_text,
        reference_time,
        max_hits=max_hits,
        max_bm25_hits=max_bm25_hits,
        max_vector_hits=max_vector_hits,
        minimum_vector_similarity=minimum_vector_similarity,
    )
    _, bm25_path, vector_path = _validate_configuration(
        reader,
        bm25_sidecar_path,
        term_key_id,
        term_hmac_secret,
        vector_sidecar_path,
        embedding_callable,
    )

    try:
        snapshot, current_digest = source._load_authoritative_snapshot(reader)
    except source.MemoryRetrievalHybridSourceError as error:
        _raise_source(error)

    # Touch metadata is local ranking input. Validate it against the exact proved
    # eligible Atomic set before any sidecar/provider work can use the query.
    _validate_touch_hints(snapshot.atomics, touch_hints)

    sparse: bm25.BM25SearchResultV1 | None = None
    bm25_generation: int | None = None
    if bm25_path is not None:
        try:
            sparse, bm25_generation = source._load_current_bm25(
                snapshot.atomics,
                current_digest,
                bm25_path,
                query,
                term_key_id=term_key_id,
                term_hmac_secret=term_hmac_secret,
                max_hits=max_bm25_hits,
            )
        except source.MemoryRetrievalHybridSourceError as error:
            _raise_source(error)

    vector_snapshot: vector_store.VectorStoreSnapshotV1 | None = None
    if vector_path is not None:
        vector_snapshot = _load_current_vector_plan(
            snapshot.atomics,
            current_digest,
            vector_path,
        )

    # No query text reaches an embedding provider until every configured local
    # input/source above has passed validation and current-revision proof.
    semantic: vector.VectorSearchResultV1 | None = None
    vector_generation: int | None = None
    query_embedding_performed = False
    if vector_snapshot is not None:
        vector_generation = vector_snapshot.generation
        if vector_snapshot.plan.document_count == 0:
            semantic = vector.VectorSearchResultV1(
                hits=(),
                indexed_document_count=0,
            )
        else:
            try:
                query_vector = await vector.embed_query_vector_v1(
                    embedding_callable,
                    query,
                    embedding_model=vector_snapshot.plan.embedding_model,
                    dimensions=vector_snapshot.plan.dimensions,
                )
            except vector.MemoryRetrievalVectorError:
                _raise("hybrid_query_embedding_failed")
            except Exception:
                _raise("hybrid_query_embedding_failed")
            query_embedding_performed = True
            try:
                semantic = vector.search_vector_index_v1(
                    vector_snapshot.plan,
                    query_vector,
                    max_hits=max_vector_hits,
                    minimum_similarity=minimum_vector_similarity,
                )
            except vector.MemoryRetrievalVectorError:
                _raise("hybrid_query_vector_invalid")
            except Exception:
                _raise("hybrid_query_vector_invalid")

    try:
        fused = fusion.fuse_hybrid_retrieval_v1(
            snapshot.atomics,
            query_text=query,
            bm25_result=sparse,
            vector_result=semantic,
            reference_time=reference_time,
            touch_hints=touch_hints,
            max_hits=max_hits,
        )
    except fusion.MemoryRetrievalHybridFusionError:
        _raise("hybrid_query_fusion_invalid")
    except Exception:
        _raise("hybrid_query_fusion_invalid")

    return HybridQueryResultV1(
        contract_version=HYBRID_QUERY_CONTRACT_VERSION,
        source_atomic_count=snapshot.count,
        bm25_generation=bm25_generation,
        vector_generation=vector_generation,
        query_embedding_performed=query_embedding_performed,
        fusion_result=fused,
    )


__all__ = (
    "HYBRID_QUERY_CONTRACT_VERSION",
    "HybridQueryResultV1",
    "MemoryRetrievalHybridQueryError",
    "fuse_current_hybrid_query_v1",
)
