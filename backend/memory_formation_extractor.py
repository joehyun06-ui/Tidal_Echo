"""Strict provider-agnostic span extractor for shadow Memory formation.

The adapter owns only the model-call and JSON-contract boundary.  It never
accepts or returns candidate plaintext, never persists provider input/output,
and delegates all semantic acceptance to :mod:`backend.memory_formation`.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Final

from backend.memory_formation import (
    MAX_PROPOSALS as FORMATION_MAX_PROPOSALS,
    SIGNAL_KIND_MAPPING,
    SOURCE_MAX_CHARS,
    AutoMemoryProposalV1,
)


EXTRACTOR_CONTRACT_VERSION: Final = "memory-formation-extractor-v1"
MAX_PROPOSALS: Final = FORMATION_MAX_PROPOSALS
EXTRACTOR_RESPONSE_MAX_CHARS: Final = 4096
EXTRACTOR_MAX_TOKENS: Final = 128
EXTRACTOR_TEMPERATURE: Final = 0.0
EXTRACTOR_TIMEOUT_SECONDS: Final = 45.0
EXTRACTOR_SESSION_ID: Final = "memory-formation-extractor-v1"

_SAFE_CONTRACT_VALUE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_ERROR_CATEGORIES: Final = frozenset({
    "extractor_invalid_output",
    "extractor_timeout",
    "extractor_unavailable",
    "invalid_generation_callable",
    "invalid_provider_model",
    "invalid_provider_prompt_contract_version",
    "invalid_source_text",
    "memory_formation_extractor_error",
})

EXTRACTOR_INSTRUCTION: Final = """You are a deterministic durable-memory span proposal extractor.
Return JSON only, with exactly this shape:
{"version":"memory-formation-extractor-v1","proposals":[{"signal_type":"durable_preference","start":0,"end":1}]}
Identify at most 3 durable spans from the user message. Offsets are zero-based Python Unicode code-point indexes and end is exclusive.
Allowed signal_type values are exactly: durable_preference, stable_profile, relationship_fact, shared_episode, project_fact, project_decision, task_progress.
Never output span text, candidate text, normalized content, kind, scope, confidence, IDs, or explanations.
Use an empty proposals list when uncertain. Exclude roleplay, fiction, jokes, hypotheticals, third-party-only facts, transient states, and anything framed as do-not-store, do-not-remember, or forget.
"""


class MemoryFormationExtractorError(ValueError):
    """Stable data-free extractor failure."""

    __slots__ = ("category",)

    def __init__(self, category: object):
        safe_category = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_formation_extractor_error"
        )
        self.category = safe_category
        super().__init__(safe_category)

    def __str__(self) -> str:
        try:
            category = object.__getattribute__(self, "category")
        except Exception:
            return "memory_formation_extractor_error"
        return (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_formation_extractor_error"
        )

    def __repr__(self) -> str:
        return f"MemoryFormationExtractorError({str(self)!r})"


@dataclass(frozen=True, slots=True, repr=False)
class AutoMemoryExtractionV1:
    """Immutable proposals-only extractor result."""

    proposals: tuple[AutoMemoryProposalV1, ...] = field(repr=False)

    def __repr__(self) -> str:
        return "<AutoMemoryExtractionV1>"


GenerationCallable = Callable[
    [tuple[dict[str, str], ...], str, str, float, int, dict[str, Any]],
    Awaitable[dict[str, Any]],
]


def _raise(category: str) -> None:
    raise MemoryFormationExtractorError(category)


def _has_invalid_unicode(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite number")


def _parse_model_output(raw_output: object, source_length: int) -> AutoMemoryExtractionV1:
    if (
        type(raw_output) is not str
        or len(raw_output) > EXTRACTOR_RESPONSE_MAX_CHARS
        or _has_invalid_unicode(raw_output)
    ):
        _raise("extractor_invalid_output")
    try:
        payload = json.loads(
            raw_output,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
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

    proposals: list[AutoMemoryProposalV1] = []
    for raw_proposal in raw_proposals:
        if (
            type(raw_proposal) is not dict
            or set(raw_proposal) != {"signal_type", "start", "end"}
        ):
            _raise("extractor_invalid_output")
        signal_type = raw_proposal["signal_type"]
        start = raw_proposal["start"]
        end = raw_proposal["end"]
        if type(signal_type) is not str or signal_type not in SIGNAL_KIND_MAPPING:
            _raise("extractor_invalid_output")
        if type(start) is not int or type(end) is not int:
            _raise("extractor_invalid_output")
        if not 0 <= start < end <= source_length:
            _raise("extractor_invalid_output")
        proposals.append(AutoMemoryProposalV1(signal_type, start, end))
    return AutoMemoryExtractionV1(tuple(proposals))


async def extract_auto_memory_proposals(
    generation_callable: GenerationCallable,
    source_text: object,
    *,
    provider_model: object,
    provider_prompt_contract_version: object,
) -> AutoMemoryExtractionV1:
    """Make one bounded stateless extraction call and parse strict proposals."""

    if not callable(generation_callable):
        _raise("invalid_generation_callable")
    if (
        type(source_text) is not str
        or not source_text
        or len(source_text) > SOURCE_MAX_CHARS
        or _has_invalid_unicode(source_text)
    ):
        _raise("invalid_source_text")
    if (
        type(provider_model) is not str
        or not provider_model
        or provider_model != provider_model.strip()
        or len(provider_model) > 256
        or _has_invalid_unicode(provider_model)
    ):
        _raise("invalid_provider_model")
    if (
        type(provider_prompt_contract_version) is not str
        or _SAFE_CONTRACT_VALUE.fullmatch(provider_prompt_contract_version) is None
    ):
        _raise("invalid_provider_prompt_contract_version")

    provider_messages = (
        {"role": "developer", "content": EXTRACTOR_INSTRUCTION},
        {"role": "user", "content": source_text},
    )
    context = {
        "prompt_contract_version": provider_prompt_contract_version,
        "memory_formation_extractor": EXTRACTOR_CONTRACT_VERSION,
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
