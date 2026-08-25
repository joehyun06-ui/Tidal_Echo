"""Pure, fail-soft structural comparison for Memory Retrieval V2 shadowing."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

try:
    from . import memory_retrieval_v2
except ImportError:  # support direct module execution in local tooling
    import memory_retrieval_v2


SHADOW_FAILURE_CATEGORY: Final = "memory_retrieval_v2_shadow_unavailable"
_RELATIONS: Final = frozenset({
    "both_empty",
    "identical",
    "reordered",
    "v2_subset",
    "v2_superset",
    "mixed",
})
_RECALL_USES: Final = frozenset({"direct", "cautious", "associate_only"})
_MAX_SAFE_COPY_DEPTH: Final = 32
_MAX_CANDIDATES: Final = 20
_MAX_SELECTED: Final = 10
_MAX_FINAL_CHARS: Final = 2_000


class _ShadowUnavailable(RuntimeError):
    """Private fixed failure used only inside the fail-soft shadow boundary."""

    __slots__ = ()

    def __init__(self):
        super().__init__(SHADOW_FAILURE_CATEGORY)

    def __str__(self) -> str:
        return SHADOW_FAILURE_CATEGORY

    def __repr__(self) -> str:
        return "_ShadowUnavailable('memory_retrieval_v2_shadow_unavailable')"


@dataclass(frozen=True, slots=True, repr=False)
class MemoryRetrievalV2ShadowReport:
    """Immutable report retaining bounded structural facts and no identities."""

    status: str
    relation: str = ""
    candidate_count: int = 0
    v1_selected_count: int = 0
    v2_eligible_count: int = 0
    v2_selected_count: int = 0
    overlap_count: int = 0
    v1_only_count: int = 0
    v2_only_count: int = 0
    v1_total_chars: int = 0
    v2_total_chars: int = 0
    direct_count: int = 0
    cautious_count: int = 0
    associate_only_count: int = 0

    def __post_init__(self) -> None:
        _validated_report_values(self)

    @property
    def category(self) -> str:
        try:
            status = object.__getattribute__(self, "status")
            return SHADOW_FAILURE_CATEGORY if status == "failed" else ""
        except BaseException:
            return SHADOW_FAILURE_CATEGORY

    @classmethod
    def failed(cls) -> MemoryRetrievalV2ShadowReport:
        return cls(status="failed")

    def __repr__(self) -> str:
        try:
            values = _validated_report_values(self)
            if values[0] == "failed":
                return (
                    "<MemoryRetrievalV2ShadowReport status=failed "
                    "category=memory_retrieval_v2_shadow_unavailable>"
                )
            (
                status,
                relation,
                candidate_count,
                v1_selected_count,
                v2_eligible_count,
                v2_selected_count,
                overlap_count,
                v1_only_count,
                v2_only_count,
                v1_total_chars,
                v2_total_chars,
                direct_count,
                cautious_count,
                associate_only_count,
            ) = values
            return (
                "<MemoryRetrievalV2ShadowReport "
                f"status={status} relation={relation} "
                f"candidate_count={candidate_count} "
                f"v1_selected_count={v1_selected_count} "
                f"v2_eligible_count={v2_eligible_count} "
                f"v2_selected_count={v2_selected_count} "
                f"overlap_count={overlap_count} "
                f"v1_only_count={v1_only_count} "
                f"v2_only_count={v2_only_count} "
                f"v1_total_chars={v1_total_chars} "
                f"v2_total_chars={v2_total_chars} "
                f"direct_count={direct_count} "
                f"cautious_count={cautious_count} "
                f"associate_only_count={associate_only_count}>"
            )
        except BaseException:
            return "<MemoryRetrievalV2ShadowReport invalid>"


def _validated_report_values(report: object) -> tuple:
    try:
        if type(report) is not MemoryRetrievalV2ShadowReport:
            raise _ShadowUnavailable()
        names = (
            "status",
            "relation",
            "candidate_count",
            "v1_selected_count",
            "v2_eligible_count",
            "v2_selected_count",
            "overlap_count",
            "v1_only_count",
            "v2_only_count",
            "v1_total_chars",
            "v2_total_chars",
            "direct_count",
            "cautious_count",
            "associate_only_count",
        )
        values = tuple(object.__getattribute__(report, name) for name in names)
        status, relation, *counts = values
        if type(status) is not str or status not in {"completed", "failed"}:
            raise _ShadowUnavailable()
        if type(relation) is not str:
            raise _ShadowUnavailable()
        if any(type(value) is not int or value < 0 for value in counts):
            raise _ShadowUnavailable()
        (
            candidate_count,
            v1_selected_count,
            v2_eligible_count,
            v2_selected_count,
            overlap_count,
            v1_only_count,
            v2_only_count,
            v1_total_chars,
            v2_total_chars,
            direct_count,
            cautious_count,
            associate_only_count,
        ) = counts
        if status == "failed":
            if relation or any(counts):
                raise _ShadowUnavailable()
            return values
        if (
            relation not in _RELATIONS
            or candidate_count > _MAX_CANDIDATES
            or v1_selected_count > min(candidate_count, _MAX_SELECTED)
            or v2_eligible_count > candidate_count
            or v2_selected_count > min(v2_eligible_count, _MAX_SELECTED)
            or overlap_count > min(v1_selected_count, v2_selected_count)
            or v1_only_count != v1_selected_count - overlap_count
            or v2_only_count != v2_selected_count - overlap_count
            or v1_total_chars > _MAX_FINAL_CHARS
            or v2_total_chars > _MAX_FINAL_CHARS
            or direct_count + cautious_count + associate_only_count
            != v2_selected_count
        ):
            raise _ShadowUnavailable()
        both_empty = (
            v1_selected_count == 0
            and v2_selected_count == 0
            and overlap_count == 0
            and v1_only_count == 0
            and v2_only_count == 0
        )
        same_set = (
            v1_selected_count > 0
            and v1_selected_count == v2_selected_count
            and overlap_count == v1_selected_count
            and v1_only_count == 0
            and v2_only_count == 0
        )
        proper_subset = (
            v2_selected_count < v1_selected_count
            and overlap_count == v2_selected_count
            and v2_only_count == 0
            and v1_only_count == v1_selected_count - v2_selected_count
            and v1_only_count > 0
        )
        proper_superset = (
            v1_selected_count < v2_selected_count
            and overlap_count == v1_selected_count
            and v1_only_count == 0
            and v2_only_count == v2_selected_count - v1_selected_count
            and v2_only_count > 0
        )
        relation_is_valid = (
            (relation == "both_empty" and both_empty)
            or (relation in {"identical", "reordered"} and same_set)
            or (relation == "v2_subset" and proper_subset)
            or (relation == "v2_superset" and proper_superset)
            or (
                relation == "mixed"
                and (v1_selected_count > 0 or v2_selected_count > 0)
                and not same_set
                and not proper_subset
                and not proper_superset
            )
        )
        if not relation_is_valid:
            raise _ShadowUnavailable()
        return values
    except _ShadowUnavailable:
        raise
    except BaseException:
        raise _ShadowUnavailable() from None


def render_memory_retrieval_v2_shadow_telemetry(report: object) -> str | None:
    """Render one validated data-free telemetry line without performing I/O."""

    try:
        values = _validated_report_values(report)
        if values[0] == "failed":
            return (
                "[memory-retrieval-v2-shadow] status=failed "
                "category=memory_retrieval_v2_shadow_unavailable"
            )
        (
            _status,
            relation,
            candidates,
            v1_selected,
            v2_eligible,
            v2_selected,
            overlap,
            v1_only,
            v2_only,
            v1_chars,
            v2_chars,
            direct,
            cautious,
            associate_only,
        ) = values
        return (
            "[memory-retrieval-v2-shadow] "
            f"status=completed relation={relation} candidates={candidates} "
            f"v1_selected={v1_selected} v2_eligible={v2_eligible} "
            f"v2_selected={v2_selected} overlap={overlap} "
            f"v1_only={v1_only} v2_only={v2_only} "
            f"v1_chars={v1_chars} v2_chars={v2_chars} "
            f"direct={direct} cautious={cautious} "
            f"associate_only={associate_only}"
        )
    except BaseException:
        return None


def _copy_safe_structure(
    value: object,
    *,
    active_containers: set[int] | None = None,
    depth: int = 0,
) -> object:
    """Copy exact built-ins without invoking caller-controlled hooks."""

    if depth > _MAX_SAFE_COPY_DEPTH:
        raise _ShadowUnavailable()
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _ShadowUnavailable()
        return value
    if type(value) is str:
        try:
            value.encode("utf-8", errors="strict")
        except BaseException:
            raise _ShadowUnavailable() from None
        return value
    if type(value) not in (dict, list, tuple):
        raise _ShadowUnavailable()

    active = active_containers if active_containers is not None else set()
    identity = id(value)
    if identity in active:
        raise _ShadowUnavailable()
    active.add(identity)
    try:
        if type(value) is dict:
            copied: dict = {}
            for key, nested in value.items():
                if type(key) is not str:
                    raise _ShadowUnavailable()
                try:
                    key.encode("utf-8", errors="strict")
                except BaseException:
                    raise _ShadowUnavailable() from None
                copied[key] = _copy_safe_structure(
                    nested,
                    active_containers=active,
                    depth=depth + 1,
                )
            return copied
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


def _safe_candidate_tuple(value: object) -> tuple[dict, ...]:
    if type(value) is not tuple or len(value) > _MAX_CANDIDATES:
        raise _ShadowUnavailable()
    copied = _copy_safe_structure(value)
    if type(copied) is not tuple or any(type(item) is not dict for item in copied):
        raise _ShadowUnavailable()
    return copied


def _selected_positions(
    selected: object,
    *,
    candidates: tuple[dict, ...],
) -> tuple[tuple[int, ...], int]:
    if type(selected) is not tuple or len(selected) > _MAX_SELECTED:
        raise _ShadowUnavailable()
    selected_copy = _copy_safe_structure(selected)
    if (
        type(selected_copy) is not tuple
        or any(type(item) is not dict for item in selected_copy)
    ):
        raise _ShadowUnavailable()
    remaining = list(enumerate(candidates))
    positions: list[int] = []
    total_chars = 0
    for item in selected_copy:
        matched = None
        for remaining_index, (position, candidate) in enumerate(remaining):
            if item == candidate:
                matched = (remaining_index, position)
                break
        if matched is None:
            raise _ShadowUnavailable()
        remaining_index, position = matched
        remaining.pop(remaining_index)
        content = item.get("normalized_content")
        if type(content) is not str:
            raise _ShadowUnavailable()
        total_chars += len(content)
        if total_chars > _MAX_FINAL_CHARS:
            raise _ShadowUnavailable()
        positions.append(position)
    return tuple(positions), total_chars


def _validated_v2_plan(
    plan: object,
    *,
    candidates: tuple[dict, ...],
) -> tuple[tuple[int, ...], int, int, int, int, int]:
    try:
        if type(plan) is not memory_retrieval_v2.MemoryRetrievalPlanV2:
            raise _ShadowUnavailable()
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
            or candidate_count != len(candidates)
            or candidate_count > _MAX_CANDIDATES
            or eligible_count > candidate_count
            or selected_count != len(items)
            or selected_count > min(eligible_count, _MAX_SELECTED)
            or query_signal_count > memory_retrieval_v2.QUERY_MAX_CHARS * 2
            or (
                query_signal_count == 0
                and (eligible_count != 0 or selected_count != 0)
            )
            or total_chars > _MAX_FINAL_CHARS
            or direct_count + cautious_count + associate_only_count
            != selected_count
        ):
            raise _ShadowUnavailable()

        selected_candidates: list[dict] = []
        computed_modes = {
            "direct": 0,
            "cautious": 0,
            "associate_only": 0,
        }
        for item in items:
            if type(item) is not memory_retrieval_v2.MemoryRecallItemV2:
                raise _ShadowUnavailable()
            recall_use = object.__getattribute__(item, "recall_use")
            if type(recall_use) is not str or recall_use not in _RECALL_USES:
                raise _ShadowUnavailable()
            candidate = item.candidate
            if type(candidate) is not dict:
                raise _ShadowUnavailable()
            copied_candidate = _copy_safe_structure(candidate)
            if type(copied_candidate) is not dict:
                raise _ShadowUnavailable()
            selected_candidates.append(copied_candidate)
            computed_modes[recall_use] += 1
        positions, computed_chars = _selected_positions(
            tuple(selected_candidates),
            candidates=candidates,
        )
        if (
            computed_chars != total_chars
            or computed_modes["direct"] != direct_count
            or computed_modes["cautious"] != cautious_count
            or computed_modes["associate_only"] != associate_only_count
        ):
            raise _ShadowUnavailable()
        return (
            positions,
            eligible_count,
            total_chars,
            direct_count,
            cautious_count,
            associate_only_count,
        )
    except _ShadowUnavailable:
        raise
    except BaseException:
        raise _ShadowUnavailable() from None


def _relation(v1_positions: tuple[int, ...], v2_positions: tuple[int, ...]) -> str:
    if not v1_positions and not v2_positions:
        return "both_empty"
    if v1_positions == v2_positions:
        return "identical"
    v1_set = set(v1_positions)
    v2_set = set(v2_positions)
    if v1_set == v2_set:
        return "reordered"
    if v2_set < v1_set:
        return "v2_subset"
    if v1_set < v2_set:
        return "v2_superset"
    return "mixed"


def compare_memory_retrieval_v2_shadow(
    candidates: object,
    v1_selected_items: object,
    *,
    query_text: object,
) -> MemoryRetrievalV2ShadowReport:
    """Run V2 on an isolated snapshot and return only structural comparison."""

    try:
        if type(query_text) is not str:
            raise _ShadowUnavailable()
        query_text.encode("utf-8", errors="strict")
        original_candidates = _safe_candidate_tuple(candidates)
        v1_positions, v1_total_chars = _selected_positions(
            v1_selected_items,
            candidates=original_candidates,
        )
        planner_candidates = _safe_candidate_tuple(original_candidates)
        plan = memory_retrieval_v2.plan_memory_recall_v2(
            planner_candidates,
            query_text=query_text,
            scope_type="global_user",
            max_items=_MAX_SELECTED,
            character_budget=_MAX_FINAL_CHARS,
        )
        (
            v2_positions,
            v2_eligible_count,
            v2_total_chars,
            direct_count,
            cautious_count,
            associate_only_count,
        ) = _validated_v2_plan(plan, candidates=original_candidates)
        v1_set = set(v1_positions)
        v2_set = set(v2_positions)
        overlap_count = len(v1_set & v2_set)
        return MemoryRetrievalV2ShadowReport(
            status="completed",
            relation=_relation(v1_positions, v2_positions),
            candidate_count=len(original_candidates),
            v1_selected_count=len(v1_positions),
            v2_eligible_count=v2_eligible_count,
            v2_selected_count=len(v2_positions),
            overlap_count=overlap_count,
            v1_only_count=len(v1_positions) - overlap_count,
            v2_only_count=len(v2_positions) - overlap_count,
            v1_total_chars=v1_total_chars,
            v2_total_chars=v2_total_chars,
            direct_count=direct_count,
            cautious_count=cautious_count,
            associate_only_count=associate_only_count,
        )
    except BaseException:
        return MemoryRetrievalV2ShadowReport.failed()


__all__ = (
    "MemoryRetrievalV2ShadowReport",
    "SHADOW_FAILURE_CATEGORY",
    "compare_memory_retrieval_v2_shadow",
    "render_memory_retrieval_v2_shadow_telemetry",
)
