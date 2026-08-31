from __future__ import annotations

import asyncio
import dataclasses
import unittest
from types import SimpleNamespace
from unittest import mock

from backend import memory_formation_integration_v2 as integration
from backend.memory_formation_extractor_v2 import (
    AutoMemoryExtractionV2,
    MemoryFormationExtractorV2Error,
)
from backend.memory_formation_v2 import (
    AutoMemoryProposalV2,
    AutoMemorySourceSpanV2,
)


class AtomicFormationV2ShadowTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_span_structure_is_counted_but_candidate_text_is_discarded(self):
        source = (
            "Project Atlas uses PostgreSQL 16. filler. "
            "The project runs on port 5432."
        )
        first = source.index("Project Atlas uses PostgreSQL 16.")
        second = source.index("The project runs on port 5432.")
        proposal = AutoMemoryProposalV2(
            "project_fact",
            (
                AutoMemorySourceSpanV2(
                    first,
                    first + len("Project Atlas uses PostgreSQL 16."),
                ),
                AutoMemorySourceSpanV2(
                    second,
                    second + len("The project runs on port 5432."),
                ),
            ),
        )

        async def extract(received):
            self.assertEqual(received, source)
            return AutoMemoryExtractionV2((proposal,))

        secret = "PRIVATE-CANDIDATE-TEXT"
        candidates = (
            SimpleNamespace(
                normalized_content=secret,
                source_spans=proposal.spans,
            ),
        )
        with mock.patch.object(
            integration,
            "build_auto_memory_candidates_v2",
            return_value=candidates,
        ) as build:
            result = await integration.run_memory_formation_v2_shadow(
                47,
                source,
                extract,
                max_item_chars=777,
            )
        build.assert_called_once_with(
            47,
            source,
            (proposal,),
            max_item_chars=777,
        )
        self.assertEqual(
            dataclasses.asdict(result),
            {
                "status": "completed",
                "category": "completed",
                "proposal_count": 1,
                "candidate_count": 1,
                "multi_span_candidate_count": 1,
                "total_span_count": 2,
            },
        )
        self.assertEqual(repr(result), "<MemoryFormationV2ShadowResult>")
        self.assertNotIn(secret, repr(result))
        self.assertNotIn(secret, str(dataclasses.asdict(result)))

    async def test_shadow_api_has_no_persistence_callback_or_write_capability(self):
        fields = [
            field.name for field in dataclasses.fields(
                integration.MemoryFormationV2ShadowResult
            )
        ]
        self.assertEqual(
            fields,
            [
                "status",
                "category",
                "proposal_count",
                "candidate_count",
                "multi_span_candidate_count",
                "total_span_count",
            ],
        )
        parameter_names = tuple(
            integration.run_memory_formation_v2_shadow.__annotations__
        )
        self.assertNotIn("accepted_proposals_callable", parameter_names)
        self.assertNotIn("persistence", parameter_names)
        self.assertFalse(hasattr(integration, "memory_store"))
        self.assertFalse(hasattr(integration, "memory_service"))

    async def test_no_proposals_is_completed_without_structure_counts(self):
        async def extract(_source):
            return AutoMemoryExtractionV2(())

        result = await integration.run_memory_formation_v2_shadow(
            1,
            "canonical source",
            extract,
            max_item_chars=1000,
        )
        self.assertEqual(
            dataclasses.asdict(result),
            {
                "status": "completed",
                "category": "no_proposals",
                "proposal_count": 0,
                "candidate_count": 0,
                "multi_span_candidate_count": 0,
                "total_span_count": 0,
            },
        )

    async def test_source_veto_and_bad_candidate_are_bounded_failures(self):
        source = "Do not remember this. Project Atlas uses Python."
        selected = "Project Atlas uses Python."
        start = source.index(selected)
        proposal = AutoMemoryProposalV2(
            "project_fact",
            (AutoMemorySourceSpanV2(start, start + len(selected)),),
        )

        async def extract(_source):
            return AutoMemoryExtractionV2((proposal,))

        result = await integration.run_memory_formation_v2_shadow(
            1,
            source,
            extract,
            max_item_chars=1000,
        )
        self.assertEqual(
            (
                result.status,
                result.category,
                result.proposal_count,
                result.candidate_count,
            ),
            ("failed", "source_ineligible", 1, 0),
        )

    async def test_extractor_failures_are_data_free_and_do_not_run_builder(self):
        for source_category, expected in (
            ("extractor_timeout", "extractor_timeout"),
            ("extractor_invalid_output", "extractor_invalid_output"),
            ("extractor_unavailable", "extractor_unavailable"),
        ):
            secret = "PRIVATE-EXTRACTOR-DETAIL"

            async def extract(_source, category=source_category):
                raise MemoryFormationExtractorV2Error(category)

            with self.subTest(category=source_category), mock.patch.object(
                integration,
                "build_auto_memory_candidates_v2",
                side_effect=AssertionError(secret),
            ):
                result = await integration.run_memory_formation_v2_shadow(
                    1,
                    secret,
                    extract,
                    max_item_chars=1000,
                )
            self.assertEqual((result.status, result.category), ("failed", expected))
            self.assertNotIn(secret, repr(result))

    async def test_extractor_cancellation_propagates(self):
        async def extract(_source):
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await integration.run_memory_formation_v2_shadow(
                1,
                "source",
                extract,
                max_item_chars=1000,
            )

    async def test_malformed_extraction_type_is_rejected_before_builder(self):
        async def extract(_source):
            return SimpleNamespace(proposals=())

        with mock.patch.object(
            integration,
            "build_auto_memory_candidates_v2",
            side_effect=AssertionError("builder must not run"),
        ):
            result = await integration.run_memory_formation_v2_shadow(
                1,
                "source",
                extract,
                max_item_chars=1000,
            )
        self.assertEqual(
            (result.status, result.category),
            ("failed", "extractor_invalid_output"),
        )

    async def test_result_counts_are_hard_bounded_even_under_mocked_builder(self):
        proposals = tuple(
            AutoMemoryProposalV2(
                "project_fact",
                (AutoMemorySourceSpanV2(index, index + 1),),
            )
            for index in range(3)
        )

        async def extract(_source):
            return AutoMemoryExtractionV2(proposals)

        candidates = tuple(
            SimpleNamespace(source_spans=tuple(
                AutoMemorySourceSpanV2(index, index + 1)
                for index in range(20)
            ))
            for _ in range(20)
        )
        with mock.patch.object(
            integration,
            "build_auto_memory_candidates_v2",
            return_value=candidates,
        ):
            result = await integration.run_memory_formation_v2_shadow(
                1,
                "abc",
                extract,
                max_item_chars=1000,
            )
        self.assertEqual(result.proposal_count, 3)
        self.assertEqual(result.candidate_count, 3)
        self.assertEqual(result.multi_span_candidate_count, 3)
        self.assertEqual(result.total_span_count, 8)


if __name__ == "__main__":
    unittest.main()
