"""Narrow composition root for operator and admin-MCP decisions."""

from __future__ import annotations

from dataclasses import dataclass, field

try:
    from . import memory_candidate_decision_adapters, memory_service
except ImportError:  # support direct module execution in local tooling
    import memory_candidate_decision_adapters
    import memory_service


@dataclass(frozen=True, slots=True, repr=False)
class MemoryCandidateDecisionComposition:
    writer: memory_service.CandidateDecisionWriter = field(repr=False)
    operator: memory_candidate_decision_adapters.MemoryCandidateDecisionAdapter = field(
        repr=False
    )
    mcp: memory_candidate_decision_adapters.MemoryCandidateDecisionAdapter = field(
        repr=False
    )

    def __repr__(self) -> str:
        return "<MemoryCandidateDecisionComposition>"


def compose_candidate_decisions(
    writer: memory_service.CandidateDecisionWriter,
) -> MemoryCandidateDecisionComposition:
    exact_writer = memory_candidate_decision_adapters._require_writer(writer)
    operator = memory_candidate_decision_adapters.bind_operator_cli(exact_writer)
    mcp = memory_candidate_decision_adapters.bind_mcp(exact_writer)
    return MemoryCandidateDecisionComposition(
        writer=exact_writer,
        operator=operator,
        mcp=mcp,
    )
