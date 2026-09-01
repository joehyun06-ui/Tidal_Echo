"""Provider-agnostic vector retrieval contract for Phase 4D-C3.

This module owns no provider, persistence, Memory authority, or prompt-context
wiring.  It embeds only already-proved normal global-user Atomic Memory through
an injected callable.  The provider receives plaintext content by ordinal only;
Memory keys never cross the embedding-call boundary.  Server code rebinds each
returned vector to the exact Atomic revision and source snapshot digest.

Vectors are canonicalized to finite float32 unit vectors.  Search returns only
Memory keys and cosine similarities.  Sensitive/restricted or non-global Memory
is never sent to an embedding provider by this contract.
"""

from __future__ import annotations

import asyncio
import math
import re
import struct
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Final

from backend import memory_hierarchy_projection as hierarchy


VECTOR_CONTRACT_VERSION: Final = "memory-retrieval-vector-v1"
EMBEDDING_CONTRACT_VERSION: Final = "memory-retrieval-embedding-v1"
MAX_VECTOR_DIMENSIONS: Final = 4096
MIN_VECTOR_DIMENSIONS: Final = 2
MAX_EMBEDDING_BATCH: Final = 32
MAX_QUERY_CHARS: Final = 32_000
MAX_VECTOR_HITS: Final = 20
EMBEDDING_TIMEOUT_SECONDS: Final = 45.0

_MODEL_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_DIGEST_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")
_MEMORY_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")

_ERROR_CATEGORIES: Final = frozenset({
    "embedding_invalid_output",
    "embedding_timeout",
    "embedding_unavailable",
    "invalid_atomics",
    "invalid_embedding_callable",
    "invalid_embedding_model",
    "invalid_index_plan",
    "invalid_query",
    "invalid_vector",
    "memory_retrieval_vector_error",
})


class MemoryRetrievalVectorError(ValueError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_retrieval_vector_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "memory_retrieval_vector_error"

    def __repr__(self) -> str:
        return f"MemoryRetrievalVectorError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryRetrievalVectorError(category)


EmbeddingCallable = Callable[
    [tuple[str, ...], str, int],
    Awaitable[object],
]


@dataclass(frozen=True, slots=True, repr=False)
class VectorDocumentPlanV1:
    memory_key: str = field(repr=False)
    atomic_revision_digest: str = field(repr=False)
    vector: tuple[float, ...] = field(repr=False)

    def __repr__(self) -> str:
        return f"<VectorDocumentPlanV1 dimensions={len(self.vector)}>"


@dataclass(frozen=True, slots=True, repr=False)
class VectorIndexPlanV1:
    contract_version: str
    embedding_contract_version: str
    source_snapshot_digest: str = field(repr=False)
    embedding_model: str
    dimensions: int
    documents: tuple[VectorDocumentPlanV1, ...] = field(repr=False)

    @property
    def document_count(self) -> int:
        return len(self.documents)

    def __repr__(self) -> str:
        return (
            "<VectorIndexPlanV1 "
            f"documents={self.document_count} dimensions={self.dimensions}>"
        )


@dataclass(frozen=True, slots=True, repr=False)
class VectorIndexBuildResultV1:
    plan: VectorIndexPlanV1 = field(repr=False)
    provider_call_count: int

    def __repr__(self) -> str:
        return (
            "<VectorIndexBuildResultV1 "
            f"documents={self.plan.document_count} "
            f"provider_calls={self.provider_call_count}>"
        )


@dataclass(frozen=True, slots=True, repr=False)
class QueryVectorV1:
    embedding_model: str
    dimensions: int
    vector: tuple[float, ...] = field(repr=False)

    def __repr__(self) -> str:
        return f"<QueryVectorV1 dimensions={self.dimensions}>"


@dataclass(frozen=True, slots=True, repr=False)
class VectorSearchHitV1:
    memory_key: str = field(repr=False)
    similarity: float

    def __repr__(self) -> str:
        return f"<VectorSearchHitV1 similarity={self.similarity:.6f}>"


@dataclass(frozen=True, slots=True, repr=False)
class VectorSearchResultV1:
    hits: tuple[VectorSearchHitV1, ...] = field(repr=False)
    indexed_document_count: int

    def __repr__(self) -> str:
        return (
            "<VectorSearchResultV1 "
            f"hits={len(self.hits)} documents={self.indexed_document_count}>"
        )


def _validate_model_and_dimensions(
    embedding_model: object,
    dimensions: object,
) -> tuple[str, int]:
    if (
        type(embedding_model) is not str
        or _MODEL_PATTERN.fullmatch(embedding_model) is None
        or type(dimensions) is not int
        or isinstance(dimensions, bool)
        or not MIN_VECTOR_DIMENSIONS <= dimensions <= MAX_VECTOR_DIMENSIONS
    ):
        _raise("invalid_embedding_model")
    return embedding_model, dimensions


def _validate_source_digest(value: object) -> str:
    if type(value) is not str or _DIGEST_PATTERN.fullmatch(value) is None:
        _raise("invalid_index_plan")
    return value


def _float32(value: object, category: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _raise(category)
    parsed = float(value)
    if not math.isfinite(parsed):
        _raise(category)
    try:
        canonical = struct.unpack("<f", struct.pack("<f", parsed))[0]
    except (OverflowError, struct.error):
        _raise(category)
    if not math.isfinite(canonical):
        _raise(category)
    return canonical


def _canonical_unit_vector(raw: object, dimensions: int, *, category: str) -> tuple[float, ...]:
    if type(raw) not in (list, tuple) or len(raw) != dimensions:
        _raise(category)
    values = tuple(_float32(value, category) for value in raw)
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or norm <= 1e-12:
        _raise(category)
    first = tuple(_float32(value / norm, category) for value in values)
    norm2 = math.sqrt(sum(value * value for value in first))
    if not math.isfinite(norm2) or norm2 <= 1e-12:
        _raise(category)
    result = tuple(_float32(value / norm2, category) for value in first)
    final_norm = math.sqrt(sum(value * value for value in result))
    if not math.isfinite(final_norm) or abs(final_norm - 1.0) > 1e-5:
        _raise(category)
    return result


def _validated_atomics(atomics: object) -> tuple[hierarchy.AtomicMemoryProjectionInputV1, ...]:
    try:
        validated, _ = hierarchy._validate_atomics(atomics)
    except hierarchy.MemoryHierarchyProjectionError:
        _raise("invalid_atomics")
    return validated


def _eligible_atomics(
    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
) -> tuple[hierarchy.AtomicMemoryProjectionInputV1, ...]:
    return tuple(
        item
        for item in atomics
        if (
            item.status == "active"
            and item.scope_type == "global_user"
            and item.scope_ref == ""
            and item.sensitivity == "normal"
        )
    )


def _validate_provider_batch(
    raw: object,
    expected_count: int,
    dimensions: int,
) -> tuple[tuple[float, ...], ...]:
    if type(raw) not in (list, tuple) or len(raw) != expected_count:
        _raise("embedding_invalid_output")
    return tuple(
        _canonical_unit_vector(vector, dimensions, category="embedding_invalid_output")
        for vector in raw
    )


async def build_vector_index_v1(
    embedding_callable: object,
    atomics: object,
    *,
    source_snapshot_digest: object,
    embedding_model: object,
    dimensions: object,
) -> VectorIndexBuildResultV1:
    """Embed the complete eligible Atomic subset without exposing Memory keys."""

    if not callable(embedding_callable):
        _raise("invalid_embedding_callable")
    model, dims = _validate_model_and_dimensions(embedding_model, dimensions)
    source_digest = _validate_source_digest(source_snapshot_digest)
    eligible = _eligible_atomics(_validated_atomics(atomics))

    documents: list[VectorDocumentPlanV1] = []
    provider_calls = 0
    for start in range(0, len(eligible), MAX_EMBEDDING_BATCH):
        batch = eligible[start : start + MAX_EMBEDDING_BATCH]
        texts = tuple(item.normalized_content for item in batch)
        try:
            async with asyncio.timeout(EMBEDDING_TIMEOUT_SECONDS):
                raw_vectors = await embedding_callable(texts, model, dims)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            _raise("embedding_timeout")
        except MemoryRetrievalVectorError:
            raise
        except Exception:
            _raise("embedding_unavailable")
        provider_calls += 1
        vectors = _validate_provider_batch(raw_vectors, len(batch), dims)
        for item, vector in zip(batch, vectors, strict=True):
            documents.append(VectorDocumentPlanV1(
                memory_key=item.memory_key,
                atomic_revision_digest=hierarchy._atomic_revision_digest(item),
                vector=vector,
            ))

    documents.sort(key=lambda item: item.memory_key)
    plan = VectorIndexPlanV1(
        contract_version=VECTOR_CONTRACT_VERSION,
        embedding_contract_version=EMBEDDING_CONTRACT_VERSION,
        source_snapshot_digest=source_digest,
        embedding_model=model,
        dimensions=dims,
        documents=tuple(documents),
    )
    return VectorIndexBuildResultV1(
        plan=validate_vector_index_plan_v1(plan),
        provider_call_count=provider_calls,
    )


def validate_vector_index_plan_v1(raw: object) -> VectorIndexPlanV1:
    if type(raw) is not VectorIndexPlanV1:
        _raise("invalid_index_plan")
    model, dims = _validate_model_and_dimensions(
        raw.embedding_model,
        raw.dimensions,
    )
    if (
        raw.contract_version != VECTOR_CONTRACT_VERSION
        or raw.embedding_contract_version != EMBEDDING_CONTRACT_VERSION
        or _DIGEST_PATTERN.fullmatch(raw.source_snapshot_digest or "") is None
        or type(raw.documents) is not tuple
        or len(raw.documents) > hierarchy.MAX_ATOMICS
    ):
        _raise("invalid_index_plan")
    previous_key = ""
    for document in raw.documents:
        if (
            type(document) is not VectorDocumentPlanV1
            or type(document.memory_key) is not str
            or _MEMORY_KEY_PATTERN.fullmatch(document.memory_key) is None
            or document.memory_key <= previous_key
            or type(document.atomic_revision_digest) is not str
            or _DIGEST_PATTERN.fullmatch(document.atomic_revision_digest) is None
            or type(document.vector) is not tuple
        ):
            _raise("invalid_index_plan")
        previous_key = document.memory_key
        canonical = _canonical_unit_vector(
            document.vector,
            dims,
            category="invalid_index_plan",
        )
        if canonical != document.vector:
            _raise("invalid_index_plan")
    if model != raw.embedding_model:
        _raise("invalid_index_plan")
    return raw


async def embed_query_vector_v1(
    embedding_callable: object,
    query_text: object,
    *,
    embedding_model: object,
    dimensions: object,
) -> QueryVectorV1:
    if not callable(embedding_callable):
        _raise("invalid_embedding_callable")
    model, dims = _validate_model_and_dimensions(embedding_model, dimensions)
    if type(query_text) is not str or not query_text.strip() or len(query_text) > MAX_QUERY_CHARS:
        _raise("invalid_query")
    try:
        query_text.encode("utf-8", errors="strict")
    except UnicodeError:
        _raise("invalid_query")
    try:
        async with asyncio.timeout(EMBEDDING_TIMEOUT_SECONDS):
            raw = await embedding_callable((query_text,), model, dims)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        _raise("embedding_timeout")
    except MemoryRetrievalVectorError:
        raise
    except Exception:
        _raise("embedding_unavailable")
    vectors = _validate_provider_batch(raw, 1, dims)
    return QueryVectorV1(
        embedding_model=model,
        dimensions=dims,
        vector=vectors[0],
    )


def search_vector_index_v1(
    index_plan: object,
    query_vector: object,
    *,
    max_hits: object = MAX_VECTOR_HITS,
    minimum_similarity: object = 0.0,
) -> VectorSearchResultV1:
    plan = validate_vector_index_plan_v1(index_plan)
    if type(query_vector) is not QueryVectorV1:
        _raise("invalid_query")
    if (
        query_vector.embedding_model != plan.embedding_model
        or query_vector.dimensions != plan.dimensions
        or type(query_vector.vector) is not tuple
    ):
        _raise("invalid_query")
    canonical_query = _canonical_unit_vector(
        query_vector.vector,
        plan.dimensions,
        category="invalid_query",
    )
    if canonical_query != query_vector.vector:
        _raise("invalid_query")
    if (
        type(max_hits) is not int
        or isinstance(max_hits, bool)
        or not 1 <= max_hits <= MAX_VECTOR_HITS
        or isinstance(minimum_similarity, bool)
        or not isinstance(minimum_similarity, (int, float))
        or not math.isfinite(float(minimum_similarity))
        or not -1.0 <= float(minimum_similarity) <= 1.0
    ):
        _raise("invalid_query")
    threshold = float(minimum_similarity)

    hits: list[VectorSearchHitV1] = []
    for document in plan.documents:
        similarity = sum(
            left * right
            for left, right in zip(document.vector, canonical_query, strict=True)
        )
        similarity = max(-1.0, min(1.0, similarity))
        if similarity <= threshold:
            continue
        hits.append(VectorSearchHitV1(
            memory_key=document.memory_key,
            similarity=round(similarity, 12),
        ))
    hits.sort(key=lambda item: (-item.similarity, item.memory_key))
    return VectorSearchResultV1(
        hits=tuple(hits[:max_hits]),
        indexed_document_count=plan.document_count,
    )
