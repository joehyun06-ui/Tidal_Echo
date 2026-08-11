"""Closed, read-only operator adapters for candidate review."""

from __future__ import annotations

try:
    from . import memory_candidate_review
except ImportError:  # support direct module execution in local tooling
    import memory_candidate_review


_OPERATOR_BINDING = object()
_MCP_BINDING = object()


class MemoryCandidateReviewAdapter:
    """A fixed-origin pass-through with no write or generic delegation API."""

    __slots__ = ("_service", "_origin")

    def __init__(
        self,
        service: memory_candidate_review.MemoryCandidateReviewService,
        *,
        _binding: object,
    ):
        if type(service) is not memory_candidate_review.MemoryCandidateReviewService:
            raise memory_candidate_review.MemoryCandidateReviewError(
                "candidate_review_configuration_invalid"
            )
        if _binding is _OPERATOR_BINDING:
            origin = "operator_cli"
        elif _binding is _MCP_BINDING:
            origin = "mcp"
        else:
            raise memory_candidate_review.MemoryCandidateReviewError(
                "candidate_review_configuration_invalid"
            )
        self._service = service
        self._origin = origin

    def __repr__(self) -> str:
        return "<MemoryCandidateReviewAdapter>"

    def list_candidates(
        self,
        *,
        limit: int = memory_candidate_review.DEFAULT_CANDIDATE_LIMIT,
        after_candidate_key: str | None = None,
        kind: str | None = None,
    ) -> tuple[memory_candidate_review.CandidateReviewSummaryV1, ...]:
        try:
            return self._service.list_candidates(
                limit=limit,
                after_candidate_key=after_candidate_key,
                kind=kind,
            )
        except memory_candidate_review.MemoryCandidateReviewError:
            raise
        except Exception:
            raise memory_candidate_review.MemoryCandidateReviewError(
                "candidate_review_state_invalid"
            ) from None

    def get_candidate(
        self,
        candidate_key: str,
    ) -> memory_candidate_review.CandidateReviewDetailV1:
        try:
            return self._service.get_candidate(candidate_key)
        except memory_candidate_review.MemoryCandidateReviewError:
            raise
        except Exception:
            raise memory_candidate_review.MemoryCandidateReviewError(
                "candidate_review_state_invalid"
            ) from None


def bind_operator_cli(
    service: memory_candidate_review.MemoryCandidateReviewService,
) -> MemoryCandidateReviewAdapter:
    if type(service) is not memory_candidate_review.MemoryCandidateReviewService:
        raise memory_candidate_review.MemoryCandidateReviewError(
            "candidate_review_configuration_invalid"
        )
    return MemoryCandidateReviewAdapter(service, _binding=_OPERATOR_BINDING)


def bind_mcp(
    service: memory_candidate_review.MemoryCandidateReviewService,
) -> MemoryCandidateReviewAdapter:
    if type(service) is not memory_candidate_review.MemoryCandidateReviewService:
        raise memory_candidate_review.MemoryCandidateReviewError(
            "candidate_review_configuration_invalid"
        )
    return MemoryCandidateReviewAdapter(service, _binding=_MCP_BINDING)
