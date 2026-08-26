"""Pure fail-closed authority boundary for active Memory Retrieval V2."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Final

try:
    from . import memory_retrieval_v2
except ImportError:  # support direct module execution in local tooling
    import memory_retrieval_v2


ACTIVE_FAILURE_CATEGORY: Final = "memory_retrieval_v2_active_unavailable"
MAX_CANDIDATES: Final = 20
MAX_SELECTED: Final = 10
MAX_FINAL_CHARS: Final = 2_000
_MAX_SAFE_COPY_DEPTH: Final = 32
_RECALL_USES: Final = frozenset({"direct", "cautious", "associate_only"})


class MemoryRetrievalV2ActiveError(RuntimeError):
    """Fixed, immutable, data-free active-boundary failure."""

    __slots__ = ()

    def __init__(self):
        super().__init__(ACTIVE_FAILURE_CATEGORY)

    @property
    def category(self) -> str:
        return ACTIVE_FAILURE_CATEGORY

    def __str__(self) -> str:
        return ACTIVE_FAILURE_CATEGORY

    def __repr__(self) -> str:
        return (
            "MemoryRetrievalV2ActiveError("
            "'memory_retrieval_v2_active_unavailable')"
        )


def _unavailable() -> MemoryRetrievalV2ActiveError:
    return MemoryRetrievalV2ActiveError()


def _freeze_safe_structure(
    value: object,
    *,
    active_containers: set[int] | None = None,
    depth: int = 0,
) -> tuple:
    """Freeze exact safe built-ins without invoking caller-defined hooks."""

    if depth > _MAX_SAFE_COPY_DEPTH:
        raise _unavailable()
    if value is None:
        return ("none",)
    if type(value) is bool:
        return ("bool", value)
    if type(value) is int:
        return ("int", value)
    if type(value) is float:
        if not math.isfinite(value):
            raise _unavailable()
        return ("float", value)
    if type(value) is str:
        try:
            value.encode("utf-8", errors="strict")
        except BaseException:
            raise _unavailable() from None
        return ("str", value)
    if type(value) not in (dict, list, tuple):
        raise _unavailable()

    active = active_containers if active_containers is not None else set()
    identity = id(value)
    if identity in active:
        raise _unavailable()
    active.add(identity)
    try:
        if type(value) is dict:
            entries: list[tuple[str, tuple]] = []
            for key, nested in value.items():
                if type(key) is not str:
                    raise _unavailable()
                try:
                    key.encode("utf-8", errors="strict")
                except BaseException:
                    raise _unavailable() from None
                entries.append((
                    key,
                    _freeze_safe_structure(
                        nested,
                        active_containers=active,
                        depth=depth + 1,
                    ),
                ))
            return ("dict", tuple(entries))
        values = tuple(
            _freeze_safe_structure(
                nested,
                active_containers=active,
                depth=depth + 1,
            )
            for nested in value
        )
        return ("list", values) if type(value) is list else ("tuple", values)
    finally:
        active.remove(identity)


def _thaw_safe_structure(value: object, *, depth: int = 0) -> object:
    """Return a fresh exact-built-in copy of an internally frozen value."""

    if depth > _MAX_SAFE_COPY_DEPTH or type(value) is not tuple or not value:
        raise _unavailable()
    tag = value[0]
    if type(tag) is not str:
        raise _unavailable()
    if tag == "none" and len(value) == 1:
        return None
    if tag == "bool" and len(value) == 2 and type(value[1]) is bool:
        return value[1]
    if tag == "int" and len(value) == 2 and type(value[1]) is int:
        return value[1]
    if tag == "float" and len(value) == 2 and type(value[1]) is float:
        if not math.isfinite(value[1]):
            raise _unavailable()
        return value[1]
    if tag == "str" and len(value) == 2 and type(value[1]) is str:
        try:
            value[1].encode("utf-8", errors="strict")
        except BaseException:
            raise _unavailable() from None
        return value[1]
    if tag == "dict" and len(value) == 2 and type(value[1]) is tuple:
        copied: dict = {}
        for entry in value[1]:
            if (
                type(entry) is not tuple
                or len(entry) != 2
                or type(entry[0]) is not str
                or entry[0] in copied
            ):
                raise _unavailable()
            copied[entry[0]] = _thaw_safe_structure(
                entry[1],
                depth=depth + 1,
            )
        return copied
    if tag in {"list", "tuple"} and len(value) == 2 and type(value[1]) is tuple:
        copied_values = tuple(
            _thaw_safe_structure(nested, depth=depth + 1)
            for nested in value[1]
        )
        return list(copied_values) if tag == "list" else copied_values
    raise _unavailable()


def _copy_safe_structure(value: object) -> object:
    return _thaw_safe_structure(_freeze_safe_structure(value))


def _safe_candidate_tuple(value: object) -> tuple[dict, ...]:
    if type(value) is not tuple or len(value) > MAX_CANDIDATES:
        raise _unavailable()
    copied = _copy_safe_structure(value)
    if type(copied) is not tuple or any(type(item) is not dict for item in copied):
        raise _unavailable()
    return copied


@dataclass(frozen=True, slots=True, repr=False, init=False)
class MemoryRetrievalV2ActiveItem:
    """One immutable, isolated active selection item."""

    _candidate_snapshot: tuple = field(repr=False)
    recall_use: str

    def __new__(cls, *_args: object, **_kwargs: object):
        raise _unavailable()

    @property
    def candidate(self) -> dict:
        try:
            frozen = object.__getattribute__(self, "_candidate_snapshot")
            candidate = _thaw_safe_structure(frozen)
            if type(candidate) is not dict:
                raise _unavailable()
            return candidate
        except MemoryRetrievalV2ActiveError:
            raise
        except BaseException:
            raise _unavailable() from None

    def __repr__(self) -> str:
        try:
            _validate_active_item(self)
            recall_use = object.__getattribute__(self, "recall_use")
            return f"<MemoryRetrievalV2ActiveItem recall_use={recall_use}>"
        except BaseException:
            return "<MemoryRetrievalV2ActiveItem invalid>"


@dataclass(frozen=True, slots=True, repr=False, init=False)
class MemoryRetrievalV2ActiveSelection:
    """Immutable active selection retaining no query or candidate positions."""

    items: tuple[MemoryRetrievalV2ActiveItem, ...] = field(repr=False)
    candidate_count: int
    eligible_count: int
    selected_count: int
    query_signal_count: int
    total_chars: int
    direct_count: int
    cautious_count: int
    associate_only_count: int

    def __new__(cls, *_args: object, **_kwargs: object):
        raise _unavailable()

    def __repr__(self) -> str:
        try:
            values = _validated_selection_values(self)
            return (
                "<MemoryRetrievalV2ActiveSelection "
                f"candidate_count={values[1]} eligible_count={values[2]} "
                f"selected_count={values[3]} query_signal_count={values[4]} "
                f"total_chars={values[5]} direct_count={values[6]} "
                f"cautious_count={values[7]} "
                f"associate_only_count={values[8]}>"
            )
        except BaseException:
            return "<MemoryRetrievalV2ActiveSelection invalid>"


@dataclass(frozen=True, slots=True, repr=False, init=False)
class MemoryRetrievalV2ActiveReport:
    """Bounded data-free facts for successful active preparation telemetry."""

    candidate_count: int
    eligible_count: int
    selected_count: int
    total_chars: int
    direct_count: int
    cautious_count: int
    associate_only_count: int

    def __new__(cls, *_args: object, **_kwargs: object):
        raise _unavailable()

    def __repr__(self) -> str:
        try:
            values = _validated_report_values(self)
            return (
                "<MemoryRetrievalV2ActiveReport "
                f"candidate_count={values[0]} eligible_count={values[1]} "
                f"selected_count={values[2]} total_chars={values[3]} "
                f"direct_count={values[4]} cautious_count={values[5]} "
                f"associate_only_count={values[6]}>"
            )
        except BaseException:
            return "<MemoryRetrievalV2ActiveReport invalid>"


def _new_active_item(
    candidate: dict,
    recall_use: str,
) -> MemoryRetrievalV2ActiveItem:
    if type(candidate) is not dict or type(recall_use) is not str:
        raise _unavailable()
    item = object.__new__(MemoryRetrievalV2ActiveItem)
    object.__setattr__(item, "_candidate_snapshot", _freeze_safe_structure(candidate))
    object.__setattr__(item, "recall_use", recall_use)
    _validate_active_item(item)
    return item


def _validate_active_item(
    item: object,
) -> tuple[dict, str]:
    try:
        if type(item) is not MemoryRetrievalV2ActiveItem:
            raise _unavailable()
        recall_use = object.__getattribute__(item, "recall_use")
        frozen = object.__getattribute__(item, "_candidate_snapshot")
        if type(recall_use) is not str or recall_use not in _RECALL_USES:
            raise _unavailable()
        candidate = _thaw_safe_structure(frozen)
        if type(candidate) is not dict:
            raise _unavailable()
        content = candidate.get("normalized_content")
        if type(content) is not str:
            raise _unavailable()
        try:
            content.encode("utf-8", errors="strict")
        except BaseException:
            raise _unavailable() from None
        return candidate, recall_use
    except MemoryRetrievalV2ActiveError:
        raise
    except BaseException:
        raise _unavailable() from None


def _validated_selection_values(selection: object) -> tuple:
    try:
        if type(selection) is not MemoryRetrievalV2ActiveSelection:
            raise _unavailable()
        names = (
            "items",
            "candidate_count",
            "eligible_count",
            "selected_count",
            "query_signal_count",
            "total_chars",
            "direct_count",
            "cautious_count",
            "associate_only_count",
        )
        values = tuple(object.__getattribute__(selection, name) for name in names)
        items, *counts = values
        if (
            type(items) is not tuple
            or any(type(value) is not int or value < 0 for value in counts)
        ):
            raise _unavailable()
        (
            candidate_count,
            eligible_count,
            selected_count,
            query_signal_count,
            total_chars,
            direct_count,
            cautious_count,
            associate_only_count,
        ) = counts
        if (
            candidate_count > MAX_CANDIDATES
            or eligible_count > candidate_count
            or selected_count != len(items)
            or selected_count > min(eligible_count, MAX_SELECTED)
            or query_signal_count > memory_retrieval_v2.QUERY_MAX_CHARS * 2
            or (
                query_signal_count == 0
                and (eligible_count != 0 or selected_count != 0)
            )
            or total_chars > MAX_FINAL_CHARS
            or direct_count + cautious_count + associate_only_count
            != selected_count
        ):
            raise _unavailable()
        computed_chars = 0
        computed_modes = {"direct": 0, "cautious": 0, "associate_only": 0}
        for item in items:
            candidate, recall_use = _validate_active_item(item)
            computed_chars += len(candidate["normalized_content"])
            computed_modes[recall_use] += 1
        if (
            computed_chars != total_chars
            or computed_modes["direct"] != direct_count
            or computed_modes["cautious"] != cautious_count
            or computed_modes["associate_only"] != associate_only_count
        ):
            raise _unavailable()
        return values
    except MemoryRetrievalV2ActiveError:
        raise
    except BaseException:
        raise _unavailable() from None


def _new_selection(
    items: tuple[MemoryRetrievalV2ActiveItem, ...],
    *,
    candidate_count: int,
    eligible_count: int,
    query_signal_count: int,
    total_chars: int,
    direct_count: int,
    cautious_count: int,
    associate_only_count: int,
) -> MemoryRetrievalV2ActiveSelection:
    selection = object.__new__(MemoryRetrievalV2ActiveSelection)
    object.__setattr__(selection, "items", items)
    object.__setattr__(selection, "candidate_count", candidate_count)
    object.__setattr__(selection, "eligible_count", eligible_count)
    object.__setattr__(selection, "selected_count", len(items))
    object.__setattr__(selection, "query_signal_count", query_signal_count)
    object.__setattr__(selection, "total_chars", total_chars)
    object.__setattr__(selection, "direct_count", direct_count)
    object.__setattr__(selection, "cautious_count", cautious_count)
    object.__setattr__(selection, "associate_only_count", associate_only_count)
    _validated_selection_values(selection)
    return selection


def validated_active_selection_items(
    selection: object,
) -> tuple[tuple[dict, str], ...]:
    """Return fresh selected-candidate copies in validated V2 order."""

    try:
        items = _validated_selection_values(selection)[0]
        return tuple(_validate_active_item(item) for item in items)
    except MemoryRetrievalV2ActiveError:
        raise
    except BaseException:
        raise _unavailable() from None


def active_report_from_selection(
    selection: object,
) -> MemoryRetrievalV2ActiveReport:
    values = _validated_selection_values(selection)
    report = object.__new__(MemoryRetrievalV2ActiveReport)
    for name, value in zip(
        (
            "candidate_count",
            "eligible_count",
            "selected_count",
            "total_chars",
            "direct_count",
            "cautious_count",
            "associate_only_count",
        ),
        (values[1], values[2], values[3], values[5], *values[6:9]),
    ):
        object.__setattr__(report, name, value)
    _validated_report_values(report)
    return report


def _validated_report_values(report: object) -> tuple[int, ...]:
    try:
        if type(report) is not MemoryRetrievalV2ActiveReport:
            raise _unavailable()
        values = tuple(
            object.__getattribute__(report, name)
            for name in (
                "candidate_count",
                "eligible_count",
                "selected_count",
                "total_chars",
                "direct_count",
                "cautious_count",
                "associate_only_count",
            )
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise _unavailable()
        candidates, eligible, selected, chars, direct, cautious, associate = values
        if (
            candidates > MAX_CANDIDATES
            or eligible > candidates
            or selected > min(eligible, MAX_SELECTED)
            or chars > MAX_FINAL_CHARS
            or direct + cautious + associate != selected
        ):
            raise _unavailable()
        return values
    except MemoryRetrievalV2ActiveError:
        raise
    except BaseException:
        raise _unavailable() from None


def render_memory_retrieval_v2_active_telemetry(report: object) -> str | None:
    """Render the one bounded, data-free active success telemetry line."""

    try:
        candidates, eligible, selected, chars, direct, cautious, associate = (
            _validated_report_values(report)
        )
        return (
            "[memory-retrieval-v2-active] status=completed "
            f"candidates={candidates} eligible={eligible} selected={selected} "
            f"chars={chars} direct={direct} cautious={cautious} "
            f"associate_only={associate}"
        )
    except BaseException:
        return None


def plan_memory_recall_v2_active(
    candidates: object,
    *,
    query_text: object,
) -> MemoryRetrievalV2ActiveSelection:
    """Plan and independently validate provider-authoritative V2 selection."""

    try:
        if (
            type(query_text) is not str
            or not query_text.strip()
            or len(query_text) > memory_retrieval_v2.QUERY_MAX_CHARS
        ):
            raise _unavailable()
        query_text.encode("utf-8", errors="strict")
        original_candidates = _safe_candidate_tuple(candidates)
        planner_candidates = _safe_candidate_tuple(original_candidates)
        plan = memory_retrieval_v2.plan_memory_recall_v2(
            planner_candidates,
            query_text=query_text,
            scope_type="global_user",
            max_items=MAX_SELECTED,
            character_budget=MAX_FINAL_CHARS,
        )
        if type(plan) is not memory_retrieval_v2.MemoryRetrievalPlanV2:
            raise _unavailable()
        items = object.__getattribute__(plan, "items")
        candidate_count = object.__getattribute__(plan, "candidate_count")
        eligible_count = object.__getattribute__(plan, "eligible_count")
        selected_count = object.__getattribute__(plan, "selected_count")
        query_signal_count = object.__getattribute__(plan, "query_signal_count")
        total_chars = object.__getattribute__(plan, "total_chars")
        direct_count = object.__getattribute__(plan, "direct_count")
        cautious_count = object.__getattribute__(plan, "cautious_count")
        associate_only_count = object.__getattribute__(
            plan,
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
            or candidate_count != len(original_candidates)
            or candidate_count > MAX_CANDIDATES
            or eligible_count > candidate_count
            or selected_count != len(items)
            or selected_count > min(eligible_count, MAX_SELECTED)
            or query_signal_count > memory_retrieval_v2.QUERY_MAX_CHARS * 2
            or (
                query_signal_count == 0
                and (eligible_count != 0 or selected_count != 0)
            )
            or total_chars > MAX_FINAL_CHARS
            or direct_count + cautious_count + associate_only_count
            != selected_count
        ):
            raise _unavailable()

        remaining_candidates = list(original_candidates)
        selected_items: list[MemoryRetrievalV2ActiveItem] = []
        computed_chars = 0
        computed_modes = {"direct": 0, "cautious": 0, "associate_only": 0}
        for item in items:
            if type(item) is not memory_retrieval_v2.MemoryRecallItemV2:
                raise _unavailable()
            recall_use = object.__getattribute__(item, "recall_use")
            if type(recall_use) is not str or recall_use not in _RECALL_USES:
                raise _unavailable()
            candidate = item.candidate
            copied_candidate = _copy_safe_structure(candidate)
            if type(copied_candidate) is not dict:
                raise _unavailable()
            matched_index = None
            for index, original in enumerate(remaining_candidates):
                if copied_candidate == original:
                    matched_index = index
                    break
            if matched_index is None:
                raise _unavailable()
            matched_candidate = remaining_candidates.pop(matched_index)
            content = matched_candidate.get("normalized_content")
            if type(content) is not str:
                raise _unavailable()
            computed_chars += len(content)
            computed_modes[recall_use] += 1
            selected_items.append(_new_active_item(matched_candidate, recall_use))
        if (
            computed_chars != total_chars
            or computed_modes["direct"] != direct_count
            or computed_modes["cautious"] != cautious_count
            or computed_modes["associate_only"] != associate_only_count
        ):
            raise _unavailable()
        return _new_selection(
            tuple(selected_items),
            candidate_count=candidate_count,
            eligible_count=eligible_count,
            query_signal_count=query_signal_count,
            total_chars=total_chars,
            direct_count=direct_count,
            cautious_count=cautious_count,
            associate_only_count=associate_only_count,
        )
    except MemoryRetrievalV2ActiveError:
        raise
    except BaseException:
        raise _unavailable() from None


__all__ = (
    "ACTIVE_FAILURE_CATEGORY",
    "MAX_CANDIDATES",
    "MAX_FINAL_CHARS",
    "MAX_SELECTED",
    "MemoryRetrievalV2ActiveError",
    "MemoryRetrievalV2ActiveItem",
    "MemoryRetrievalV2ActiveReport",
    "MemoryRetrievalV2ActiveSelection",
    "active_report_from_selection",
    "plan_memory_recall_v2_active",
    "render_memory_retrieval_v2_active_telemetry",
    "validated_active_selection_items",
)
