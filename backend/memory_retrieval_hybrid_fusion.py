"""Pure explainable hybrid-fusion foundation for Phase 4D-D1.

This module changes no current Memory retrieval or prompt-context authority.
It combines four bounded candidate channels over already-proved active Atomic
Memory:

- a high-precision exact/identifier channel derived from the query,
- the existing Retrieval V2 lexical/CJK ranking semantics,
- a precomputed C1 BM25 result,
- a precomputed C3 vector result.

BM25 scores and cosine similarities are intentionally not mixed directly.
Channels are fused by deterministic rank contribution, then only small bounded
metadata boosts are applied. Exact identifier hits remain a separate priority
tier so technical literals cannot be washed out by semantic ranking.

The output contains Memory keys and structural scoring components only. It owns
no Memory truth, provider, sidecar I/O, hierarchy expansion, prompt rendering,
touch persistence, or production/runtime gate.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final

from backend import memory_hierarchy_projection as hierarchy
from backend import memory_retrieval_bm25 as bm25
from backend import memory_retrieval_hierarchy_routing as hierarchy_routing
from backend import memory_retrieval_v2 as lexical_v2
from backend import memory_retrieval_vector as vector


HYBRID_FUSION_CONTRACT_VERSION: Final = "memory-retrieval-hybrid-fusion-v1"
MAX_HITS: Final = 20
RRF_K: Final = 10
EXACT_CHANNEL_WEIGHT: Final = 2.0
LEXICAL_CHANNEL_WEIGHT: Final = 1.0
BM25_CHANNEL_WEIGHT: Final = 1.0
VECTOR_CHANNEL_WEIGHT: Final = 1.0
_TOTAL_CHANNEL_WEIGHT: Final = (
    EXACT_CHANNEL_WEIGHT
    + LEXICAL_CHANNEL_WEIGHT
    + BM25_CHANNEL_WEIGHT
    + VECTOR_CHANNEL_WEIGHT
)
MAX_CONFIDENCE_BOOST: Final = 0.05
MAX_RECENCY_BOOST: Final = 0.03
MAX_TOUCH_BOOST: Final = 0.02
RECENCY_HALF_LIFE_DAYS: Final = 180.0
TOUCH_COUNT_CAP: Final = 100
MAX_TOUCH_COUNT_INPUT: Final = 1_000_000_000
MAX_TOUCH_HINTS: Final = hierarchy.MAX_ATOMICS
MAX_EXACT_TERMS: Final = 16
MAX_EXACT_TERM_CHARS: Final = 128

_MEMORY_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_TECHNICAL_TOKEN_PATTERN: Final = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:/_-]{3,127}"
)
_ERROR_CATEGORIES: Final = frozenset({
    "invalid_atomics",
    "invalid_bm25_result",
    "invalid_query",
    "invalid_reference_time",
    "invalid_touch_hints",
    "invalid_vector_result",
    "memory_retrieval_hybrid_fusion_error",
})


class MemoryRetrievalHybridFusionError(ValueError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_retrieval_hybrid_fusion_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "memory_retrieval_hybrid_fusion_error"

    def __repr__(self) -> str:
        return f"MemoryRetrievalHybridFusionError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryRetrievalHybridFusionError(category)


@dataclass(frozen=True, slots=True, repr=False)
class TouchHintV1:
    memory_key: str = field(repr=False)
    recall_count: int

    def __repr__(self) -> str:
        return f"<TouchHintV1 recall_count={self.recall_count}>"


@dataclass(frozen=True, slots=True, repr=False)
class HybridFusionHitV1:
    memory_key: str = field(repr=False)
    exact_rank: int | None
    lexical_rank: int | None
    bm25_rank: int | None
    vector_rank: int | None
    exact_match_count: int
    channel_count: int
    rank_fusion_score: float
    confidence_boost: float
    recency_boost: float
    touch_boost: float
    final_score: float

    def __repr__(self) -> str:
        return (
            "<HybridFusionHitV1 "
            f"channels={self.channel_count} exact_matches={self.exact_match_count} "
            f"score={self.final_score:.6f}>"
        )


@dataclass(frozen=True, slots=True, repr=False)
class HybridFusionResultV1:
    contract_version: str
    hits: tuple[HybridFusionHitV1, ...] = field(repr=False)
    eligible_atomic_count: int
    exact_hit_count: int
    lexical_hit_count: int
    bm25_hit_count: int
    vector_hit_count: int
    touch_hint_count: int
    bm25_available: bool
    vector_available: bool

    def __repr__(self) -> str:
        return (
            "<HybridFusionResultV1 "
            f"hits={len(self.hits)} eligible={self.eligible_atomic_count} "
            f"exact={self.exact_hit_count} lexical={self.lexical_hit_count} "
            f"bm25={self.bm25_hit_count} vector={self.vector_hit_count} "
            f"touch_hints={self.touch_hint_count} "
            f"bm25_available={self.bm25_available} "
            f"vector_available={self.vector_available}>"
        )


def _validated_atomics(
    atomics: object,
) -> tuple[
    tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
    dict[str, hierarchy.AtomicMemoryProjectionInputV1],
]:
    try:
        validated, _ = hierarchy._validate_atomics(atomics)
    except hierarchy.MemoryHierarchyProjectionError:
        _raise("invalid_atomics")
    eligible = tuple(
        item
        for item in validated
        if (
            item.status == "active"
            and item.scope_type == "global_user"
            and item.scope_ref == ""
            and item.sensitivity == "normal"
        )
    )
    return eligible, {item.memory_key: item for item in eligible}


def _validated_query(value: object) -> str:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > lexical_v2.QUERY_MAX_CHARS
    ):
        _raise("invalid_query")
    try:
        value.encode("utf-8", errors="strict")
    except Exception:
        _raise("invalid_query")
    return value


def _parse_reference_time(value: object) -> datetime:
    if type(value) is not str or not value:
        _raise("invalid_reference_time")
    try:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(text)
    except Exception:
        _raise("invalid_reference_time")
    if parsed.tzinfo is None:
        _raise("invalid_reference_time")
    return parsed.astimezone(timezone.utc)


def _parse_optional_atomic_time(value: str) -> datetime | None:
    try:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _looks_technical_literal(token: str) -> bool:
    return (
        any(character.isdigit() for character in token)
        or any(character in "._:/-" for character in token)
        or ("_" in token)
        or (token.isupper() and len(token) >= 4)
    )


def _exact_terms(query_text: str) -> tuple[str, ...]:
    try:
        normalized = unicodedata.normalize("NFC", query_text)
    except Exception:
        _raise("invalid_query")
    seen: set[str] = set()
    terms: list[str] = []
    for match in _TECHNICAL_TOKEN_PATTERN.finditer(normalized):
        token = match.group(0)
        if len(token) > MAX_EXACT_TERM_CHARS or not _looks_technical_literal(token):
            continue
        folded = token.casefold()
        if folded not in seen:
            seen.add(folded)
            terms.append(folded)
        if len(terms) == MAX_EXACT_TERMS:
            break
    return tuple(terms)


def _content_has_exact_term(content: str, term: str) -> bool:
    try:
        folded = unicodedata.normalize("NFC", content).casefold()
    except Exception:
        _raise("invalid_atomics")
    pattern = re.compile(
        r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])"
    )
    return pattern.search(folded) is not None


def _exact_channel(
    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
    query_text: str,
) -> tuple[tuple[str, int], ...]:
    terms = _exact_terms(query_text)
    if not terms:
        return ()
    ranked: list[tuple[int, int, str]] = []
    for atomic in atomics:
        matches = tuple(
            term
            for term in terms
            if _content_has_exact_term(atomic.normalized_content, term)
        )
        if not matches:
            continue
        ranked.append((
            -len(matches),
            -max(len(term) for term in matches),
            atomic.memory_key,
        ))
    ranked.sort()
    return tuple((memory_key, -match_count) for match_count, _length, memory_key in ranked)


def _lexical_channel(
    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...],
    query_text: str,
) -> tuple[str, ...]:
    try:
        normalized_query = lexical_v2._normalize_for_retrieval(
            query_text,
            category="invalid_query",
        )
        query_signals = lexical_v2._usable_signals(
            lexical_v2._text_signals(normalized_query)
        )
        if not (query_signals.alphanumeric or query_signals.cjk_bigrams):
            return ()
        candidates = tuple(
            lexical_v2._ValidatedCandidate(
                item={"memory_key": item.memory_key},
                content=item.normalized_content,
                explicitness=item.explicitness,
                confidence=item.confidence,
                position=position,
            )
            for position, item in enumerate(atomics)
        )
        ranked = lexical_v2._rank_candidates(
            candidates,
            normalized_query=normalized_query,
            query_signals=query_signals,
        )
        return tuple(entry.candidate.item["memory_key"] for entry in ranked)
    except lexical_v2.MemoryRetrievalV2Error:
        _raise("invalid_query")
    except Exception:
        _raise("invalid_atomics")


def _validated_bm25(
    raw: object,
    eligible_keys: frozenset[str],
) -> bm25.BM25SearchResultV1 | None:
    if raw is None:
        return None
    try:
        result = hierarchy_routing._validated_bm25_result(raw)
    except hierarchy_routing.MemoryRetrievalHierarchyRoutingError:
        _raise("invalid_bm25_result")
    if result.indexed_document_count > len(eligible_keys):
        _raise("invalid_bm25_result")
    if any(hit.memory_key not in eligible_keys for hit in result.hits):
        _raise("invalid_bm25_result")
    return result


def _validated_vector(
    raw: object,
    eligible_keys: frozenset[str],
) -> vector.VectorSearchResultV1 | None:
    if raw is None:
        return None
    if type(raw) is not vector.VectorSearchResultV1:
        _raise("invalid_vector_result")
    if (
        type(raw.hits) is not tuple
        or len(raw.hits) > vector.MAX_VECTOR_HITS
        or type(raw.indexed_document_count) is not int
        or isinstance(raw.indexed_document_count, bool)
        or not 0 <= raw.indexed_document_count <= len(eligible_keys)
    ):
        _raise("invalid_vector_result")
    seen: set[str] = set()
    canonical: list[tuple[float, str]] = []
    for hit in raw.hits:
        if (
            type(hit) is not vector.VectorSearchHitV1
            or type(hit.memory_key) is not str
            or _MEMORY_KEY_PATTERN.fullmatch(hit.memory_key) is None
            or hit.memory_key not in eligible_keys
            or hit.memory_key in seen
            or type(hit.similarity) is not float
            or not math.isfinite(hit.similarity)
            or not 0.0 < hit.similarity <= 1.0
        ):
            _raise("invalid_vector_result")
        seen.add(hit.memory_key)
        canonical.append((hit.similarity, hit.memory_key))
    if canonical != sorted(canonical, key=lambda item: (-item[0], item[1])):
        _raise("invalid_vector_result")
    return raw


def _validated_touch_hints(
    raw: object,
    eligible_keys: frozenset[str],
) -> dict[str, int]:
    if raw is None:
        return {}
    if type(raw) not in (list, tuple) or len(raw) > MAX_TOUCH_HINTS:
        _raise("invalid_touch_hints")
    result: dict[str, int] = {}
    for item in raw:
        if (
            type(item) is not TouchHintV1
            or type(item.memory_key) is not str
            or _MEMORY_KEY_PATTERN.fullmatch(item.memory_key) is None
            or item.memory_key not in eligible_keys
            or item.memory_key in result
            or type(item.recall_count) is not int
            or isinstance(item.recall_count, bool)
            or not 0 <= item.recall_count <= MAX_TOUCH_COUNT_INPUT
        ):
            _raise("invalid_touch_hints")
        result[item.memory_key] = item.recall_count
    return result


def _rank_unit(rank: int) -> float:
    return (RRF_K + 1.0) / (RRF_K + float(rank))


def _rank_component(rank: int | None, weight: float) -> float:
    return 0.0 if rank is None else (weight * _rank_unit(rank)) / _TOTAL_CHANNEL_WEIGHT


def _confidence_boost(item: hierarchy.AtomicMemoryProjectionInputV1) -> float:
    return MAX_CONFIDENCE_BOOST * float(item.confidence)


def _recency_boost(
    item: hierarchy.AtomicMemoryProjectionInputV1,
    reference_time: datetime,
) -> float:
    confirmed = _parse_optional_atomic_time(item.last_confirmed_at)
    if confirmed is None or confirmed > reference_time:
        return 0.0
    age_days = (reference_time - confirmed).total_seconds() / 86400.0
    if age_days < 0.0 or not math.isfinite(age_days):
        return 0.0
    return MAX_RECENCY_BOOST * (0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS))


def _touch_boost(count: int) -> float:
    capped = min(count, TOUCH_COUNT_CAP)
    if capped <= 0:
        return 0.0
    return MAX_TOUCH_BOOST * (
        math.log1p(capped) / math.log1p(TOUCH_COUNT_CAP)
    )


def fuse_hybrid_retrieval_v1(
    atomics: object,
    *,
    query_text: object,
    bm25_result: object,
    vector_result: object,
    reference_time: object,
    touch_hints: object = (),
    max_hits: object = MAX_HITS,
) -> HybridFusionResultV1:
    """Fuse exact, current lexical, BM25 and vector ranks without changing authority."""

    eligible, by_key = _validated_atomics(atomics)
    query = _validated_query(query_text)
    if (
        type(max_hits) is not int
        or isinstance(max_hits, bool)
        or not 1 <= max_hits <= MAX_HITS
    ):
        _raise("invalid_query")
    now = _parse_reference_time(reference_time)
    eligible_keys = frozenset(by_key)

    exact_items = _exact_channel(eligible, query)
    lexical_keys = _lexical_channel(eligible, query)
    sparse = _validated_bm25(bm25_result, eligible_keys)
    semantic = _validated_vector(vector_result, eligible_keys)
    touches = _validated_touch_hints(touch_hints, eligible_keys)

    exact_rank = {
        memory_key: rank
        for rank, (memory_key, _count) in enumerate(exact_items, start=1)
    }
    exact_count = dict(exact_items)
    lexical_rank = {
        memory_key: rank
        for rank, memory_key in enumerate(lexical_keys, start=1)
    }
    bm25_rank = {
        hit.memory_key: rank
        for rank, hit in enumerate(sparse.hits, start=1)
    } if sparse is not None else {}
    vector_rank = {
        hit.memory_key: rank
        for rank, hit in enumerate(semantic.hits, start=1)
    } if semantic is not None else {}

    union = set(exact_rank) | set(lexical_rank) | set(bm25_rank) | set(vector_rank)
    hits: list[HybridFusionHitV1] = []
    for memory_key in union:
        atomic = by_key[memory_key]
        erank = exact_rank.get(memory_key)
        lrank = lexical_rank.get(memory_key)
        brank = bm25_rank.get(memory_key)
        vrank = vector_rank.get(memory_key)
        rank_score = (
            _rank_component(erank, EXACT_CHANNEL_WEIGHT)
            + _rank_component(lrank, LEXICAL_CHANNEL_WEIGHT)
            + _rank_component(brank, BM25_CHANNEL_WEIGHT)
            + _rank_component(vrank, VECTOR_CHANNEL_WEIGHT)
        )
        confidence = _confidence_boost(atomic)
        recency = _recency_boost(atomic, now)
        touch = _touch_boost(touches.get(memory_key, 0))
        final = rank_score + confidence + recency + touch
        hits.append(HybridFusionHitV1(
            memory_key=memory_key,
            exact_rank=erank,
            lexical_rank=lrank,
            bm25_rank=brank,
            vector_rank=vrank,
            exact_match_count=exact_count.get(memory_key, 0),
            channel_count=sum(
                rank is not None for rank in (erank, lrank, brank, vrank)
            ),
            rank_fusion_score=round(rank_score, 12),
            confidence_boost=round(confidence, 12),
            recency_boost=round(recency, 12),
            touch_boost=round(touch, 12),
            final_score=round(final, 12),
        ))

    hits.sort(key=lambda item: (
        0 if item.exact_rank is not None else 1,
        -item.final_score,
        -item.channel_count,
        item.memory_key,
    ))
    return HybridFusionResultV1(
        contract_version=HYBRID_FUSION_CONTRACT_VERSION,
        hits=tuple(hits[:max_hits]),
        eligible_atomic_count=len(eligible),
        exact_hit_count=len(exact_rank),
        lexical_hit_count=len(lexical_rank),
        bm25_hit_count=len(sparse.hits) if sparse is not None else 0,
        vector_hit_count=len(semantic.hits) if semantic is not None else 0,
        touch_hint_count=len(touches),
        bm25_available=sparse is not None,
        vector_available=semantic is not None,
    )
