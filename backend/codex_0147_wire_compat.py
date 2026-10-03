"""Backward import compatibility for callers of the former 0.147 adapter.

Both historic fixtures and the pinned 0.160 wire shape are handled by the shared
projection. Runtime code imports codex_generation_protocol directly.
"""
from .codex_generation_protocol import correlated_turn_from_page, final_answer_from_turn

__all__ = ["correlated_turn_from_page", "final_answer_from_turn"]
