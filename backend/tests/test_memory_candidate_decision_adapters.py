from __future__ import annotations

import dataclasses
import importlib
import inspect
import unittest
from unittest import mock

from backend import (
    memory_candidate_decision_adapters,
    memory_candidate_decision_ledger,
    memory_candidate_integrity,
    memory_service,
)


class MemoryCandidateDecisionAdapterTests(unittest.TestCase):
    def setUp(self):
        global memory_candidate_decision_adapters
        global memory_candidate_decision_ledger, memory_candidate_integrity
        global memory_service

        memory_candidate_decision_ledger = importlib.import_module(
            "backend.memory_candidate_decision_ledger"
        )
        memory_candidate_integrity = importlib.import_module(
            "backend.memory_candidate_integrity"
        )
        memory_service = importlib.import_module("backend.memory_service")
        memory_candidate_decision_adapters = importlib.import_module(
            "backend.memory_candidate_decision_adapters"
        )
        self.writer = object.__new__(memory_service.CandidateDecisionWriter)
        self.bindings = []

        def record(_writer, *, binding):
            self.bindings.append(binding)
            return binding

        patcher = mock.patch.object(
            memory_service.CandidateDecisionWriter,
            "decide",
            new=record,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.operator = memory_candidate_decision_adapters.bind_operator_cli(
            self.writer
        )
        self.mcp = memory_candidate_decision_adapters.bind_mcp(self.writer)

    @staticmethod
    def request_id(number: int = 1) -> str:
        return f"{number:032d}"

    @staticmethod
    def candidate_key(character: str = "A") -> str:
        return character * 32

    def assert_error(self, category: str, call, *args, **kwargs):
        with self.assertRaises(
            memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError
        ) as ctx:
            call(*args, **kwargs)
        self.assertEqual(ctx.exception.category, category)
        return ctx.exception

    def test_request_models_are_exact_frozen_slotted_and_data_free(self):
        cases = (
            (
                memory_candidate_decision_adapters.ApproveCandidateRequestV1,
                "<ApproveCandidateRequestV1>",
            ),
            (
                memory_candidate_decision_adapters.RejectCandidateRequestV1,
                "<RejectCandidateRequestV1>",
            ),
        )
        for request_type, expected_repr in cases:
            with self.subTest(request=request_type.__name__):
                self.assertEqual(
                    tuple(field.name for field in dataclasses.fields(request_type)),
                    ("request_id", "candidate_key"),
                )
                request = request_type(
                    self.request_id(),
                    self.candidate_key(),
                )
                self.assertEqual(repr(request), expected_repr)
                self.assertFalse(hasattr(request, "__dict__"))
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    request.request_id = self.request_id(2)
                for forbidden in (
                    "origin", "decision", "content", "kind", "scope",
                    "sensitivity", "confidence", "explicitness",
                    "fingerprint", "memory_id", "canonical_message_id",
                    "reason", "contract_version", "timestamp",
                ):
                    self.assertFalse(hasattr(request, forbidden))
                with self.assertRaises(TypeError):
                    request_type(
                        self.request_id(),
                        self.candidate_key(),
                        origin="web",
                    )

    def test_operator_and_mcp_origins_and_decisions_are_fixed(self):
        approve = memory_candidate_decision_adapters.ApproveCandidateRequestV1(
            self.request_id(10),
            self.candidate_key("A"),
        )
        reject = memory_candidate_decision_adapters.RejectCandidateRequestV1(
            self.request_id(11),
            self.candidate_key("B"),
        )
        self.operator.approve_candidate(approve)
        self.mcp.reject_candidate(reject)
        self.assertEqual(len(self.bindings), 2)
        operator_binding, mcp_binding = self.bindings
        self.assertEqual(operator_binding.origin, "operator_cli")
        self.assertEqual(operator_binding.decision, "approve")
        self.assertEqual(mcp_binding.origin, "mcp")
        self.assertEqual(mcp_binding.decision, "reject")
        for binding in self.bindings:
            self.assertEqual(
                binding.review_contract_version,
                memory_candidate_integrity.CANDIDATE_REVIEW_CONTRACT_VERSION,
            )
            self.assertEqual(
                binding.decision_contract_version,
                memory_candidate_decision_ledger.CANDIDATE_DECISION_CONTRACT_VERSION,
            )

    def test_exact_request_type_is_required_for_each_operation(self):
        approve = memory_candidate_decision_adapters.ApproveCandidateRequestV1(
            self.request_id(20), self.candidate_key("C")
        )
        reject = memory_candidate_decision_adapters.RejectCandidateRequestV1(
            self.request_id(21), self.candidate_key("D")
        )
        self.assert_error(
            "invalid_candidate_decision_request",
            self.operator.approve_candidate,
            reject,
        )
        self.assert_error(
            "invalid_candidate_decision_request",
            self.operator.reject_candidate,
            approve,
        )
        self.assert_error(
            "invalid_candidate_decision_request",
            self.operator.approve_candidate,
            {"request_id": self.request_id(20)},
        )
        self.assertEqual(self.bindings, [])

    def test_canonical_validation_is_reused_and_errors_are_data_free(self):
        secret_request = "not/a/request"
        secret_key = "not/a/candidate"
        request = memory_candidate_decision_adapters.ApproveCandidateRequestV1(
            secret_request,
            secret_key,
        )
        error = self.assert_error(
            "invalid_candidate_decision_request",
            self.operator.approve_candidate,
            request,
        )
        rendered = f"{error!s} {error!r}"
        self.assertNotIn(secret_request, rendered)
        self.assertNotIn(secret_key, rendered)

        request = memory_candidate_decision_adapters.RejectCandidateRequestV1(
            self.request_id(30),
            secret_key,
        )
        error = self.assert_error(
            "invalid_candidate_key",
            self.mcp.reject_candidate,
            request,
        )
        self.assertNotIn(secret_key, f"{error!s} {error!r}")

    def test_unexpected_writer_failure_is_closed_and_data_free(self):
        raw = "sensitive-writer-detail"
        with mock.patch.object(
            memory_service.CandidateDecisionWriter,
            "decide",
            side_effect=RuntimeError(raw),
        ):
            error = self.assert_error(
                "candidate_decision_state_invalid",
                self.operator.approve_candidate,
                memory_candidate_decision_adapters.ApproveCandidateRequestV1(
                    self.request_id(40), self.candidate_key("E")
                ),
            )
        self.assertNotIn(raw, f"{error!s} {error!r}")

    def test_public_surface_and_binders_are_exact(self):
        public_callables = {
            name
            for name, value in inspect.getmembers(
                memory_candidate_decision_adapters.MemoryCandidateDecisionAdapter
            )
            if callable(value) and not name.startswith("_")
        }
        self.assertEqual(
            public_callables,
            {"approve_candidate", "reject_candidate"},
        )
        for forbidden in (
            "bind", "bind_web", "bind_telegram", "bind_operit", "bind_pwa",
            "decide", "execute", "promote", "dismiss", "undo",
            "list_candidates", "get_candidate",
        ):
            self.assertFalse(
                hasattr(memory_candidate_decision_adapters, forbidden)
                or hasattr(
                    memory_candidate_decision_adapters.MemoryCandidateDecisionAdapter,
                    forbidden,
                )
            )
        error = self.assert_error(
            "candidate_decision_configuration_invalid",
            memory_candidate_decision_adapters.bind_operator_cli,
            object(),
        )
        self.assertEqual(str(error), "candidate_decision_configuration_invalid")


if __name__ == "__main__":
    unittest.main()
