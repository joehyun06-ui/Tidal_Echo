"""Shadow-only composition for Atomic Memory Formation V2.

This module is comparison-only. It invokes an injected V2 extractor, rebuilds
candidates from exact canonical source ranges, discards candidate plaintext,
and returns only bounded structural telemetry. It has no persistence callback
by design, so enabling this shadow can never contend with the V1
``memory_auto_formation_runs`` authority for the same canonical message.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Final

from backend.memory_formation_extractor_v2 import (
    AutoMemoryExtractionV2,
    MemoryFormationExtractorV2Error,
)
from backend.memory_formation_v2 import (
    AutoMemoryProposalV2,
    MemoryFormationV2Error,
    build_auto_memory_candidates_v2,
)


_STATUSES: Final = frozenset({"completed", "failed"})
_CATEGORIES: Final = frozenset({
    "candidate_rejected",
    "completed",
    "extractor_invalid_output",
    "extractor_timeout",
    "extractor_unavailable",
    "no_proposals",
    "source_ineligible",
})


@dataclass(frozen=True, slots=True, repr=False)
class MemoryFormationV2ShadowResult:
    """Data-free bounded V2 shadow outcome."""

    status: str
    category: str
    proposal_count: int
    candidate_count: int
    multi_span_candidate_count: int
    total_span_count: int

    def __repr__(self) -> str:
        return "<MemoryFormationV2ShadowResult>"


ExtractorCallableV2 = Callable[[str], Awaitable[AutoMemoryExtractionV2]]


def _result(
    status: str,
    category: str,
    proposal_count: int = 0,
    candidate_count: int = 0,
    multi_span_candidate_count: int = 0,
    total_span_count: int = 0,
) -> MemoryFormationV2ShadowResult:
    if status not in _STATUSES or category not in _CATEGORIES:
        status, category = "failed", "extractor_unavailable"
        proposal_count = candidate_count = multi_span_candidate_count = total_span_count = 0
    return MemoryFormationV2ShadowResult(
        status=status,
        category=category,
        proposal_count=max(0, min(int(proposal_count), 3)),
        candidate_count=max(0, min(int(candidate_count), 3)),
        multi_span_candidate_count=max(0, min(int(multi_span_candidate_count), 3)),
        total_span_count=max(0, min(int(total_span_count), 8)),
    )


async def run_memory_formation_v2_shadow(
    source_message_id: object,
    source_text: object,
    extractor_callable: ExtractorCallableV2,
    *,
    max_item_chars: object,
) -> MemoryFormationV2ShadowResult:
    """Run V2 extractor + deterministic builder and retain no candidate state."""

    if type(source_text) is not str or not callable(extractor_callable):
        return _result("failed", "extractor_unavailable")
    try:
        extraction = await extractor_callable(source_text)
    except asyncio.CancelledError:
        raise
    except MemoryFormationExtractorV2Error as error:
        if error.category == "extractor_timeout":
            category = "extractor_timeout"
        elif error.category == "extractor_invalid_output":
            category = "extractor_invalid_output"
        else:
            category = "extractor_unavailable"
        return _result("failed", category)
    except Exception:
        return _result("failed", "extractor_unavailable")

    if type(extraction) is not AutoMemoryExtractionV2:
        return _result("failed", "extractor_invalid_output")
    proposals = extraction.proposals
    if type(proposals) is not tuple:
        return _result("failed", "extractor_invalid_output")
    proposal_count = len(proposals)
    try:
        candidates = build_auto_memory_candidates_v2(
            source_message_id,
            source_text,
            proposals,
            max_item_chars=max_item_chars,
        )
    except MemoryFormationV2Error as error:
        category = (
            "source_ineligible"
            if error.category == "ineligible_proposal"
            else "candidate_rejected"
        )
        return _result("failed", category, proposal_count)

    candidate_count = len(candidates)
    multi_span_count = sum(
        1 for candidate in candidates if len(candidate.source_spans) > 1
    )
    total_span_count = sum(len(candidate.source_spans) for candidate in candidates)
    del candidates
    if proposal_count == 0:
        return _result("completed", "no_proposals")
    return _result(
        "completed",
        "completed",
        proposal_count,
        candidate_count,
        multi_span_count,
        total_span_count,
    )
