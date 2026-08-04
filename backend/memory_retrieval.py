"""Pure, deterministic lexical selection for transient Memory retrieval.

The selector accepts only the active, normal-sensitivity safe dictionaries
returned by ``MemoryReadService.get_active_memories``.  It performs no I/O and
does not turn its result into a trusted prompt contract; the Phase 2 renderer
must validate the returned item copies again before use.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Final


QUERY_MAX_CHARS: Final = 32_000
HARD_MAX_CANDIDATES: Final = 20
HARD_MAX_CANDIDATE_CHARS: Final = 8_000
DEFAULT_MAX_ITEMS: Final = 10
DEFAULT_CHARACTER_BUDGET: Final = 2_000

_ERROR_CATEGORIES: Final = (
    "invalid_query",
    "invalid_scope",
    "invalid_candidates",
    "invalid_budget",
)
_ERROR_CATEGORY_CODES: Final = {
    category: index for index, category in enumerate(_ERROR_CATEGORIES)
}
_GENERIC_ERROR_CATEGORY: Final = "invalid_candidates"

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
_SAFE_ITEM_FIELDS: Final = frozenset({
    "memory_key",
    "kind",
    "scope_type",
    "scope_ref",
    "normalized_content",
    "fingerprint_version",
    "status",
    "explicitness",
    "confidence",
    "sensitivity",
    "first_observed_at",
    "last_confirmed_at",
    "created_at",
    "updated_at",
    "provenance",
})

# Fixed code-point ranges avoid locale and runtime-name lookups when deciding
# whether adjacent characters form a CJK retrieval bigram.
_CJK_RANGES: Final = (
    (0x1100, 0x11FF),  # Hangul Jamo
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0x3130, 0x318F),  # Hangul Compatibility Jamo
    (0x31F0, 0x31FF),  # Katakana Phonetic Extensions
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xA960, 0xA97F),  # Hangul Jamo Extended-A
    (0xAC00, 0xD7AF),  # Hangul Syllables
    (0xD7B0, 0xD7FF),  # Hangul Jamo Extended-B
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0xFF66, 0xFF9D),  # Halfwidth Katakana
    (0x1B000, 0x1B0FF),  # Kana Supplement / Extended-A
    (0x1AFF0, 0x1AFFF),  # Kana Extended-B
    (0x20000, 0x2A6DF),  # CJK Unified Ideographs Extension B
    (0x2A700, 0x2B73F),  # Extension C
    (0x2B740, 0x2B81F),  # Extension D
    (0x2B820, 0x2CEAF),  # CJK Unified Ideographs Extension E
    (0x2CEB0, 0x2EBEF),  # CJK Unified Ideographs Extension F
    (0x2EBF0, 0x2EE5F),  # CJK Unified Ideographs Extension I
    (0x2F800, 0x2FA1F),  # CJK Compatibility Supplement
    (0x30000, 0x3134F),  # Extension G
    (0x31350, 0x323AF),  # Extension H
    (0x323B0, 0x3347F),  # CJK Unified Ideographs Extension J
)


def _category_from_code(code: object) -> str:
    if type(code) is int and 0 <= code < len(_ERROR_CATEGORIES):
        return _ERROR_CATEGORIES[code]
    return _GENERIC_ERROR_CATEGORY


class MemoryRetrievalError(RuntimeError):
    """Fixed, data-free selector failure."""

    __slots__ = ("_category_code",)

    def __init__(self, category: object = _GENERIC_ERROR_CATEGORY):
        safe_category = (
            category
            if type(category) is str and category in _ERROR_CATEGORY_CODES
            else _GENERIC_ERROR_CATEGORY
        )
        object.__setattr__(
            self,
            "_category_code",
            _ERROR_CATEGORY_CODES[safe_category],
        )
        super().__init__(safe_category)

    @property
    def category(self) -> str:
        try:
            code = object.__getattribute__(self, "_category_code")
        except Exception:
            return _GENERIC_ERROR_CATEGORY
        return _category_from_code(code)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("MemoryRetrievalError is immutable")

    def __str__(self) -> str:
        return self.category

    def __repr__(self) -> str:
        return f"MemoryRetrievalError({self.category!r})"


@dataclass(frozen=True, slots=True, repr=False)
class MemoryRetrievalSelectionV1:
    """A ranked tuple of untrusted safe-item copies and data-free counts."""

    items: tuple[dict, ...] = field(repr=False)
    candidate_count: int
    selected_count: int
    query_signal_count: int

    def __repr__(self) -> str:
        try:
            items = object.__getattribute__(self, "items")
            candidate_count = object.__getattribute__(self, "candidate_count")
            selected_count = object.__getattribute__(self, "selected_count")
            query_signal_count = object.__getattribute__(self, "query_signal_count")
            if (
                type(items) is not tuple
                or type(candidate_count) is not int
                or type(selected_count) is not int
                or type(query_signal_count) is not int
                or candidate_count < 0
                or selected_count < 0
                or query_signal_count < 0
                or selected_count != len(items)
                or selected_count > candidate_count
            ):
                raise ValueError
            return (
                "<MemoryRetrievalSelectionV1 "
                f"candidate_count={candidate_count} "
                f"selected_count={selected_count} "
                f"query_signal_count={query_signal_count}>"
            )
        except Exception:
            return "<MemoryRetrievalSelectionV1 invalid>"


@dataclass(frozen=True, slots=True, repr=False)
class _ValidatedCandidate:
    item: dict = field(repr=False)
    content: str = field(repr=False)
    position: int


@dataclass(frozen=True, slots=True, repr=False)
class _TextSignals:
    alphanumeric: tuple[str, ...] = field(repr=False)
    cjk_bigrams: tuple[str, ...] = field(repr=False)


def _validate_query(query_text: object) -> str:
    if type(query_text) is not str or len(query_text) > QUERY_MAX_CHARS:
        raise MemoryRetrievalError("invalid_query")
    try:
        query_text.encode("utf-8", errors="strict")
    except Exception:
        raise MemoryRetrievalError("invalid_query") from None
    return query_text


def _validate_scope(scope_type: object) -> str:
    if type(scope_type) is not str or scope_type != "global_user":
        raise MemoryRetrievalError("invalid_scope")
    return scope_type


def _validate_budget(
    *,
    max_items: object,
    character_budget: object,
) -> tuple[int, int]:
    if (
        type(max_items) is not int
        or not 1 <= max_items <= HARD_MAX_CANDIDATES
        or type(character_budget) is not int
        or not 1 <= character_budget <= HARD_MAX_CANDIDATE_CHARS
    ):
        raise MemoryRetrievalError("invalid_budget")
    return max_items, character_budget


def _validate_candidates(
    items: object,
    *,
    expected_scope_type: str,
) -> tuple[_ValidatedCandidate, ...]:
    if type(items) not in (list, tuple) or len(items) > HARD_MAX_CANDIDATES:
        raise MemoryRetrievalError("invalid_candidates")

    validated: list[_ValidatedCandidate] = []
    total_chars = 0
    try:
        for position, raw in enumerate(items):
            if type(raw) is not dict or not _SAFE_ITEM_FIELDS.issubset(raw):
                raise MemoryRetrievalError("invalid_candidates")
            item = raw.copy()
            if type(item["status"]) is not str or item["status"] != "active":
                raise MemoryRetrievalError("invalid_candidates")
            if (
                type(item["sensitivity"]) is not str
                or item["sensitivity"] != "normal"
            ):
                raise MemoryRetrievalError("invalid_candidates")
            kind = item["kind"]
            if type(kind) is not str or kind not in _KINDS:
                raise MemoryRetrievalError("invalid_candidates")
            if (
                type(item["scope_type"]) is not str
                or item["scope_type"] != expected_scope_type
                or type(item["scope_ref"]) is not str
                or item["scope_ref"] != ""
            ):
                raise MemoryRetrievalError("invalid_candidates")
            content = item["normalized_content"]
            if type(content) is not str or not content.strip():
                raise MemoryRetrievalError("invalid_candidates")
            content.encode("utf-8", errors="strict")
            if (
                type(item["memory_key"]) is not str
                or not item["memory_key"]
                or type(item["fingerprint_version"]) is not int
                or type(item["explicitness"]) is not str
                or type(item["confidence"]) not in (int, float)
                or type(item["provenance"]) is not list
                or any(
                    type(item[name]) is not str or not item[name]
                    for name in (
                        "first_observed_at",
                        "last_confirmed_at",
                        "created_at",
                        "updated_at",
                    )
                )
            ):
                raise MemoryRetrievalError("invalid_candidates")
            total_chars += len(content)
            validated.append(_ValidatedCandidate(item, content, position))
    except MemoryRetrievalError:
        raise
    except Exception:
        raise MemoryRetrievalError("invalid_candidates") from None

    if total_chars > HARD_MAX_CANDIDATE_CHARS:
        raise MemoryRetrievalError("invalid_candidates")
    return tuple(validated)


def _normalize_for_retrieval(text: str, *, category: str) -> str:
    try:
        normalized = unicodedata.normalize("NFC", text)
        normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
        normalized = normalized.casefold()
        return " ".join(normalized.split())
    except Exception:
        raise MemoryRetrievalError(category) from None


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return any(start <= codepoint <= end for start, end in _CJK_RANGES)


def _append_unique(value: str, output: list[str], seen: set[str]) -> None:
    if value not in seen:
        seen.add(value)
        output.append(value)


def _text_signals(text: str) -> _TextSignals:
    alphanumeric: list[str] = []
    cjk_bigrams: list[str] = []
    seen_alphanumeric: set[str] = set()
    seen_cjk: set[str] = set()
    alphanumeric_run: list[str] = []
    cjk_run: list[str] = []

    def flush_alphanumeric() -> None:
        if alphanumeric_run:
            token = "".join(alphanumeric_run)
            if len(token) >= 2:
                _append_unique(token, alphanumeric, seen_alphanumeric)
            alphanumeric_run.clear()

    def flush_cjk() -> None:
        if cjk_run:
            for index in range(len(cjk_run) - 1):
                _append_unique(
                    cjk_run[index] + cjk_run[index + 1],
                    cjk_bigrams,
                    seen_cjk,
                )
            cjk_run.clear()

    for character in text:
        if _is_cjk(character):
            cjk_run.append(character)
        else:
            flush_cjk()
        category = unicodedata.category(character)
        if category[:1] in {"L", "N"}:
            alphanumeric_run.append(character)
        else:
            flush_alphanumeric()
    flush_alphanumeric()
    flush_cjk()
    return _TextSignals(tuple(alphanumeric), tuple(cjk_bigrams))


def _overlap_count(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    right_set = set(right)
    return sum(1 for token in left if token in right_set)


def _rank_candidates(
    candidates: tuple[_ValidatedCandidate, ...],
    *,
    normalized_query: str,
    query_signals: _TextSignals,
) -> list[tuple[int, int, int, _ValidatedCandidate]]:
    ranked: list[tuple[int, int, int, _ValidatedCandidate]] = []
    query_has_signal = bool(
        query_signals.alphanumeric or query_signals.cjk_bigrams
    )
    for candidate in candidates:
        normalized_content = _normalize_for_retrieval(
            candidate.content,
            category="invalid_candidates",
        )
        content_signals = _text_signals(normalized_content)
        alphanumeric_overlap = _overlap_count(
            query_signals.alphanumeric,
            content_signals.alphanumeric,
        )
        cjk_overlap = _overlap_count(
            query_signals.cjk_bigrams,
            content_signals.cjk_bigrams,
        )
        content_has_signal = bool(
            content_signals.alphanumeric or content_signals.cjk_bigrams
        )
        containment = (
            normalized_query in normalized_content and query_has_signal
        ) or (
            normalized_content in normalized_query and content_has_signal
        )
        if not (alphanumeric_overlap or cjk_overlap or containment):
            continue

        score = alphanumeric_overlap * 20 + cjk_overlap * 8
        if containment:
            score += 200
        if normalized_query == normalized_content:
            score += 1000
        strong_overlap = alphanumeric_overlap + cjk_overlap
        ranked.append((score, strong_overlap, candidate.position, candidate))

    ranked.sort(key=lambda entry: (-entry[0], -entry[1], entry[2]))
    return ranked


def select_relevant_memory_items(
    items: object,
    *,
    query_text: str,
    scope_type: str,
    max_items: int = DEFAULT_MAX_ITEMS,
    character_budget: int = DEFAULT_CHARACTER_BUDGET,
) -> MemoryRetrievalSelectionV1:
    """Validate, rank, and budget lexically relevant global-user Memory items."""

    validated_scope = _validate_scope(scope_type)
    validated_max_items, validated_character_budget = _validate_budget(
        max_items=max_items,
        character_budget=character_budget,
    )
    validated_query = _validate_query(query_text)
    candidates = _validate_candidates(
        items,
        expected_scope_type=validated_scope,
    )

    normalized_query = _normalize_for_retrieval(
        validated_query,
        category="invalid_query",
    )
    try:
        query_signals = _text_signals(normalized_query)
    except Exception:
        raise MemoryRetrievalError("invalid_query") from None
    query_signal_count = (
        len(query_signals.alphanumeric) + len(query_signals.cjk_bigrams)
    )
    if query_signal_count == 0:
        return MemoryRetrievalSelectionV1(
            items=(),
            candidate_count=len(candidates),
            selected_count=0,
            query_signal_count=0,
        )

    try:
        ranked = _rank_candidates(
            candidates,
            normalized_query=normalized_query,
            query_signals=query_signals,
        )
    except MemoryRetrievalError:
        raise
    except Exception:
        raise MemoryRetrievalError("invalid_candidates") from None

    selected: list[dict] = []
    total_chars = 0
    for _score, _strong_overlap, _position, candidate in ranked:
        if len(selected) >= validated_max_items:
            break
        next_total = total_chars + len(candidate.content)
        if next_total > validated_character_budget:
            continue
        selected.append(candidate.item.copy())
        total_chars = next_total

    return MemoryRetrievalSelectionV1(
        items=tuple(selected),
        candidate_count=len(candidates),
        selected_count=len(selected),
        query_signal_count=query_signal_count,
    )


__all__ = (
    "DEFAULT_CHARACTER_BUDGET",
    "DEFAULT_MAX_ITEMS",
    "HARD_MAX_CANDIDATE_CHARS",
    "HARD_MAX_CANDIDATES",
    "MemoryRetrievalError",
    "MemoryRetrievalSelectionV1",
    "QUERY_MAX_CHARS",
    "select_relevant_memory_items",
)
