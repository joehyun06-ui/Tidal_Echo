"""Pure multi-span Atomic Memory formation contract for Phase 4D-A.

V2 is intentionally parallel to the deployed V1 contract. It introduces
multi-span source binding and deterministic subject attribution while remaining
candidate-only: no persistence, provider, network, history scan, or runtime
wiring lives here.

The model/caller may propose only a closed signal type and one or more source
ranges. Candidate text is always reconstructed from the canonical source
ranges; subject/kind/scope/sensitivity are server-derived.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from backend import memory_formation
from backend.memory_policy import MemoryPolicy, MemoryPolicyError


MAX_PROPOSALS: Final = memory_formation.MAX_PROPOSALS
MAX_SPANS_PER_PROPOSAL: Final = 4
MAX_TOTAL_SPANS: Final = 8
FORMATION_CONTRACT_VERSION: Final = "memory-formation-v2"
SOURCE_JOINER: Final = "\n"

SIGNAL_KIND_MAPPING = memory_formation.SIGNAL_KIND_MAPPING
SUBJECT_BY_SIGNAL: Final = MappingProxyType({
    "durable_preference": "user",
    "stable_profile": "user",
    "relationship_fact": "user",
    "shared_episode": "user",
    "project_fact": "project",
    "project_decision": "project",
    "task_progress": "project",
})

_ERROR_CATEGORIES: Final = frozenset({
    "candidate_budget_exceeded",
    "candidate_policy_rejected",
    "duplicate_proposal",
    "duplicate_span",
    "empty_spans",
    "ineligible_proposal",
    "invalid_max_item_chars",
    "invalid_proposal",
    "invalid_proposals",
    "invalid_signal_type",
    "invalid_source_message_id",
    "invalid_source_text",
    "invalid_span",
    "invalid_spans",
    "memory_formation_v2_error",
    "overlapping_proposals",
    "overlapping_spans",
    "source_text_too_long",
    "too_many_proposals",
    "too_many_spans",
    "too_many_total_spans",
})


class MemoryFormationV2Error(ValueError):
    """A stable, data-free Atomic Formation V2 failure."""

    __slots__ = ("category",)

    def __init__(self, category: object):
        safe_category = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_formation_v2_error"
        )
        self.category = safe_category
        super().__init__(safe_category)

    def __str__(self) -> str:
        try:
            category = object.__getattribute__(self, "category")
        except Exception:
            return "memory_formation_v2_error"
        return (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_formation_v2_error"
        )

    def __repr__(self) -> str:
        return f"MemoryFormationV2Error({str(self)!r})"


@dataclass(frozen=True, slots=True, repr=False)
class AutoMemorySourceSpanV2:
    """One exact half-open source range."""

    start: int
    end: int

    def __repr__(self) -> str:
        return "<AutoMemorySourceSpanV2>"


@dataclass(frozen=True, slots=True, repr=False)
class AutoMemoryProposalV2:
    """One atomic fact proposal backed by one or more exact source ranges."""

    signal_type: str
    spans: tuple[AutoMemorySourceSpanV2, ...] = field(repr=False)

    def __repr__(self) -> str:
        return "<AutoMemoryProposalV2>"


@dataclass(frozen=True, slots=True, repr=False)
class AutoMemoryCandidateV2:
    """A policy-validated V2 candidate; this type performs no Memory write."""

    source_message_id: int
    signal_type: str
    kind: str
    subject: str
    source_spans: tuple[AutoMemorySourceSpanV2, ...] = field(repr=False)
    scope_type: str
    scope_ref: str
    normalized_content: str = field(repr=False)
    sensitivity: str

    def __repr__(self) -> str:
        return "<AutoMemoryCandidateV2>"


def _raise(category: str) -> None:
    raise MemoryFormationV2Error(category)


def _translate_v1_error(error: memory_formation.MemoryFormationError) -> None:
    category = getattr(error, "category", "")
    if category in _ERROR_CATEGORIES:
        _raise(category)
    _raise("memory_formation_v2_error")


def _validate_source(source_message_id: object, source_text: object) -> str:
    try:
        return memory_formation._validate_source(source_message_id, source_text)
    except memory_formation.MemoryFormationError as error:
        _translate_v1_error(error)
    raise AssertionError("unreachable")


def _validate_max_item_chars(max_item_chars: object) -> int:
    try:
        return memory_formation._validate_max_item_chars(max_item_chars)
    except memory_formation.MemoryFormationError as error:
        _translate_v1_error(error)
    raise AssertionError("unreachable")


def _validate_one_span(
    span: object,
    *,
    source_length: int,
) -> AutoMemorySourceSpanV2:
    if type(span) is not AutoMemorySourceSpanV2:
        _raise("invalid_span")
    start = span.start
    end = span.end
    if type(start) is not int or type(end) is not int:
        _raise("invalid_span")
    if not 0 <= start < end <= source_length:
        _raise("invalid_span")
    return span


def _validated_spans(
    spans: object,
    *,
    source_length: int,
) -> tuple[AutoMemorySourceSpanV2, ...]:
    if type(spans) is not tuple:
        _raise("invalid_spans")
    if not spans:
        _raise("empty_spans")
    if len(spans) > MAX_SPANS_PER_PROPOSAL:
        _raise("too_many_spans")

    validated: list[AutoMemorySourceSpanV2] = []
    seen: set[tuple[int, int]] = set()
    for raw_span in spans:
        span = _validate_one_span(raw_span, source_length=source_length)
        identity = (span.start, span.end)
        if identity in seen:
            _raise("duplicate_span")
        seen.add(identity)
        validated.append(span)

    result = tuple(sorted(validated, key=lambda item: (item.start, item.end)))
    previous_end = -1
    for span in result:
        if span.start < previous_end:
            _raise("overlapping_spans")
        previous_end = span.end
    return result


def validate_auto_memory_proposals(
    proposals: object,
    *,
    source_length: int,
) -> tuple[AutoMemoryProposalV2, ...]:
    """Validate and canonicalize exact V2 proposal objects."""

    if type(source_length) is not int or source_length < 0:
        _raise("invalid_source_text")
    if type(proposals) not in (list, tuple):
        _raise("invalid_proposals")
    if len(proposals) > MAX_PROPOSALS:
        _raise("too_many_proposals")

    validated: list[AutoMemoryProposalV2] = []
    seen_proposals: set[tuple[str, tuple[tuple[int, int], ...]]] = set()
    all_spans: list[tuple[int, int]] = []
    total_spans = 0

    for proposal in proposals:
        if type(proposal) is not AutoMemoryProposalV2:
            _raise("invalid_proposal")
        signal_type = proposal.signal_type
        if type(signal_type) is not str or signal_type not in SIGNAL_KIND_MAPPING:
            _raise("invalid_signal_type")
        spans = _validated_spans(proposal.spans, source_length=source_length)
        total_spans += len(spans)
        if total_spans > MAX_TOTAL_SPANS:
            _raise("too_many_total_spans")
        identity = (
            signal_type,
            tuple((span.start, span.end) for span in spans),
        )
        if identity in seen_proposals:
            _raise("duplicate_proposal")
        seen_proposals.add(identity)
        all_spans.extend((span.start, span.end) for span in spans)
        validated.append(AutoMemoryProposalV2(signal_type, spans))

    globally_sorted = sorted(all_spans)
    previous_end = -1
    for start, end in globally_sorted:
        if start < previous_end:
            _raise("overlapping_proposals")
        previous_end = end

    return tuple(sorted(
        validated,
        key=lambda item: (
            item.spans[0].start,
            item.spans[0].end,
            item.signal_type,
            tuple((span.start, span.end) for span in item.spans),
        ),
    ))


def _candidate_source_text(
    source_text: str,
    spans: tuple[AutoMemorySourceSpanV2, ...],
) -> str:
    return SOURCE_JOINER.join(
        source_text[span.start:span.end]
        for span in spans
    )


def build_auto_memory_candidates_v2(
    source_message_id: object,
    source_text: object,
    proposals: object,
    *,
    max_item_chars: object = memory_formation.DEFAULT_MAX_ITEM_CHARS,
) -> tuple[AutoMemoryCandidateV2, ...]:
    """Build deterministic multi-span candidates from canonical source slices."""

    validated_source = _validate_source(source_message_id, source_text)
    validated_max_item_chars = _validate_max_item_chars(max_item_chars)
    validated_proposals = validate_auto_memory_proposals(
        proposals,
        source_length=len(validated_source),
    )
    if not validated_proposals:
        return ()

    source_eligibility_view = memory_formation._source_eligibility_view(
        validated_source
    )
    if memory_formation._source_has_formation_veto(source_eligibility_view):
        _raise("ineligible_proposal")

    try:
        policy = MemoryPolicy(
            max_item_chars=validated_max_item_chars,
            sensitive_storage_enabled=False,
        )
        policy.validate_scope(
            memory_formation.SCOPE_TYPE,
            memory_formation.SCOPE_REF,
        )
    except MemoryPolicyError:
        _raise("candidate_policy_rejected")

    candidates: list[AutoMemoryCandidateV2] = []
    total_chars = 0
    for proposal in validated_proposals:
        kind = SIGNAL_KIND_MAPPING[proposal.signal_type]
        subject = SUBJECT_BY_SIGNAL[proposal.signal_type]
        source_projection = _candidate_source_text(
            validated_source,
            proposal.spans,
        )
        try:
            policy.validate_kind(kind)
            normalized_content = policy.validate_content(
                source_projection,
                memory_formation.SENSITIVITY,
            )
        except MemoryPolicyError:
            _raise("candidate_policy_rejected")
        if not memory_formation._is_eligible(
            proposal.signal_type,
            normalized_content,
        ):
            _raise("ineligible_proposal")
        total_chars += len(normalized_content)
        if total_chars > memory_formation.TOTAL_CANDIDATE_MAX_CHARS:
            _raise("candidate_budget_exceeded")
        candidates.append(AutoMemoryCandidateV2(
            source_message_id=source_message_id,
            signal_type=proposal.signal_type,
            kind=kind,
            subject=subject,
            source_spans=proposal.spans,
            scope_type=memory_formation.SCOPE_TYPE,
            scope_ref=memory_formation.SCOPE_REF,
            normalized_content=normalized_content,
            sensitivity=memory_formation.SENSITIVITY,
        ))
    return tuple(candidates)
