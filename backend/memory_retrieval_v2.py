"""Pure deterministic planning for bounded transient Memory recall.

Retrieval V2 accepts only an already-read in-memory snapshot of safe Memory
dictionaries.  It performs no I/O and has no runtime integration in Phase 5B-A.
Selected candidates remain untrusted copies for later independent validation.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final


QUERY_MAX_CHARS: Final = 32_000
HARD_MAX_CANDIDATES: Final = 20
HARD_MAX_CANDIDATE_CHARS: Final = 8_000
DEFAULT_MAX_ITEMS: Final = 10
DEFAULT_CHARACTER_BUDGET: Final = 2_000

_ALPHANUMERIC_OVERLAP_WEIGHT: Final = 20
_CJK_BIGRAM_OVERLAP_WEIGHT: Final = 8
_CONTAINMENT_BOOST: Final = 200
_EXACT_EQUALITY_BOOST: Final = 1_000
_MAX_SAFE_COPY_DEPTH: Final = 32

_ERROR_CATEGORIES: Final = (
    "invalid_query",
    "invalid_scope",
    "invalid_candidates",
    "invalid_budget",
    "memory_retrieval_v2_error",
)
_ERROR_CATEGORY_CODES: Final = {
    category: index for index, category in enumerate(_ERROR_CATEGORIES)
}
_GENERIC_ERROR_CATEGORY: Final = "memory_retrieval_v2_error"

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
_RECALL_USES: Final = frozenset({"direct", "cautious", "associate_only"})
_LOW_INFORMATION_ALPHANUMERIC: Final = frozenset({
    "the",
    "and",
    "for",
    "are",
    "was",
    "were",
    "you",
    "your",
    "user",
    "our",
    "with",
    "from",
    "that",
    "this",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "how",
    "have",
    "has",
    "had",
    "does",
    "did",
    "can",
    "could",
    "would",
    "should",
})

# Kept byte-for-byte equivalent to the V1 ranges for lexical parity.  The
# explicit ranges avoid locale and runtime-name lookups.
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


def _category_from_code(code: object) -> str:
    if type(code) is int and 0 <= code < len(_ERROR_CATEGORIES):
        return _ERROR_CATEGORIES[code]
    return _GENERIC_ERROR_CATEGORY


class MemoryRetrievalV2Error(RuntimeError):
    """Fixed-category, immutable, data-free planner failure."""

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
        except BaseException:
            return _GENERIC_ERROR_CATEGORY
        return _category_from_code(code)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("MemoryRetrievalV2Error is immutable")

    def __str__(self) -> str:
        return self.category

    def __repr__(self) -> str:
        try:
            return f"MemoryRetrievalV2Error({self.category!r})"
        except BaseException:
            return "MemoryRetrievalV2Error('memory_retrieval_v2_error')"


def _copy_safe_structure(
    value: object,
    *,
    active_containers: set[int] | None = None,
    depth: int = 0,
) -> object:
    """Copy exact contract-safe built-ins without invoking caller hooks."""

    if depth > _MAX_SAFE_COPY_DEPTH:
        raise MemoryRetrievalV2Error("invalid_candidates")
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise MemoryRetrievalV2Error("invalid_candidates")
        return value
    if type(value) is str:
        try:
            value.encode("utf-8", errors="strict")
        except Exception:
            raise MemoryRetrievalV2Error("invalid_candidates") from None
        return value
    if type(value) not in (dict, list, tuple):
        raise MemoryRetrievalV2Error("invalid_candidates")

    active = active_containers if active_containers is not None else set()
    identity = id(value)
    if identity in active:
        raise MemoryRetrievalV2Error("invalid_candidates")
    active.add(identity)
    try:
        if type(value) is dict:
            copied_dict: dict = {}
            for key, nested in value.items():
                if type(key) is not str:
                    raise MemoryRetrievalV2Error("invalid_candidates")
                try:
                    key.encode("utf-8", errors="strict")
                except Exception:
                    raise MemoryRetrievalV2Error("invalid_candidates") from None
                copied_dict[key] = _copy_safe_structure(
                    nested,
                    active_containers=active,
                    depth=depth + 1,
                )
            return copied_dict
        copied_values = [
            _copy_safe_structure(
                nested,
                active_containers=active,
                depth=depth + 1,
            )
            for nested in value
        ]
        return copied_values if type(value) is list else tuple(copied_values)
    finally:
        active.remove(identity)


@dataclass(frozen=True, slots=True, repr=False, init=False)
class MemoryRecallItemV2:
    """One recall-use decision retaining a private immutable candidate copy."""

    _candidate: object = field(repr=False)
    recall_use: str

    def __init__(self, candidate: dict, recall_use: str):
        if (
            type(candidate) is not dict
            or type(recall_use) is not str
            or recall_use not in _RECALL_USES
        ):
            raise MemoryRetrievalV2Error()
        candidate_copy = _copy_safe_structure(candidate)
        if type(candidate_copy) is not dict:
            raise MemoryRetrievalV2Error("invalid_candidates")
        object.__setattr__(
            self,
            "_candidate",
            MappingProxyType(candidate_copy),
        )
        object.__setattr__(self, "recall_use", recall_use)

    @property
    def candidate(self) -> dict:
        """Return a fresh dictionary copy for later independent verification."""

        try:
            candidate = object.__getattribute__(self, "_candidate")
            if type(candidate) is not MappingProxyType:
                raise ValueError
            candidate_copy = _copy_safe_structure(dict(candidate))
            if type(candidate_copy) is not dict:
                raise ValueError
            return candidate_copy
        except BaseException:
            raise MemoryRetrievalV2Error() from None

    def __repr__(self) -> str:
        try:
            candidate = object.__getattribute__(self, "_candidate")
            recall_use = object.__getattribute__(self, "recall_use")
            if (
                type(candidate) is not MappingProxyType
                or type(recall_use) is not str
                or recall_use not in _RECALL_USES
            ):
                raise ValueError
            return f"<MemoryRecallItemV2 recall_use={recall_use}>"
        except BaseException:
            return "<MemoryRecallItemV2 invalid>"


@dataclass(frozen=True, slots=True, repr=False)
class MemoryRetrievalPlanV2:
    """Immutable recall plan with data-free bounded structural metadata."""

    items: tuple[MemoryRecallItemV2, ...] = field(repr=False)
    candidate_count: int
    eligible_count: int
    selected_count: int
    query_signal_count: int
    total_chars: int
    direct_count: int
    cautious_count: int
    associate_only_count: int

    def __repr__(self) -> str:
        try:
            items = object.__getattribute__(self, "items")
            candidate_count = object.__getattribute__(self, "candidate_count")
            eligible_count = object.__getattribute__(self, "eligible_count")
            selected_count = object.__getattribute__(self, "selected_count")
            query_signal_count = object.__getattribute__(
                self,
                "query_signal_count",
            )
            total_chars = object.__getattribute__(self, "total_chars")
            direct_count = object.__getattribute__(self, "direct_count")
            cautious_count = object.__getattribute__(self, "cautious_count")
            associate_only_count = object.__getattribute__(
                self,
                "associate_only_count",
            )
            counts = (
                candidate_count,
                eligible_count,
                selected_count,
                query_signal_count,
                total_chars,
                direct_count,
                cautious_count,
                associate_only_count,
            )
            if (
                type(items) is not tuple
                or any(type(value) is not int or value < 0 for value in counts)
                or candidate_count > HARD_MAX_CANDIDATES
                or eligible_count > candidate_count
                or selected_count != len(items)
                or selected_count > eligible_count
                or selected_count > DEFAULT_MAX_ITEMS
                or query_signal_count > QUERY_MAX_CHARS * 2
                or total_chars > DEFAULT_CHARACTER_BUDGET
                or direct_count + cautious_count + associate_only_count
                != selected_count
            ):
                raise ValueError
            computed_chars = 0
            computed_modes = {
                "direct": 0,
                "cautious": 0,
                "associate_only": 0,
            }
            for item in items:
                if type(item) is not MemoryRecallItemV2:
                    raise ValueError
                candidate = object.__getattribute__(item, "_candidate")
                recall_use = object.__getattribute__(item, "recall_use")
                if (
                    type(candidate) is not MappingProxyType
                    or type(recall_use) is not str
                    or recall_use not in computed_modes
                ):
                    raise ValueError
                content = candidate.get("normalized_content")
                if type(content) is not str:
                    raise ValueError
                computed_chars += len(content)
                computed_modes[recall_use] += 1
            if (
                computed_chars != total_chars
                or computed_modes["direct"] != direct_count
                or computed_modes["cautious"] != cautious_count
                or computed_modes["associate_only"] != associate_only_count
            ):
                raise ValueError
            return (
                "<MemoryRetrievalPlanV2 "
                f"candidate_count={candidate_count} "
                f"eligible_count={eligible_count} "
                f"selected_count={selected_count} "
                f"query_signal_count={query_signal_count} "
                f"total_chars={total_chars} "
                f"direct_count={direct_count} "
                f"cautious_count={cautious_count} "
                f"associate_only_count={associate_only_count}>"
            )
        except BaseException:
            return "<MemoryRetrievalPlanV2 invalid>"


@dataclass(frozen=True, slots=True, repr=False)
class _ValidatedCandidate:
    item: dict = field(repr=False)
    content: str = field(repr=False)
    explicitness: str
    confidence: int | float
    position: int


@dataclass(frozen=True, slots=True, repr=False)
class _TextSignals:
    alphanumeric: tuple[str, ...] = field(repr=False)
    cjk_bigrams: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class _RankedCandidate:
    candidate: _ValidatedCandidate = field(repr=False)
    relevance_tier: int
    lexical_score: int


def _validate_query(query_text: object) -> str:
    if type(query_text) is not str or len(query_text) > QUERY_MAX_CHARS:
        raise MemoryRetrievalV2Error("invalid_query")
    try:
        query_text.encode("utf-8", errors="strict")
    except Exception:
        raise MemoryRetrievalV2Error("invalid_query") from None
    return query_text


def _validate_scope(scope_type: object) -> str:
    if type(scope_type) is not str or scope_type != "global_user":
        raise MemoryRetrievalV2Error("invalid_scope")
    return scope_type


def _validate_budget(
    *,
    max_items: object,
    character_budget: object,
) -> tuple[int, int]:
    if (
        type(max_items) is not int
        or not 1 <= max_items <= DEFAULT_MAX_ITEMS
        or type(character_budget) is not int
        or not 1 <= character_budget <= DEFAULT_CHARACTER_BUDGET
    ):
        raise MemoryRetrievalV2Error("invalid_budget")
    return max_items, character_budget


def _validate_candidates(
    candidates: object,
    *,
    expected_scope_type: str,
) -> tuple[_ValidatedCandidate, ...]:
    if (
        type(candidates) not in (list, tuple)
        or len(candidates) > HARD_MAX_CANDIDATES
    ):
        raise MemoryRetrievalV2Error("invalid_candidates")

    validated: list[_ValidatedCandidate] = []
    total_chars = 0
    try:
        for position, raw in enumerate(candidates):
            if type(raw) is not dict:
                raise MemoryRetrievalV2Error("invalid_candidates")
            item = _copy_safe_structure(raw)
            if (
                type(item) is not dict
                or not _SAFE_ITEM_FIELDS.issubset(item)
            ):
                raise MemoryRetrievalV2Error("invalid_candidates")
            if type(item["status"]) is not str or item["status"] != "active":
                raise MemoryRetrievalV2Error("invalid_candidates")
            if (
                type(item["sensitivity"]) is not str
                or item["sensitivity"] != "normal"
            ):
                raise MemoryRetrievalV2Error("invalid_candidates")
            kind = item["kind"]
            if type(kind) is not str or kind not in _KINDS:
                raise MemoryRetrievalV2Error("invalid_candidates")
            if (
                type(item["scope_type"]) is not str
                or item["scope_type"] != expected_scope_type
                or type(item["scope_ref"]) is not str
                or item["scope_ref"] != ""
            ):
                raise MemoryRetrievalV2Error("invalid_candidates")
            content = item["normalized_content"]
            if type(content) is not str or not content.strip():
                raise MemoryRetrievalV2Error("invalid_candidates")
            content.encode("utf-8", errors="strict")
            explicitness = item["explicitness"]
            confidence = item["confidence"]
            if (
                type(explicitness) is not str
                or explicitness not in {"explicit", "inferred"}
                or type(confidence) not in (int, float)
                or not 0.0 <= confidence <= 1.0
                or not math.isfinite(confidence)
                or type(item["memory_key"]) is not str
                or not item["memory_key"]
                or type(item["fingerprint_version"]) is not int
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
                raise MemoryRetrievalV2Error("invalid_candidates")
            total_chars += len(content)
            validated.append(_ValidatedCandidate(
                item=item,
                content=content,
                explicitness=explicitness,
                confidence=confidence,
                position=position,
            ))
    except MemoryRetrievalV2Error:
        raise
    except Exception:
        raise MemoryRetrievalV2Error("invalid_candidates") from None

    if total_chars > HARD_MAX_CANDIDATE_CHARS:
        raise MemoryRetrievalV2Error("invalid_candidates")
    return tuple(validated)


def _normalize_for_retrieval(text: str, *, category: str) -> str:
    try:
        normalized = unicodedata.normalize("NFC", text)
        normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
        normalized = normalized.casefold()
        return " ".join(normalized.split())
    except Exception:
        raise MemoryRetrievalV2Error(category) from None


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return any(start <= codepoint <= end for start, end in _CJK_RANGES)


def _append_unique(value: str, output: list[str], seen: set[str]) -> None:
    if value not in seen:
        seen.add(value)
        output.append(value)


def _text_signals(text: str) -> _TextSignals:
    """Extract the same ordered raw lexical signals as Retrieval V1."""

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


def _is_usable_alphanumeric(token: str) -> bool:
    if any(character.isdigit() for character in token):
        return len(token) >= 2
    return len(token) >= 3 and token not in _LOW_INFORMATION_ALPHANUMERIC


def _usable_signals(signals: _TextSignals) -> _TextSignals:
    return _TextSignals(
        tuple(
            token
            for token in signals.alphanumeric
            if _is_usable_alphanumeric(token)
        ),
        signals.cjk_bigrams,
    )


def _overlap_count(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    right_set = set(right)
    return sum(1 for token in left if token in right_set)


def _rank_candidates(
    candidates: tuple[_ValidatedCandidate, ...],
    *,
    normalized_query: str,
    query_signals: _TextSignals,
) -> list[_RankedCandidate]:
    ranked: list[_RankedCandidate] = []
    query_has_signal = bool(
        query_signals.alphanumeric or query_signals.cjk_bigrams
    )
    for candidate in candidates:
        normalized_content = _normalize_for_retrieval(
            candidate.content,
            category="invalid_candidates",
        )
        content_signals = _usable_signals(_text_signals(normalized_content))
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
        overlap = alphanumeric_overlap + cjk_overlap
        if not (containment or overlap):
            continue

        exact = normalized_query == normalized_content
        if exact:
            relevance_tier = 4
        elif containment:
            relevance_tier = 3
        elif overlap >= 2:
            relevance_tier = 2
        else:
            relevance_tier = 1
        lexical_score = (
            alphanumeric_overlap * _ALPHANUMERIC_OVERLAP_WEIGHT
            + cjk_overlap * _CJK_BIGRAM_OVERLAP_WEIGHT
        )
        if containment:
            lexical_score += _CONTAINMENT_BOOST
        if exact:
            lexical_score += _EXACT_EQUALITY_BOOST
        ranked.append(_RankedCandidate(
            candidate=candidate,
            relevance_tier=relevance_tier,
            lexical_score=lexical_score,
        ))

    ranked.sort(key=lambda entry: (
        -entry.relevance_tier,
        -entry.lexical_score,
        0 if entry.candidate.explicitness == "explicit" else 1,
        -entry.candidate.confidence,
        entry.candidate.position,
    ))
    return ranked


def _recall_use(entry: _RankedCandidate) -> str | None:
    candidate = entry.candidate
    if candidate.confidence < 0.50:
        return None
    if (
        candidate.explicitness == "explicit"
        and candidate.confidence >= 0.90
        and entry.relevance_tier >= 2
    ):
        return "direct"
    if (
        candidate.explicitness == "explicit"
        and candidate.confidence >= 0.70
    ) or (
        candidate.explicitness == "inferred"
        and candidate.confidence >= 0.90
        and entry.relevance_tier >= 2
    ):
        return "cautious"
    return "associate_only"


def _build_plan(
    candidates: tuple[_ValidatedCandidate, ...],
    *,
    query_signal_count: int,
    ranked: list[_RankedCandidate],
    max_items: int,
    character_budget: int,
) -> MemoryRetrievalPlanV2:
    selected: list[MemoryRecallItemV2] = []
    total_chars = 0
    mode_counts = {
        "direct": 0,
        "cautious": 0,
        "associate_only": 0,
    }
    for entry in ranked:
        if len(selected) == max_items:
            break
        recall_use = _recall_use(entry)
        if recall_use is None:
            continue
        candidate = entry.candidate
        next_total = total_chars + len(candidate.content)
        if next_total > character_budget:
            continue
        selected.append(MemoryRecallItemV2(candidate.item, recall_use))
        total_chars = next_total
        mode_counts[recall_use] += 1

    return MemoryRetrievalPlanV2(
        items=tuple(selected),
        candidate_count=len(candidates),
        eligible_count=len(ranked),
        selected_count=len(selected),
        query_signal_count=query_signal_count,
        total_chars=total_chars,
        direct_count=mode_counts["direct"],
        cautious_count=mode_counts["cautious"],
        associate_only_count=mode_counts["associate_only"],
    )


def plan_memory_recall_v2(
    candidates: object,
    *,
    query_text: object,
    scope_type: object,
    max_items: object = DEFAULT_MAX_ITEMS,
    character_budget: object = DEFAULT_CHARACTER_BUDGET,
) -> MemoryRetrievalPlanV2:
    """Validate, rank, classify, and budget a deterministic recall plan."""

    try:
        validated_scope = _validate_scope(scope_type)
        validated_max_items, validated_character_budget = _validate_budget(
            max_items=max_items,
            character_budget=character_budget,
        )
        validated_query = _validate_query(query_text)
        validated_candidates = _validate_candidates(
            candidates,
            expected_scope_type=validated_scope,
        )
        normalized_query = _normalize_for_retrieval(
            validated_query,
            category="invalid_query",
        )
        query_signals = _usable_signals(_text_signals(normalized_query))
        query_signal_count = (
            len(query_signals.alphanumeric) + len(query_signals.cjk_bigrams)
        )
        if query_signal_count == 0:
            return _build_plan(
                validated_candidates,
                query_signal_count=0,
                ranked=[],
                max_items=validated_max_items,
                character_budget=validated_character_budget,
            )
        ranked = _rank_candidates(
            validated_candidates,
            normalized_query=normalized_query,
            query_signals=query_signals,
        )
        return _build_plan(
            validated_candidates,
            query_signal_count=query_signal_count,
            ranked=ranked,
            max_items=validated_max_items,
            character_budget=validated_character_budget,
        )
    except MemoryRetrievalV2Error:
        raise
    except Exception:
        raise MemoryRetrievalV2Error() from None


__all__ = (
    "DEFAULT_CHARACTER_BUDGET",
    "DEFAULT_MAX_ITEMS",
    "HARD_MAX_CANDIDATE_CHARS",
    "HARD_MAX_CANDIDATES",
    "MemoryRecallItemV2",
    "MemoryRetrievalPlanV2",
    "MemoryRetrievalV2Error",
    "QUERY_MAX_CHARS",
    "plan_memory_recall_v2",
)
