from __future__ import annotations

import dataclasses
import unittest

from backend import memory_formation
from backend import memory_formation_v2 as formation


def source_span(source: str, text: str) -> formation.AutoMemorySourceSpanV2:
    start = source.index(text)
    return formation.AutoMemorySourceSpanV2(start, start + len(text))


def proposal(
    source: str,
    signal_type: str,
    *parts: str,
) -> formation.AutoMemoryProposalV2:
    return formation.AutoMemoryProposalV2(
        signal_type,
        tuple(source_span(source, part) for part in parts),
    )


class AtomicMemoryFormationV2Tests(unittest.TestCase):
    def assert_category(self, expected: str, callable_, *args, **kwargs):
        with self.assertRaises(formation.MemoryFormationV2Error) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.category, expected)
        self.assertEqual(str(raised.exception), expected)

    def test_multi_span_candidate_keeps_exact_literals_and_drops_unselected_filler(self):
        source = (
            "Project Atlas uses PostgreSQL 16. "
            "UNRELATED FILLER MUST NOT ENTER MEMORY. "
            "The service stays on port 5432 with release 2026.08.31."
        )
        item = proposal(
            source,
            "project_fact",
            "Project Atlas uses PostgreSQL 16.",
            "The service stays on port 5432 with release 2026.08.31.",
        )
        result = formation.build_auto_memory_candidates_v2(
            91,
            source,
            (item,),
        )
        self.assertEqual(len(result), 1)
        candidate = result[0]
        self.assertEqual(candidate.source_message_id, 91)
        self.assertEqual(candidate.signal_type, "project_fact")
        self.assertEqual(candidate.kind, "project")
        self.assertEqual(candidate.subject, "project")
        self.assertEqual(
            candidate.normalized_content,
            (
                "Project Atlas uses PostgreSQL 16. "
                "The service stays on port 5432 with release 2026.08.31."
            ),
        )
        for literal in (
            "Atlas",
            "PostgreSQL",
            "16",
            "5432",
            "2026.08.31",
        ):
            self.assertIn(literal, candidate.normalized_content)
        self.assertNotIn("UNRELATED FILLER", candidate.normalized_content)
        self.assertEqual(
            tuple(
                source[span.start:span.end]
                for span in candidate.source_spans
            ),
            (
                "Project Atlas uses PostgreSQL 16.",
                "The service stays on port 5432 with release 2026.08.31.",
            ),
        )

    def test_subject_attribution_is_server_derived_from_closed_signal_class(self):
        expected = {
            "durable_preference": "user",
            "stable_profile": "user",
            "relationship_fact": "user",
            "shared_episode": "user",
            "project_fact": "project",
            "project_decision": "project",
            "task_progress": "project",
        }
        self.assertEqual(dict(formation.SUBJECT_BY_SIGNAL), expected)
        self.assertEqual(
            [field.name for field in dataclasses.fields(formation.AutoMemoryProposalV2)],
            ["signal_type", "spans"],
        )
        self.assertNotIn(
            "subject",
            [field.name for field in dataclasses.fields(formation.AutoMemoryProposalV2)],
        )

        positive = {
            "durable_preference": "I usually prefer window seats.",
            "stable_profile": "I work as a product designer.",
            "relationship_fact": "My wife is Alex.",
            "shared_episode": "We visited Kyoto together last year.",
            "project_fact": "Project Atlas uses Python.",
            "project_decision": "We decided to use PostgreSQL for the project database.",
            "task_progress": "The API migration is finished.",
        }
        for signal_type, source in positive.items():
            with self.subTest(signal_type=signal_type):
                candidate = formation.build_auto_memory_candidates_v2(
                    7,
                    source,
                    (proposal(source, signal_type, source),),
                )[0]
                self.assertEqual(candidate.subject, expected[signal_type])

    def test_unsorted_spans_and_proposals_are_canonicalized_by_source_order(self):
        source = (
            "I usually prefer window seats. "
            "Project Atlas uses Python. "
            "The service runs on Render."
        )
        preference = proposal(
            source,
            "durable_preference",
            "I usually prefer window seats.",
        )
        project = formation.AutoMemoryProposalV2(
            "project_fact",
            (
                source_span(source, "The service runs on Render."),
                source_span(source, "Project Atlas uses Python."),
            ),
        )
        validated = formation.validate_auto_memory_proposals(
            (project, preference),
            source_length=len(source),
        )
        self.assertEqual(
            [item.signal_type for item in validated],
            ["durable_preference", "project_fact"],
        )
        self.assertEqual(
            [
                source[span.start:span.end]
                for span in validated[1].spans
            ],
            [
                "Project Atlas uses Python.",
                "The service runs on Render.",
            ],
        )

    def test_v2_keeps_v1_policy_veto_and_never_cherry_picks_forbidden_context(self):
        source = "Do not remember this. Project Atlas uses Python."
        item = proposal(source, "project_fact", "Project Atlas uses Python.")
        self.assert_category(
            "ineligible_proposal",
            formation.build_auto_memory_candidates_v2,
            1,
            source,
            (item,),
        )

    def test_empty_proposals_are_exact_empty_tuple(self):
        self.assertIs(
            formation.build_auto_memory_candidates_v2(1, "", ()),
            (),
        )
        self.assertIs(
            formation.validate_auto_memory_proposals((), source_length=0),
            (),
        )

    def test_span_container_and_range_contract_fail_closed(self):
        source = "Project Atlas uses Python."
        valid = source_span(source, source)
        self.assert_category(
            "invalid_spans",
            formation.validate_auto_memory_proposals,
            (formation.AutoMemoryProposalV2("project_fact", [valid]),),
            source_length=len(source),
        )
        self.assert_category(
            "empty_spans",
            formation.validate_auto_memory_proposals,
            (formation.AutoMemoryProposalV2("project_fact", ()),),
            source_length=len(source),
        )
        self.assert_category(
            "invalid_span",
            formation.validate_auto_memory_proposals,
            (
                formation.AutoMemoryProposalV2(
                    "project_fact",
                    (formation.AutoMemorySourceSpanV2(True, 2),),
                ),
            ),
            source_length=len(source),
        )
        self.assert_category(
            "invalid_span",
            formation.validate_auto_memory_proposals,
            (
                formation.AutoMemoryProposalV2(
                    "project_fact",
                    (formation.AutoMemorySourceSpanV2(0, len(source) + 1),),
                ),
            ),
            source_length=len(source),
        )

    def test_duplicate_and_overlapping_spans_are_rejected(self):
        source = "abcdefghij"
        duplicate = formation.AutoMemorySourceSpanV2(0, 2)
        self.assert_category(
            "duplicate_span",
            formation.validate_auto_memory_proposals,
            (
                formation.AutoMemoryProposalV2(
                    "stable_profile",
                    (duplicate, duplicate),
                ),
            ),
            source_length=len(source),
        )
        self.assert_category(
            "overlapping_spans",
            formation.validate_auto_memory_proposals,
            (
                formation.AutoMemoryProposalV2(
                    "stable_profile",
                    (
                        formation.AutoMemorySourceSpanV2(0, 4),
                        formation.AutoMemorySourceSpanV2(3, 5),
                    ),
                ),
            ),
            source_length=len(source),
        )
        self.assert_category(
            "overlapping_proposals",
            formation.validate_auto_memory_proposals,
            (
                formation.AutoMemoryProposalV2(
                    "stable_profile",
                    (formation.AutoMemorySourceSpanV2(0, 4),),
                ),
                formation.AutoMemoryProposalV2(
                    "project_fact",
                    (formation.AutoMemorySourceSpanV2(2, 6),),
                ),
            ),
            source_length=len(source),
        )

    def test_span_and_proposal_budgets_are_closed(self):
        source = "abcdefghijklmnop"
        too_many_local = tuple(
            formation.AutoMemorySourceSpanV2(index * 2, index * 2 + 1)
            for index in range(5)
        )
        self.assert_category(
            "too_many_spans",
            formation.validate_auto_memory_proposals,
            (
                formation.AutoMemoryProposalV2(
                    "stable_profile",
                    too_many_local,
                ),
            ),
            source_length=len(source),
        )

        nine_spans = [
            formation.AutoMemorySourceSpanV2(index, index + 1)
            for index in range(9)
        ]
        self.assert_category(
            "too_many_total_spans",
            formation.validate_auto_memory_proposals,
            (
                formation.AutoMemoryProposalV2("stable_profile", tuple(nine_spans[:3])),
                formation.AutoMemoryProposalV2("project_fact", tuple(nine_spans[3:6])),
                formation.AutoMemoryProposalV2("task_progress", tuple(nine_spans[6:9])),
            ),
            source_length=len(source),
        )
        four = tuple(
            formation.AutoMemoryProposalV2(
                "stable_profile",
                (formation.AutoMemorySourceSpanV2(index, index + 1),),
            )
            for index in range(4)
        )
        self.assert_category(
            "too_many_proposals",
            formation.validate_auto_memory_proposals,
            four,
            source_length=len(source),
        )

    def test_duplicate_proposals_are_rejected_after_span_canonicalization(self):
        source = "abcdefghij"
        left = formation.AutoMemoryProposalV2(
            "stable_profile",
            (
                formation.AutoMemorySourceSpanV2(0, 1),
                formation.AutoMemorySourceSpanV2(3, 4),
            ),
        )
        right = formation.AutoMemoryProposalV2(
            "stable_profile",
            (
                formation.AutoMemorySourceSpanV2(3, 4),
                formation.AutoMemorySourceSpanV2(0, 1),
            ),
        )
        self.assert_category(
            "duplicate_proposal",
            formation.validate_auto_memory_proposals,
            (left, right),
            source_length=len(source),
        )

    def test_contract_objects_are_frozen_slotted_and_data_free_in_repr(self):
        span = formation.AutoMemorySourceSpanV2(0, 1)
        item = formation.AutoMemoryProposalV2("stable_profile", (span,))
        self.assertFalse(hasattr(span, "__dict__"))
        self.assertFalse(hasattr(item, "__dict__"))
        self.assertEqual(repr(span), "<AutoMemorySourceSpanV2>")
        self.assertEqual(repr(item), "<AutoMemoryProposalV2>")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            span.start = 2
        with self.assertRaises(dataclasses.FrozenInstanceError):
            item.signal_type = "project_fact"

    def test_v1_contract_remains_unchanged_and_parallel(self):
        self.assertEqual(memory_formation.FORMATION_CONTRACT_VERSION, "memory-formation-v1")
        self.assertEqual(formation.FORMATION_CONTRACT_VERSION, "memory-formation-v2")
        self.assertEqual(
            [field.name for field in dataclasses.fields(memory_formation.AutoMemoryProposalV1)],
            ["signal_type", "start", "end"],
        )
        self.assertEqual(
            set(formation.SIGNAL_KIND_MAPPING),
            set(memory_formation.SIGNAL_KIND_MAPPING),
        )


if __name__ == "__main__":
    unittest.main()
