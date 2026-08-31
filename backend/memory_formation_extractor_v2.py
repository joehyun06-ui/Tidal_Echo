"""Strict multi-span proposal extractor for Atomic Memory Formation V2.

The provider may return only signal classes and canonical source ranges. It may
not author candidate plaintext, subject attribution, kind, scope, confidence,
identity, or explanations. All semantic acceptance and server-derived fields
remain owned by :mod:`backend.memory_formation_v2`.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Final

from backend import memory_formation
from backend import memory_formation_extractor
from backend import memory_formation_v2


EXTRACTOR_CONTRACT_VERSION: Final = "memory-formation-extractor-v2"
MAX_PROPOSALS: Final = memory_formation_v2.MAX_PROPOSALS
MAX_SPANS_PER_PROPOSAL: Final = memory_formation_v2.MAX_SPANS_PER_PROPOSAL
MAX_TOTAL_SPANS: Final = memory_formation_v2.MAX_TOTAL_SPANS
EXTRACTOR_RESPONSE_MAX_CHARS: Final = 6144
EXTRACTOR_MAX_TOKENS: Final = 256
EXTRACTOR_TEMPERATURE: Final = 0.0
EXTRACTOR_TIMEOUT_SECONDS: Final = memory_formation_extractor.EXTRACTOR_TIMEOUT_SECONDS
EXTRACTOR_SESSION_ID: Final = "memory-formation-extractor-v2"

_ERROR_CATEGORIES: Final = frozenset({
    "extractor_invalid_output",
    "extractor_timeout",
    "extractor_unavailable",
    "invalid_generation_callable",
    "invalid_provider_model",
    "invalid_provider_prompt_contract_version",
    "invalid_source_text",
    "memory_formation_extractor_v2_error",
})

EXTRACTOR_INSTRUCTION: Final = """You are a deterministic atomic durable-memory source-range extractor.
Return JSON only, with exactly this shape:
{"version":"memory-formation-extractor-v2","proposals":[{"signal_type":"project_fact","spans":[{"start":0,"end":10},{"start":20,"end":30}]}]}
Identify at most 3 atomic durable facts from the user message. Each proposal may bind 1 to 4 non-overlapping source spans, with at most 8 spans total across the response. Group multiple spans only when they jointly express the same atomic fact; do not join unrelated facts.
Offsets are zero-based Python Unicode code-point indexes and end is exclusive.
Allowed signal_type values are exactly: durable_preference, stable_profile, relationship_fact, shared_episode, project_fact, project_decision, task_progress.
Never output span text, candidate text, normalized content, subject, actor, kind, scope, confidence, IDs, tags, entities, summaries, explanations, or inferred facts.
Subject attribution is server-derived and must never be supplied by you.
Preserve exact source ranges for names, identifiers, dates, versions, numbers, ports, commit/deploy IDs, and other literals needed by the fact.
Use an empty proposals list when uncertain. Exclude roleplay, fiction, jokes, hypotheticals, third-party-only facts, transient states, and anything framed as do-not-store, do-not-remember, or forget.
"""


class MemoryFormationExtractorV2Error(ValueError):
    """Stable data-free extractor V2 failure."""

    __slots__ = ("category",)

    def __init__(self, category: object):
        safe_category = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_formation_extractor_v2_error"
        )
        self.category = safe_category
        super().__init__(safe_category)

    def __str__(self) -> str:
        try:
            category = object.__getattribute__(self, "category")
        except Exception:
            return "memory_formation_extractor_v2_error"
        return (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_formation_extractor_v2_error"
        )

    def __repr__(self) -> str:
        return f"MemoryFormationExtractorV2Error({str(self)!r})"


@dataclass(frozen=True, slots=True, repr=False)
class AutoMemoryExtractionV2:
    proposals: tuple[memory_formation_v2.AutoMemoryProposalV2, ...] = field(
        repr=False
    )

    def __repr__(self) -> str:
        return "<AutoMemoryExtractionV2>"


GenerationCallable = Callable[
    [tuple[dict[str, str], ...], str, str, float, int, dict[str, Any]],
    Awaitable[dict[str, Any]],
]


def _raise(category: str) -> None:
    raise MemoryFormationExtractorV2Error(category)


def _parse_model_output(
    raw_output: object,
    source_length: int,
) -> AutoMemoryExtractionV2:
    if (
        type(raw_output) is not str
        or len(raw_output) > EXTRACTOR_RESPONSE_MAX_CHARS
        or memory_formation_extractor._has_invalid_unicode(raw_output)
    ):
        _raise("extractor_invalid_output")
    try:
        payload = json.loads(
            raw_output,
            object_pairs_hook=memory_formation_extractor._reject_duplicate_json_keys,
            parse_constant=memory_formation_extractor._reject_json_constant,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError):
        _raise("extractor_invalid_output")
    if type(payload) is not dict or set(payload) != {"version", "proposals"}:
        _raise("extractor_invalid_output")
    if payload["version"] != EXTRACTOR_CONTRACT_VERSION:
        _raise("extractor_invalid_output")
    raw_proposals = payload["proposals"]
    if type(raw_proposals) is not list or len(raw_proposals) > MAX_PROPOSALS:
        _raise("extractor_invalid_output")

    proposals: list[memory_formation_v2.AutoMemoryProposalV2] = []
    for raw_proposal in raw_proposals:
        if (
            type(raw_proposal) is not dict
            or set(raw_proposal) != {"signal_type", "spans"}
        ):
            _raise("extractor_invalid_output")
        signal_type = raw_proposal["signal_type"]
        raw_spans = raw_proposal["spans"]
        if (
            type(signal_type) is not str
            or signal_type not in memory_formation_v2.SIGNAL_KIND_MAPPING
            or type(raw_spans) is not list
            or not 1 <= len(raw_spans) <= MAX_SPANS_PER_PROPOSAL
        ):
            _raise("extractor_invalid_output")
        spans: list[memory_formation_v2.AutoMemorySourceSpanV2] = []
        for raw_span in raw_spans:
            if type(raw_span) is not dict or set(raw_span) != {"start", "end"}:
                _raise("extractor_invalid_output")
            start = raw_span["start"]
            end = raw_span["end"]
            if type(start) is not int or type(end) is not int:
                _raise("extractor_invalid_output")
            if not 0 <= start < end <= source_length:
                _raise("extractor_invalid_output")
            spans.append(memory_formation_v2.AutoMemorySourceSpanV2(start, end))
        proposals.append(
            memory_formation_v2.AutoMemoryProposalV2(
                signal_type,
                tuple(spans),
            )
        )

    try:
        validated = memory_formation_v2.validate_auto_memory_proposals(
            proposals,
            source_length=source_length,
        )
    except memory_formation_v2.MemoryFormationV2Error:
        _raise("extractor_invalid_output")
    return AutoMemoryExtractionV2(validated)


async def extract_auto_memory_proposals_v2(
    generation_callable: GenerationCallable,
    source_text: object,
    *,
    provider_model: object,
    provider_prompt_contract_version: object,
) -> AutoMemoryExtractionV2:
    """Make one bounded stateless extraction call and parse strict V2 ranges."""

    if not callable(generation_callable):
        _raise("invalid_generation_callable")
    if (
        type(source_text) is not str
        or not source_text
        or len(source_text) > memory_formation.SOURCE_MAX_CHARS
        or memory_formation_extractor._has_invalid_unicode(source_text)
    ):
        _raise("invalid_source_text")
    if (
        type(provider_model) is not str
        or not provider_model
        or provider_model != provider_model.strip()
        or len(provider_model) > 256
        or memory_formation_extractor._has_invalid_unicode(provider_model)
    ):
        _raise("invalid_provider_model")
    if (
        type(provider_prompt_contract_version) is not str
        or memory_formation_extractor._SAFE_CONTRACT_VALUE.fullmatch(
            provider_prompt_contract_version
        )
        is None
    ):
        _raise("invalid_provider_prompt_contract_version")

    provider_messages = (
        {"role": "developer", "content": EXTRACTOR_INSTRUCTION},
        {"role": "user", "content": source_text},
    )
    context = {
        "prompt_contract_version": provider_prompt_contract_version,
        "memory_formation_extractor": EXTRACTOR_CONTRACT_VERSION,
        "memory_formation_contract": memory_formation_v2.FORMATION_CONTRACT_VERSION,
    }
    try:
        async with asyncio.timeout(EXTRACTOR_TIMEOUT_SECONDS):
            response = await generation_callable(
                provider_messages,
                EXTRACTOR_SESSION_ID,
                provider_model,
                EXTRACTOR_TEMPERATURE,
                EXTRACTOR_MAX_TOKENS,
                context,
            )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        _raise("extractor_timeout")
    except Exception:
        _raise("extractor_unavailable")
    if type(response) is not dict:
        _raise("extractor_invalid_output")
    return _parse_model_output(response.get("text"), len(source_text))
