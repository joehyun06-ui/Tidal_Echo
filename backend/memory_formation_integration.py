"""Shadow-only composition for automatic Memory formation.

This module receives a canonical source, invokes an injected extractor, runs
the Phase 4A deterministic builder, and immediately discards every candidate.
Only bounded status and count telemetry leaves the composition boundary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Final

from backend.memory_formation import MemoryFormationError, build_auto_memory_candidates
from backend.memory_formation_extractor import (
    AutoMemoryExtractionV1,
    MemoryFormationExtractorError,
)


_STATUSES: Final = frozenset({"completed", "failed"})
_CATEGORIES: Final = frozenset({
    "candidate_rejected",
    "completed",
    "extractor_invalid_output",
    "extractor_unavailable",
    "no_proposals",
    "source_ineligible",
})


@dataclass(frozen=True, slots=True, repr=False)
class MemoryFormationShadowResult:
    """Data-free outcome returned by the shadow composition layer."""

    status: str
    category: str
    proposal_count: int
    candidate_count: int

    def __repr__(self) -> str:
        return "<MemoryFormationShadowResult>"


ExtractorCallable = Callable[[str], Awaitable[AutoMemoryExtractionV1]]


def _result(
    status: str,
    category: str,
    proposal_count: int = 0,
    candidate_count: int = 0,
) -> MemoryFormationShadowResult:
    if status not in _STATUSES or category not in _CATEGORIES:
        status, category = "failed", "extractor_unavailable"
        proposal_count = candidate_count = 0
    return MemoryFormationShadowResult(
        status=status,
        category=category,
        proposal_count=max(0, min(int(proposal_count), 3)),
        candidate_count=max(0, min(int(candidate_count), 3)),
    )


async def run_memory_formation_shadow(
    source_message_id: object,
    source_text: object,
    extractor_callable: ExtractorCallable,
    *,
    max_item_chars: object,
) -> MemoryFormationShadowResult:
    """Run extractor and Phase 4A formation without retaining candidates."""

    if type(source_text) is not str or not callable(extractor_callable):
        return _result("failed", "extractor_unavailable")
    try:
        extraction = await extractor_callable(source_text)
    except asyncio.CancelledError:
        raise
    except MemoryFormationExtractorError as error:
        category = (
            "extractor_invalid_output"
            if error.category == "extractor_invalid_output"
            else "extractor_unavailable"
        )
        return _result("failed", category)
    except Exception:
        return _result("failed", "extractor_unavailable")

    if type(extraction) is not AutoMemoryExtractionV1:
        return _result("failed", "extractor_invalid_output")
    proposals = extraction.proposals
    if type(proposals) is not tuple:
        return _result("failed", "extractor_invalid_output")
    proposal_count = len(proposals)
    try:
        candidates = build_auto_memory_candidates(
            source_message_id,
            source_text,
            proposals,
            max_item_chars=max_item_chars,
        )
    except MemoryFormationError as error:
        category = (
            "source_ineligible"
            if error.category == "ineligible_proposal"
            else "candidate_rejected"
        )
        return _result("failed", category, proposal_count)
    candidate_count = len(candidates)
    del candidates
    if proposal_count == 0:
        return _result("completed", "no_proposals")
    return _result("completed", "completed", proposal_count, candidate_count)
