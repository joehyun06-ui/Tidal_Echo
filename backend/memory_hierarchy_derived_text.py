"""Digest-bound derived text contract for Phase 4D-B6.

Derived text is disposable projection cache, never Memory truth.  A document is
bound to one already-proved hierarchy node by ``node_key`` / ``node_type`` /
``projection_digest`` and consists only of short model-authored sentences plus
server-validated Atomic Memory support keys.  The server joins sentence text and
computes the content digest; the model cannot author a separate summary blob,
node identity, projection digest, title, confidence, entities, or Memory writes.

A changed hierarchy digest makes an old document stale by construction.  This
module performs no I/O, persistence, provider call, retrieval, or Memory write.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Final

from backend import memory_hierarchy_projection as hierarchy


DERIVED_TEXT_CONTRACT_VERSION: Final = "memory-hierarchy-derived-text-v1"
MAX_SENTENCES: Final = 8
MAX_SENTENCE_CHARS: Final = 320
MAX_TEXT_CHARS: Final = 1600
MAX_SUPPORT_KEYS_PER_SENTENCE: Final = 16
MAX_TOTAL_SUPPORT_REFS: Final = 64

_NODE_TYPES: Final = frozenset({"topic", "episode", "canonical_state"})
_NODE_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MEMORY_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_DIGEST_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")

_ERROR_CATEGORIES: Final = frozenset({
    "duplicate_sentence",
    "invalid_atomics",
    "invalid_derived_sentence",
    "invalid_node_binding",
    "memory_hierarchy_derived_text_error",
    "node_member_mismatch",
    "text_budget_exceeded",
    "too_many_sentences",
    "too_many_support_refs",
    "unknown_support_key",
})


class MemoryHierarchyDerivedTextError(ValueError):
    """Stable, data-free derived-text contract failure."""

    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_hierarchy_derived_text_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "memory_hierarchy_derived_text_error"

    def __repr__(self) -> str:
        return f"MemoryHierarchyDerivedTextError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryHierarchyDerivedTextError(category)


@dataclass(frozen=True, slots=True, repr=False)
class DerivedTextNodeBindingV1:
    """Content-free binding to one current hierarchy projection node."""

    node_type: str
    node_key: str
    parent_key: str
    projection_digest: str = field(repr=False)
    atomic_keys: tuple[str, ...] = field(repr=False)

    def __repr__(self) -> str:
        return (
            "<DerivedTextNodeBindingV1 "
            f"type={self.node_type!r} key={self.node_key!r}>"
        )

    def receipt(self) -> hierarchy.ProjectionNodeReceiptV1:
        return hierarchy.ProjectionNodeReceiptV1(
            node_type=self.node_type,
            node_key=self.node_key,
            parent_key=self.parent_key,
            projection_digest=self.projection_digest,
        )


@dataclass(frozen=True, slots=True, repr=False)
class DerivedTextSentenceV1:
    """One short derived sentence with explicit Atomic support references."""

    text: str = field(repr=False)
    support_keys: tuple[str, ...] = field(repr=False)

    def __repr__(self) -> str:
        return f"<DerivedTextSentenceV1 supports={len(self.support_keys)}>"


@dataclass(frozen=True, slots=True, repr=False)
class DerivedTextDocumentV1:
    """Immutable derived text bound to one exact hierarchy node revision."""

    contract_version: str
    node_type: str
    node_key: str
    parent_key: str
    projection_digest: str = field(repr=False)
    content_digest: str = field(repr=False)
    text: str = field(repr=False)
    sentences: tuple[DerivedTextSentenceV1, ...] = field(repr=False)

    def __repr__(self) -> str:
        return (
            "<DerivedTextDocumentV1 "
            f"type={self.node_type!r} key={self.node_key!r} "
            f"sentences={len(self.sentences)}>"
        )

    @property
    def support_keys(self) -> tuple[str, ...]:
        return tuple(sorted({
            key
            for sentence in self.sentences
            for key in sentence.support_keys
        }))

    def receipt(self) -> hierarchy.ProjectionNodeReceiptV1:
        return hierarchy.ProjectionNodeReceiptV1(
            node_type=self.node_type,
            node_key=self.node_key,
            parent_key=self.parent_key,
            projection_digest=self.projection_digest,
        )


def _valid_node_key(value: object) -> str:
    if type(value) is not str or _NODE_KEY_PATTERN.fullmatch(value) is None:
        _raise("invalid_node_binding")
    return value


def _valid_memory_key(value: object) -> str:
    if type(value) is not str or _MEMORY_KEY_PATTERN.fullmatch(value) is None:
        _raise("invalid_node_binding")
    return value


def _validate_binding(raw: object) -> DerivedTextNodeBindingV1:
    if type(raw) is not DerivedTextNodeBindingV1:
        _raise("invalid_node_binding")
    if raw.node_type not in _NODE_TYPES:
        _raise("invalid_node_binding")
    node_key = _valid_node_key(raw.node_key)
    if type(raw.parent_key) is not str:
        _raise("invalid_node_binding")
    parent_key = raw.parent_key
    if parent_key:
        _valid_node_key(parent_key)
    if raw.node_type == "topic":
        if parent_key:
            _raise("invalid_node_binding")
    elif not parent_key:
        _raise("invalid_node_binding")
    if type(raw.projection_digest) is not str or _DIGEST_PATTERN.fullmatch(
        raw.projection_digest
    ) is None:
        _raise("invalid_node_binding")
    if type(raw.atomic_keys) is not tuple or not raw.atomic_keys:
        _raise("invalid_node_binding")
    members = tuple(sorted(raw.atomic_keys))
    if len(set(members)) != len(members):
        _raise("invalid_node_binding")
    for memory_key in members:
        _valid_memory_key(memory_key)
    return DerivedTextNodeBindingV1(
        node_type=raw.node_type,
        node_key=node_key,
        parent_key=parent_key,
        projection_digest=raw.projection_digest,
        atomic_keys=members,
    )


def binding_from_projection_node_v1(raw_node: object) -> DerivedTextNodeBindingV1:
    """Create one content-free binding from a planner-owned node object."""

    if type(raw_node) is not hierarchy.ProjectionNodePlanV1:
        _raise("invalid_node_binding")
    return _validate_binding(DerivedTextNodeBindingV1(
        node_type=raw_node.node_type,
        node_key=raw_node.node_key,
        parent_key=raw_node.parent_key,
        projection_digest=raw_node.projection_digest,
        atomic_keys=raw_node.atomic_keys,
    ))


def _validated_node_atomics(
    binding: DerivedTextNodeBindingV1,
    raw_atomics: object,
) -> tuple[hierarchy.AtomicMemoryProjectionInputV1, ...]:
    try:
        atomics, _ = hierarchy._validate_atomics(raw_atomics)
    except hierarchy.MemoryHierarchyProjectionError:
        _raise("invalid_atomics")
    by_key = {item.memory_key: item for item in atomics}
    if set(by_key) != set(binding.atomic_keys):
        _raise("node_member_mismatch")
    return tuple(by_key[key] for key in binding.atomic_keys)


def _normalize_sentence_text(value: object) -> str:
    if type(value) is not str or not value or len(value) > MAX_SENTENCE_CHARS:
        _raise("invalid_derived_sentence")
    if any(
        char in "\r\n\t\x00"
        or 0xD800 <= ord(char) <= 0xDFFF
        or (ord(char) < 0x20 and char != " ")
        for char in value
    ):
        _raise("invalid_derived_sentence")
    normalized = " ".join(value.split())
    if normalized != value or not normalized:
        _raise("invalid_derived_sentence")
    return normalized


def _validate_sentences(
    raw_sentences: object,
    binding: DerivedTextNodeBindingV1,
) -> tuple[DerivedTextSentenceV1, ...]:
    if type(raw_sentences) not in (list, tuple):
        _raise("invalid_derived_sentence")
    if not raw_sentences:
        _raise("invalid_derived_sentence")
    if len(raw_sentences) > MAX_SENTENCES:
        _raise("too_many_sentences")
    node_members = set(binding.atomic_keys)
    seen_sentences: set[tuple[str, tuple[str, ...]]] = set()
    total_supports = 0
    sentences: list[DerivedTextSentenceV1] = []
    for raw in raw_sentences:
        if type(raw) is not DerivedTextSentenceV1:
            _raise("invalid_derived_sentence")
        text = _normalize_sentence_text(raw.text)
        if type(raw.support_keys) is not tuple or not raw.support_keys:
            _raise("invalid_derived_sentence")
        if len(raw.support_keys) > MAX_SUPPORT_KEYS_PER_SENTENCE:
            _raise("too_many_support_refs")
        support_keys = tuple(sorted(raw.support_keys))
        if len(set(support_keys)) != len(support_keys):
            _raise("invalid_derived_sentence")
        for memory_key in support_keys:
            if type(memory_key) is not str or _MEMORY_KEY_PATTERN.fullmatch(memory_key) is None:
                _raise("unknown_support_key")
            if memory_key not in node_members:
                _raise("unknown_support_key")
        total_supports += len(support_keys)
        if total_supports > MAX_TOTAL_SUPPORT_REFS:
            _raise("too_many_support_refs")
        identity = (text, support_keys)
        if identity in seen_sentences:
            _raise("duplicate_sentence")
        seen_sentences.add(identity)
        sentences.append(DerivedTextSentenceV1(
            text=text,
            support_keys=support_keys,
        ))
    return tuple(sentences)


def _content_digest(
    binding: DerivedTextNodeBindingV1,
    sentences: tuple[DerivedTextSentenceV1, ...],
    text: str,
) -> str:
    payload = json.dumps(
        {
            "contract_version": DERIVED_TEXT_CONTRACT_VERSION,
            "node_type": binding.node_type,
            "node_key": binding.node_key,
            "parent_key": binding.parent_key,
            "projection_digest": binding.projection_digest,
            "text": text,
            "sentences": [
                {
                    "text": sentence.text,
                    "support_keys": list(sentence.support_keys),
                }
                for sentence in sentences
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_derived_text_document_v1(
    raw_binding: object,
    raw_atomics: object,
    raw_sentences: object,
) -> DerivedTextDocumentV1:
    """Build one digest-bound derived document from supported short sentences."""

    binding = _validate_binding(raw_binding)
    _validated_node_atomics(binding, raw_atomics)
    sentences = _validate_sentences(raw_sentences, binding)
    text = " ".join(sentence.text for sentence in sentences)
    if not text or len(text) > MAX_TEXT_CHARS:
        _raise("text_budget_exceeded")
    content_digest = _content_digest(binding, sentences, text)
    return DerivedTextDocumentV1(
        contract_version=DERIVED_TEXT_CONTRACT_VERSION,
        node_type=binding.node_type,
        node_key=binding.node_key,
        parent_key=binding.parent_key,
        projection_digest=binding.projection_digest,
        content_digest=content_digest,
        text=text,
        sentences=sentences,
    )


def document_matches_current_node_v1(
    raw_document: object,
    raw_receipt: object,
) -> bool:
    """Return true only for the exact current node revision."""

    if type(raw_document) is not DerivedTextDocumentV1:
        return False
    if type(raw_receipt) is not hierarchy.ProjectionNodeReceiptV1:
        return False
    return (
        raw_document.contract_version == DERIVED_TEXT_CONTRACT_VERSION
        and raw_document.node_type == raw_receipt.node_type
        and raw_document.node_key == raw_receipt.node_key
        and raw_document.parent_key == raw_receipt.parent_key
        and raw_document.projection_digest == raw_receipt.projection_digest
        and type(raw_document.content_digest) is str
        and _DIGEST_PATTERN.fullmatch(raw_document.content_digest) is not None
    )
