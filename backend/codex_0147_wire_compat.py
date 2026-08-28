"""Pinned Codex 0.147 wire compatibility for live-canary final answers.

Codex 0.147 serializes ``MessagePhase::FinalAnswer`` as ``"final_answer"``.
The original P2 projection expected a camelCase spelling, which caused completed
turns with valid agent text to be classified as empty.  Keep this compatibility
layer scoped to the alternate live-canary composition while qualification is active.
"""

from __future__ import annotations

from collections.abc import Mapping

from .codex_generation_protocol import (
    MAX_ASSISTANT_TEXT_CHARS,
    CorrelatedTurn,
    correlated_turn_from_page as _base_correlated_turn_from_page,
)


def final_answer_from_turn(turn: Mapping[str, object]) -> str | None:
    """Project the actual Codex 0.147 AgentMessageThreadItem phase values."""
    items = turn.get("items")
    if not isinstance(items, list):
        return None
    final: str | None = None
    fallback: str | None = None
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text or len(text) > MAX_ASSISTANT_TEXT_CHARS:
            continue
        phase = item.get("phase")
        if phase in {"final_answer", "finalAnswer"}:
            final = text
        elif phase is None:
            fallback = text
    return final if final is not None else fallback


def correlated_turn_from_page(page: object, client_message_id: str) -> CorrelatedTurn | None:
    """Reuse strict base correlation, then repair only the 0.147 phase projection."""
    correlated = _base_correlated_turn_from_page(page, client_message_id)
    if correlated is None or correlated.final_answer:
        return correlated
    if not isinstance(page, dict):
        return correlated
    turns = page.get("data", page.get("turns"))
    if not isinstance(turns, list):
        return correlated
    for raw_turn in turns:
        if not isinstance(raw_turn, dict) or raw_turn.get("id") != correlated.turn_id:
            continue
        return CorrelatedTurn(
            correlated.turn_id,
            correlated.status,
            final_answer_from_turn(raw_turn),
        )
    return correlated
