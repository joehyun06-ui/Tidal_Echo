from __future__ import annotations

import dataclasses
import importlib
import inspect
import unittest
from unittest import mock

from backend import (
    memory_candidate_decision_adapters,
    memory_candidate_decision_composition,
    memory_candidate_decision_ledger,
    memory_service,
)


class MemoryCandidateDecisionCompositionTests(unittest.TestCase):
    def setUp(self):
        global memory_candidate_decision_adapters
        global memory_candidate_decision_composition
        global memory_candidate_decision_ledger, memory_service

        memory_candidate_decision_ledger = importlib.import_module(
            "backend.memory_candidate_decision_ledger"
        )
        memory_service = importlib.import_module("backend.memory_service")
        memory_candidate_decision_adapters = importlib.import_module(
            "backend.memory_candidate_decision_adapters"
        )
        memory_candidate_decision_composition = importlib.import_module(
            "backend.memory_candidate_decision_composition"
        )
        self.writer = object.__new__(memory_service.CandidateDecisionWriter)

    def test_exact_writer_is_shared_by_operator_and_mcp(self):
        composition = (
            memory_candidate_decision_composition.compose_candidate_decisions(
                self.writer
            )
        )
        self.assertIs(composition.writer, self.writer)
        self.assertIs(composition.operator._writer, self.writer)
        self.assertIs(composition.mcp._writer, self.writer)
        self.assertEqual(composition.operator._origin, "operator_cli")
        self.assertEqual(composition.mcp._origin, "mcp")
        self.assertIsNot(composition.operator, composition.mcp)

    def test_composition_is_frozen_slotted_and_repr_safe(self):
        composition = (
            memory_candidate_decision_composition.compose_candidate_decisions(
                self.writer
            )
        )
        self.assertEqual(
            tuple(
                field.name
                for field in dataclasses.fields(
                    memory_candidate_decision_composition
                    .MemoryCandidateDecisionComposition
                )
            ),
            ("writer", "operator", "mcp"),
        )
        self.assertEqual(
            repr(composition),
            "<MemoryCandidateDecisionComposition>",
        )
        self.assertFalse(hasattr(composition, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            composition.writer = object()

    def test_invalid_writer_fails_closed(self):
        with self.assertRaises(
            memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError
        ) as ctx:
            memory_candidate_decision_composition.compose_candidate_decisions(
                object()
            )
        self.assertEqual(
            ctx.exception.category,
            "candidate_decision_configuration_invalid",
        )

    def test_fixed_origins_remain_distinct_through_composition(self):
        bindings = []

        def record(_writer, *, binding):
            bindings.append(binding)
            return binding

        with mock.patch.object(
            memory_service.CandidateDecisionWriter,
            "decide",
            new=record,
        ):
            composition = (
                memory_candidate_decision_composition
                .compose_candidate_decisions(self.writer)
            )
            composition.operator.approve_candidate(
                memory_candidate_decision_adapters.ApproveCandidateRequestV1(
                    "1" * 32, "A" * 32
                )
            )
            composition.mcp.reject_candidate(
                memory_candidate_decision_adapters.RejectCandidateRequestV1(
                    "2" * 32, "B" * 32
                )
            )
        self.assertEqual(
            [(binding.origin, binding.decision) for binding in bindings],
            [("operator_cli", "approve"), ("mcp", "reject")],
        )

    def test_import_graph_has_no_runtime_bootstrap_or_broader_authority(self):
        source = inspect.getsource(memory_candidate_decision_composition)
        for forbidden in (
            "bootstrap_memory_runtime",
            "MemoryStore",
            "memory_explicit_actions",
            "memory_operator_composition",
            "memory_candidate_review",
            "FastAPI",
            "telegram",
            "operit",
            "kelivo",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn("memory_candidate_decision_adapters", source)
        self.assertIn("memory_service", source)


if __name__ == "__main__":
    unittest.main()
