"""V2-aware terminal candidate decision adapters with unchanged public requests.

The operator/MCP request envelope and immutable decision ledger remain the
reviewed V1 contract.  Only the bound writer is V2-aware, so multi-span evidence
is re-proved before terminal approve/reject mutations.
"""

from __future__ import annotations

from backend import (
    memory_candidate_decision_adapters,
    memory_candidate_decision_ledger,
    memory_candidate_decision_v2,
)


ApproveCandidateRequestV1 = memory_candidate_decision_adapters.ApproveCandidateRequestV1
RejectCandidateRequestV1 = memory_candidate_decision_adapters.RejectCandidateRequestV1

_OPERATOR_BINDING = object()
_MCP_BINDING = object()


def _raise(category: str) -> None:
    raise memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError(category)


def _require_writer(
    writer: object,
) -> memory_candidate_decision_v2.CandidateDecisionWriterV2:
    if type(writer) is not memory_candidate_decision_v2.CandidateDecisionWriterV2:
        _raise("candidate_decision_configuration_invalid")
    return writer


class MemoryCandidateDecisionAdapterV2:
    """Two-operation fixed-origin adapter backed by the V2 proof engine."""

    __slots__ = ("_writer", "_origin")

    def __init__(self, writer: object, *, _binding: object):
        self._writer = _require_writer(writer)
        if _binding is _OPERATOR_BINDING:
            self._origin = "operator_cli"
        elif _binding is _MCP_BINDING:
            self._origin = "mcp"
        else:
            _raise("candidate_decision_configuration_invalid")

    def __repr__(self) -> str:
        return "<MemoryCandidateDecisionAdapterV2>"

    def _decide(self, request: object, decision: str):
        expected = (
            ApproveCandidateRequestV1
            if decision == "approve"
            else RejectCandidateRequestV1
        )
        if type(request) is not expected:
            _raise("invalid_candidate_decision_request")
        binding = memory_candidate_decision_ledger.CandidateDecisionLedgerBindingV1(
            request_id=request.request_id,
            candidate_key=request.candidate_key,
            origin=self._origin,
            decision=decision,
        )
        memory_candidate_decision_ledger.validate_binding(binding)
        try:
            return self._writer.decide(binding=binding)
        except memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError:
            raise
        except Exception:
            _raise("candidate_decision_state_invalid")

    def approve_candidate(self, request: ApproveCandidateRequestV1):
        return self._decide(request, "approve")

    def reject_candidate(self, request: RejectCandidateRequestV1):
        return self._decide(request, "reject")


def bind_operator_cli(writer: object) -> MemoryCandidateDecisionAdapterV2:
    return MemoryCandidateDecisionAdapterV2(
        _require_writer(writer),
        _binding=_OPERATOR_BINDING,
    )


def bind_mcp(writer: object) -> MemoryCandidateDecisionAdapterV2:
    return MemoryCandidateDecisionAdapterV2(
        _require_writer(writer),
        _binding=_MCP_BINDING,
    )
