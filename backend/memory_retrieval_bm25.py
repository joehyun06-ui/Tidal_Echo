"""Deterministic BM25 lexical foundation for Phase 4D-C1.

This module owns no Memory authority and performs no I/O.  It accepts a complete
already-proved active Atomic snapshot, indexes only normal global-user memories,
and turns lexical terms into domain-separated HMAC digests before they enter an
index plan.  Plaintext Atomic content and plaintext lexical terms never survive
in the returned plan.

The resulting plan is disposable routing data.  Search returns only Memory keys
plus bounded scores; any later prompt/context consumer must resolve and re-prove
those keys against authoritative Atomic Memory.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Final

from backend import deployment_config
from backend import memory_hierarchy_projection as hierarchy


BM25_CONTRACT_VERSION: Final = "memory-retrieval-bm25-v1"
TOKENIZER_VERSION: Final = "memory-retrieval-bm25-tokenizer-v1"
TERM_HASH_DOMAIN: Final = b"memory-retrieval-bm25-term-v1\x00"
TERM_HASH_BYTES: Final = hashlib.sha256().digest_size
MAX_QUERY_CHARS: Final = 32_000
MAX_HITS: Final = 20
MAX_DOCUMENT_TERMS: Final = 8_192
BM25_K1: Final = 1.2
BM25_B: Final = 0.75

_DIGEST_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")
_KEY_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MEMORY_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")

_CJK_RANGES: Final = (
    (0x1100, 0x11FF),
    (0x3040, 0x309F),
    (0x30A0, 0x30FF),
    (0x3130, 0x318F),
    (0x31F0, 0x31FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xA960, 0xA97F),
    (0xAC00, 0xD7AF),
    (0xD7B0, 0xD7FF),
    (0xF900, 0xFAFF),
    (0xFF66, 0xFF9D),
    (0x1B000, 0x1B0FF),
    (0x1AFF0, 0x1AFFF),
    (0x20000, 0x2A6DF),
    (0x2A700, 0x2B73F),
    (0x2B740, 0x2B81F),
    (0x2B820, 0x2CEAF),
    (0x2CEB0, 0x2EBEF),
    (0x2EBF0, 0x2EE5F),
    (0x2F800, 0x2FA1F),
    (0x30000, 0x3134F),
    (0x31350, 0x323AF),
    (0x323B0, 0x3347F),
)

_ERROR_CATEGORIES: Final = frozenset({
    "invalid_atomics",
    "invalid_index_plan",
    "invalid_query",
    "invalid_term_key",
    "memory_retrieval_bm25_error",
})


class MemoryRetrievalBM25Error(ValueError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_retrieval_bm25_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "memory_retrieval_bm25_error"

    def __repr__(self) -> str:
        return f"MemoryRetrievalBM25Error({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryRetrievalBM25Error(category)


@dataclass(frozen=True, slots=True, repr=False)
class BM25PostingPlanV1:
    term_hash: bytes = field(repr=False)
    term_frequency: int

    def __repr__(self) -> str:
        return f"<BM25PostingPlanV1 tf={self.term_frequency}>"


@dataclass(frozen=True, slots=True, repr=False)
class BM25DocumentPlanV1:
    memory_key: str = field(repr=False)
    document_length: int
    postings: tuple[BM25PostingPlanV1, ...] = field(repr=False)

    def __repr__(self) -> str:
        return (
            "<BM25DocumentPlanV1 "
            f"length={self.document_length} terms={len(self.postings)}>"
        )


@dataclass(frozen=True, slots=True, repr=False)
class BM25IndexPlanV1:
    contract_version: str
    tokenizer_version: str
    source_snapshot_digest: str = field(repr=False)
    term_key_id: str = field(repr=False)
    documents: tuple[BM25DocumentPlanV1, ...] = field(repr=False)
    term_document_frequencies: tuple[tuple[bytes, int], ...] = field(repr=False)

    @property
    def document_count(self) -> int:
        return len(self.documents)

    @property
    def total_document_length(self) -> int:
        return sum(document.document_length for document in self.documents)

    @property
    def unique_term_count(self) -> int:
        return len(self.term_document_frequencies)

    @property
    def posting_count(self) -> int:
        return sum(len(document.postings) for document in self.documents)

    def __repr__(self) -> str:
        return (
            "<BM25IndexPlanV1 "
            f"documents={self.document_count} terms={self.unique_term_count} "
            f"postings={self.posting_count}>"
        )


@dataclass(frozen=True, slots=True, repr=False)
class BM25SearchHitV1:
    memory_key: str = field(repr=False)
    score: float
    matched_term_count: int

    def __repr__(self) -> str:
        return (
            "<BM25SearchHitV1 "
            f"score={self.score:.6f} matched_terms={self.matched_term_count}>"
        )


@dataclass(frozen=True, slots=True, repr=False)
class BM25SearchResultV1:
    hits: tuple[BM25SearchHitV1, ...] = field(repr=False)
    query_term_count: int
    indexed_document_count: int

    def __repr__(self) -> str:
        return (
            "<BM25SearchResultV1 "
            f"hits={len(self.hits)} query_terms={self.query_term_count} "
            f"documents={self.indexed_document_count}>"
        )


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return any(start <= codepoint <= end for start, end in _CJK_RANGES)


def _normalize_text(value: object, *, category: str) -> str:
    if type(value) is not str:
        _raise(category)
    try:
        value.encode("utf-8", errors="strict")
        normalized = unicodedata.normalize("NFC", value).casefold()
    except Exception:
        _raise(category)
    return normalized.replace("\r\n", "\n").replace("\r", "\n")


def tokenize_lexical_terms_v1(value: object, *, query: bool = False) -> tuple[str, ...]:
    """Return deterministic namespaced lexical terms, preserving term frequency."""

    category = "invalid_query" if query else "invalid_atomics"
    normalized = _normalize_text(value, category=category)
    if query and len(normalized) > MAX_QUERY_CHARS:
        _raise("invalid_query")

    terms: list[str] = []
    alphanumeric_run: list[str] = []
    cjk_run: list[str] = []

    def flush_alphanumeric() -> None:
        if not alphanumeric_run:
            return
        token = "".join(alphanumeric_run)
        alphanumeric_run.clear()
        if token:
            terms.append("a:" + token)

    def flush_cjk() -> None:
        if not cjk_run:
            return
        run = tuple(cjk_run)
        cjk_run.clear()
        terms.extend("c:" + character for character in run)
        terms.extend(
            "b:" + run[index] + run[index + 1]
            for index in range(len(run) - 1)
        )

    for character in normalized:
        if _is_cjk(character):
            flush_alphanumeric()
            cjk_run.append(character)
            continue
        flush_cjk()
        category_name = unicodedata.category(character)
        if category_name[:1] in {"L", "N"}:
            alphanumeric_run.append(character)
        else:
            flush_alphanumeric()

    flush_alphanumeric()
    flush_cjk()
    if len(terms) > MAX_DOCUMENT_TERMS and not query:
        _raise("invalid_atomics")
    return tuple(terms)


def _validate_term_key(term_key_id: object, term_hmac_secret: object) -> tuple[str, bytes]:
    if (
        type(term_key_id) is not str
        or _KEY_ID_PATTERN.fullmatch(term_key_id) is None
        or type(term_hmac_secret) is not str
        or not deployment_config.memory_fingerprint_secret_is_strong(
            term_hmac_secret
        )
    ):
        _raise("invalid_term_key")
    try:
        secret = term_hmac_secret.encode("ascii", errors="strict")
    except Exception:
        _raise("invalid_term_key")
    return term_key_id, secret


def _term_hash(secret: bytes, term: str) -> bytes:
    try:
        encoded = term.encode("utf-8", errors="strict")
    except Exception:
        _raise("invalid_atomics")
    return hmac.new(secret, TERM_HASH_DOMAIN + encoded, hashlib.sha256).digest()


def _validate_source_digest(value: object) -> str:
    if type(value) is not str or _DIGEST_PATTERN.fullmatch(value) is None:
        _raise("invalid_index_plan")
    return value


def build_bm25_index_v1(
    atomics: object,
    *,
    source_snapshot_digest: object,
    term_key_id: object,
    term_hmac_secret: object,
) -> BM25IndexPlanV1:
    """Build a plaintext-free index plan from proved active Atomic Memory."""

    try:
        validated, _ = hierarchy._validate_atomics(atomics)
    except hierarchy.MemoryHierarchyProjectionError:
        _raise("invalid_atomics")
    source_digest = _validate_source_digest(source_snapshot_digest)
    key_id, secret = _validate_term_key(term_key_id, term_hmac_secret)

    documents: list[BM25DocumentPlanV1] = []
    document_frequency: Counter[bytes] = Counter()

    for atomic in validated:
        if atomic.scope_type != "global_user" or atomic.sensitivity != "normal":
            continue
        terms = tokenize_lexical_terms_v1(atomic.normalized_content)
        if not terms:
            continue
        term_frequencies = Counter(_term_hash(secret, term) for term in terms)
        postings = tuple(
            BM25PostingPlanV1(term_hash, frequency)
            for term_hash, frequency in sorted(term_frequencies.items())
        )
        document = BM25DocumentPlanV1(
            memory_key=atomic.memory_key,
            document_length=len(terms),
            postings=postings,
        )
        documents.append(document)
        document_frequency.update(term_frequencies.keys())

    documents.sort(key=lambda item: item.memory_key)
    plan = BM25IndexPlanV1(
        contract_version=BM25_CONTRACT_VERSION,
        tokenizer_version=TOKENIZER_VERSION,
        source_snapshot_digest=source_digest,
        term_key_id=key_id,
        documents=tuple(documents),
        term_document_frequencies=tuple(sorted(document_frequency.items())),
    )
    return validate_bm25_index_plan_v1(plan)


def validate_bm25_index_plan_v1(raw: object) -> BM25IndexPlanV1:
    if type(raw) is not BM25IndexPlanV1:
        _raise("invalid_index_plan")
    if (
        raw.contract_version != BM25_CONTRACT_VERSION
        or raw.tokenizer_version != TOKENIZER_VERSION
        or _DIGEST_PATTERN.fullmatch(raw.source_snapshot_digest or "") is None
        or _KEY_ID_PATTERN.fullmatch(raw.term_key_id or "") is None
        or type(raw.documents) is not tuple
        or type(raw.term_document_frequencies) is not tuple
        or len(raw.documents) > hierarchy.MAX_ATOMICS
    ):
        _raise("invalid_index_plan")

    previous_key = ""
    recomputed_df: Counter[bytes] = Counter()
    for document in raw.documents:
        if type(document) is not BM25DocumentPlanV1:
            _raise("invalid_index_plan")
        if (
            _MEMORY_KEY_PATTERN.fullmatch(document.memory_key or "") is None
            or document.memory_key <= previous_key
            or type(document.document_length) is not int
            or not 1 <= document.document_length <= MAX_DOCUMENT_TERMS
            or type(document.postings) is not tuple
            or not document.postings
        ):
            _raise("invalid_index_plan")
        previous_key = document.memory_key
        previous_hash = b""
        total_tf = 0
        for posting in document.postings:
            if (
                type(posting) is not BM25PostingPlanV1
                or type(posting.term_hash) is not bytes
                or len(posting.term_hash) != TERM_HASH_BYTES
                or posting.term_hash <= previous_hash
                or type(posting.term_frequency) is not int
                or posting.term_frequency <= 0
            ):
                _raise("invalid_index_plan")
            previous_hash = posting.term_hash
            total_tf += posting.term_frequency
            recomputed_df[posting.term_hash] += 1
        if total_tf != document.document_length:
            _raise("invalid_index_plan")

    expected_df = tuple(sorted(recomputed_df.items()))
    if raw.term_document_frequencies != expected_df:
        _raise("invalid_index_plan")
    for term_hash, count in raw.term_document_frequencies:
        if (
            type(term_hash) is not bytes
            or len(term_hash) != TERM_HASH_BYTES
            or type(count) is not int
            or not 1 <= count <= len(raw.documents)
        ):
            _raise("invalid_index_plan")
    return raw


def search_bm25_index_v1(
    index_plan: object,
    query_text: object,
    *,
    term_key_id: object,
    term_hmac_secret: object,
    max_hits: object = MAX_HITS,
) -> BM25SearchResultV1:
    """Rank indexed Memory keys using Okapi BM25; no Memory content is returned."""

    plan = validate_bm25_index_plan_v1(index_plan)
    key_id, secret = _validate_term_key(term_key_id, term_hmac_secret)
    if key_id != plan.term_key_id:
        _raise("invalid_term_key")
    if (
        type(max_hits) is not int
        or isinstance(max_hits, bool)
        or not 1 <= max_hits <= MAX_HITS
    ):
        _raise("invalid_query")

    normalized_query = _normalize_text(query_text, category="invalid_query")
    if len(normalized_query) > MAX_QUERY_CHARS:
        _raise("invalid_query")
    query_terms = tokenize_lexical_terms_v1(normalized_query, query=True)
    query_hashes = tuple(sorted({
        _term_hash(secret, term) for term in query_terms
    }))
    if not query_hashes or not plan.documents:
        return BM25SearchResultV1(
            hits=(),
            query_term_count=len(query_hashes),
            indexed_document_count=len(plan.documents),
        )

    document_count = len(plan.documents)
    average_length = plan.total_document_length / document_count
    df = dict(plan.term_document_frequencies)
    query_set = set(query_hashes)
    ranked: list[BM25SearchHitV1] = []

    for document in plan.documents:
        posting_map = {
            posting.term_hash: posting.term_frequency
            for posting in document.postings
            if posting.term_hash in query_set
        }
        if not posting_map:
            continue
        score = 0.0
        for term_hash, tf in posting_map.items():
            document_frequency = df.get(term_hash)
            if document_frequency is None:
                _raise("invalid_index_plan")
            idf = math.log(
                1.0
                + (
                    document_count - document_frequency + 0.5
                ) / (document_frequency + 0.5)
            )
            length_norm = 1.0 - BM25_B + (
                BM25_B * document.document_length / average_length
            )
            denominator = tf + BM25_K1 * length_norm
            score += idf * (tf * (BM25_K1 + 1.0)) / denominator
        if not math.isfinite(score) or score <= 0:
            _raise("invalid_index_plan")
        ranked.append(BM25SearchHitV1(
            memory_key=document.memory_key,
            score=round(score, 12),
            matched_term_count=len(posting_map),
        ))

    ranked.sort(
        key=lambda hit: (-hit.score, -hit.matched_term_count, hit.memory_key)
    )
    return BM25SearchResultV1(
        hits=tuple(ranked[:max_hits]),
        query_term_count=len(query_hashes),
        indexed_document_count=document_count,
    )
