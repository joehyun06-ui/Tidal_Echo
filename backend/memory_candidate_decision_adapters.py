"""Fixed-origin operator adapters for terminal candidate decisions."""

from __future__ import annotations

from dataclasses import dataclass, field

try:
    from . import memory_candidate_decision_ledger, memory_service
except ImportError:  # support direct module execution in local tooling
    import memory_candidate_decision_ledger
    import memory_service


_OPERATOR_BINDING = object()
_MCP_BINDING = object()


@dataclass(frozen=True, slots=True, repr=False)
class ApproveCandidateRequestV1:
    request_id: str = field(repr=False)
    candidate_key: str = field(repr=False)

    def __repr__(self) -> str:
        return "<ApproveCandidateRequestV1>"


@dataclass(frozen=True, slots=True, repr=False)
class RejectCandidateRequestV1:
    request_id: str = field(repr=False)
    candidate_key: str = field(repr=False)

    def __repr__(self) -> str:
        return "<RejectCandidateRequestV1>"


def _raise(category: str) -> None:
    raise memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError(
        category
    )


def _require_writer(writer: object) -> memory_service.CandidateDecisionWriter:
    if type(writer) is not memory_service.CandidateDecisionWriter:
        _raise("candidate_decision_configuration_invalid")
    return writer


class MemoryCandidateDecisionAdapter:
    """A two-operation capability with a constructor-fixed origin."""

    __slots__ = ("_writer", "_origin")

    def __init__(
        self,
        writer: memory_service.CandidateDecisionWriter,
        *,
        _binding: object,
    ):
        self._writer = _require_writer(writer)
        if _binding is _OPERATOR_BINDING:
            origin = "operator_cli"
        elif _binding is _MCP_BINDING:
            origin = "mcp"
        else:
            _raise("candidate_decision_configuration_invalid")
        self._origin = origin

    def __repr__(self) -> str:
        return "<MemoryCandidateDecisionAdapter>"

    def approve_candidate(
        self,
        request: ApproveCandidateRequestV1,
    ) -> memory_candidate_decision_ledger.CandidateDecisionResultV1:
        if type(request) is not ApproveCandidateRequestV1:
            _raise("invalid_candidate_decision_request")
        binding = memory_candidate_decision_ledger.CandidateDecisionLedgerBindingV1(
            request_id=request.request_id,
            candidate_key=request.candidate_key,
            origin=self._origin,
            decision="approve",
        )
        memory_candidate_decision_ledger.validate_binding(binding)
        try:
            return self._writer.decide(binding=binding)
        except memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError:
            raise
        except Exception:
            _raise("candidate_decision_state_invalid")

    def reject_candidate(
        self,
        request: RejectCandidateRequestV1,
    ) -> memory_candidate_decision_ledger.CandidateDecisionResultV1:
        if type(request) is not RejectCandidateRequestV1:
            _raise("invalid_candidate_decision_request")
        binding = memory_candidate_decision_ledger.CandidateDecisionLedgerBindingV1(
            request_id=request.request_id,
            candidate_key=request.candidate_key,
            origin=self._origin,
            decision="reject",
        )
        memory_candidate_decision_ledger.validate_binding(binding)
        try:
            return self._writer.decide(binding=binding)
        except memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError:
            raise
        except Exception:
            _raise("candidate_decision_state_invalid")


def bind_operator_cli(
    writer: memory_service.CandidateDecisionWriter,
) -> MemoryCandidateDecisionAdapter:
    return MemoryCandidateDecisionAdapter(
        _require_writer(writer),
        _binding=_OPERATOR_BINDING,
    )


def bind_mcp(
    writer: memory_service.CandidateDecisionWriter,
) -> MemoryCandidateDecisionAdapter:
    return MemoryCandidateDecisionAdapter(
        _require_writer(writer),
        _binding=_MCP_BINDING,
    )
