from __future__ import annotations

import ast
import dataclasses
import logging
import unittest
from pathlib import Path
from unittest import mock

from backend import memory_formation


POSITIVE_CASES = (
    ("durable_preference", "I usually prefer window seats.", "user_preference"),
    ("durable_preference", "我一直更喜欢喝无糖拿铁。", "user_preference"),
    ("stable_profile", "I work as a product designer.", "user_profile"),
    ("stable_profile", "我的专业是计算机科学。", "user_profile"),
    ("relationship_fact", "My wife is Alex.", "relationship"),
    ("relationship_fact", "我和小李是大学同学。", "relationship"),
    ("shared_episode", "We visited Kyoto together last year.", "shared_episode"),
    ("shared_episode", "我们去年一起去了杭州。", "shared_episode"),
    ("project_fact", "Project Atlas uses Python.", "project"),
    ("project_fact", "这个项目使用 FastAPI。", "project"),
    ("project_decision", "We decided to keep the API stateless.", "decision"),
    ("project_decision", "这个项目以后统一使用 PostgreSQL。", "decision"),
    ("task_progress", "The database migration is completed; next step is the frontend.", "task_or_progress"),
    ("task_progress", "数据库迁移已经完成，下一步做前端。", "task_or_progress"),
)

ELIGIBILITY_NEGATIVE_CASES = (
    ("durable_preference", "I currently prefer window seats."),
    ("durable_preference", "我现在有点喜欢靠窗座位。"),
    ("stable_profile", "If I were a doctor, I would work as a surgeon."),
    ("stable_profile", "假设我是医生，我是一名外科医生。"),
    ("shared_episode", "In this story, we visited Kyoto together last year."),
    ("project_fact", "这个虚构故事里的项目使用 Python。"),
    ("durable_preference", "Just kidding, I usually prefer aisle seats."),
    ("durable_preference", "开玩笑的，我一直更喜欢夜班。"),
    ("durable_preference", "My friend usually prefers sugar-free lattes."),
    ("durable_preference", "我朋友喜欢无糖拿铁。"),
    ("durable_preference", "Do not remember that I usually prefer coffee."),
    ("durable_preference", "不要记住我一直喜欢咖啡。"),
    ("durable_preference", "I feel sleepy right now, although I usually prefer mornings."),
    ("durable_preference", "我现在有点困，但我一直喜欢早起。"),
)

REVIEW_BLOCKING_NEGATIVE_CASES = (
    ("stable_profile", "I am a little tired."),
    ("stable_profile", "I am a bit hungry."),
    ("stable_profile", "Maybe I am a designer."),
    ("stable_profile", "Perhaps I am a product designer."),
    ("relationship_fact", "My wife and I are tired."),
    ("relationship_fact", "My husband and I are hungry."),
    ("relationship_fact", "My wife is Tired."),
    ("shared_episode", "I remember when we were hungry."),
    ("shared_episode", "I remember when we might go to Kyoto."),
    ("project_decision", "We decided to eat pizza."),
    ("project_decision", "We agreed to watch a movie."),
    ("project_decision", "以后统一用筷子。"),
    ("task_progress", "Dinner is done."),
    ("task_progress", "The movie is finished."),
    ("task_progress", "Laundry is done."),
    ("task_progress", "晚饭已经完成。"),
    ("project_fact", "My friend's project Atlas uses Python."),
    ("project_fact", "My coworker's project Atlas uses Python."),
    ("project_fact", "我朋友的项目使用 Python。"),
    ("project_fact", "我同事的项目采用 PostgreSQL。"),
    ("durable_preference", "别存，我一直喜欢咖啡。"),
    ("durable_preference", "不要存，我一直喜欢咖啡。"),
    ("durable_preference", "请勿保存，我一直喜欢咖啡。"),
    ("durable_preference", "Don't keep this in memory. I usually prefer tea."),
    ("durable_preference", "Do not keep this in memory. I usually prefer tea."),
)

UNCERTAINTY_NEGATIVE_CASES = (
    ("stable_profile", "Maybe I am a product designer."),
    ("stable_profile", "Perhaps I am a product designer."),
    ("stable_profile", "I might work as a product designer."),
    ("stable_profile", "I may work as a product designer."),
    ("stable_profile", "I possibly work as a product designer."),
    ("stable_profile", "可能我的专业是计算机科学。"),
    ("stable_profile", "也许我的专业是计算机科学。"),
    ("stable_profile", "或许我的专业是计算机科学。"),
)

TIGHTENED_GATE_POSITIVE_CASES = (
    ("stable_profile", "I am a product designer."),
    ("stable_profile", "我是产品设计师。"),
    ("relationship_fact", "My wife is Jordan."),
    ("relationship_fact", "My husband is named Morgan."),
    ("relationship_fact", "My partner is called Riley."),
    ("relationship_fact", "My wife and I are married."),
    ("relationship_fact", "我和小李是大学同学。"),
    ("shared_episode", "I remember when we visited Kyoto together."),
    ("shared_episode", "我记得我们一起去了杭州。"),
    ("project_decision", "We agreed to use PostgreSQL for the project database."),
    ("project_decision", "我们决定这个项目统一使用 PostgreSQL。"),
    ("task_progress", "The API migration is finished."),
    ("task_progress", "后端迁移已完成。"),
)

CLOSURE_REVIEW_NEGATIVE_CASES = (
    ("project_decision", "We decided to build a sandcastle."),
    ("project_decision", "We decided to release the bird."),
    ("project_decision", "We decided to watch the model in the fashion show."),
    ("project_decision", "We decided to eat pizza after the API meeting."),
    ("task_progress", "Dinner is done after the API meeting."),
    ("task_progress", "The movie is finished; next step is the frontend."),
    ("project_fact", "A friend's project Atlas uses Python."),
    ("project_fact", "Our client's project Atlas uses Python."),
    ("project_fact", "A coworker's project Atlas uses Python."),
    ("project_fact", "A teammate's project Atlas uses Python."),
    ("project_fact", "朋友的项目使用 Python。"),
    ("project_fact", "同事的项目采用 PostgreSQL。"),
    ("project_fact", "客户的项目使用 FastAPI。"),
    ("relationship_fact", "My wife is Tired."),
    ("relationship_fact", "My wife is Hungry."),
    ("relationship_fact", "My wife is Sick."),
    ("relationship_fact", "My wife is Happy."),
    ("relationship_fact", "My wife is Fine."),
    ("relationship_fact", "My wife is Okay."),
)

SOURCE_CONTEXT_VETO_CASES = (
    (
        "project_fact",
        "My friend's project Atlas uses Python.",
        "project Atlas uses Python.",
    ),
    (
        "project_fact",
        "My coworker's project Atlas uses Python.",
        "project Atlas uses Python.",
    ),
    (
        "durable_preference",
        "Do not remember that I usually prefer coffee.",
        "I usually prefer coffee.",
    ),
    (
        "durable_preference",
        "Don't keep this in memory. I usually prefer tea.",
        "I usually prefer tea.",
    ),
    (
        "durable_preference",
        "Just kidding, I usually prefer aisle seats.",
        "I usually prefer aisle seats.",
    ),
    (
        "stable_profile",
        "If I were a doctor, I work as a product designer.",
        "I work as a product designer.",
    ),
    (
        "durable_preference",
        "别存这件事，我一直喜欢咖啡。",
        "我一直喜欢咖啡。",
    ),
    (
        "durable_preference",
        "开玩笑的，我一直更喜欢夜班。",
        "我一直更喜欢夜班。",
    ),
    (
        "project_fact",
        "我朋友的项目使用 Python。",
        "项目使用 Python。",
    ),
)


def proposal_for(text: str, signal_type: str) -> memory_formation.AutoMemoryProposalV1:
    return memory_formation.AutoMemoryProposalV1(signal_type, 0, len(text))


def build_one(
    text: str,
    signal_type: str,
    *,
    source_message_id: int = 17,
    max_item_chars: int = 1000,
) -> memory_formation.AutoMemoryCandidateV1:
    result = memory_formation.build_auto_memory_candidates(
        source_message_id,
        text,
        (proposal_for(text, signal_type),),
        max_item_chars=max_item_chars,
    )
    if len(result) != 1:
        raise AssertionError("expected one candidate")
    return result[0]


class AutoMemoryFormationPositiveTests(unittest.TestCase):
    def test_chinese_and_english_positive_cases_cover_every_signal_class(self):
        languages_by_signal: dict[str, int] = {}
        for signal_type, text, expected_kind in POSITIVE_CASES:
            with self.subTest(signal_type=signal_type, text=text):
                candidate = build_one(text, signal_type)
                self.assertEqual(candidate.signal_type, signal_type)
                self.assertEqual(candidate.kind, expected_kind)
                self.assertEqual(candidate.normalized_content, text)
                languages_by_signal[signal_type] = languages_by_signal.get(signal_type, 0) + 1
        self.assertEqual(set(languages_by_signal), set(memory_formation.SIGNAL_KIND_MAPPING))
        self.assertEqual(set(languages_by_signal.values()), {2})

    def test_tightened_gates_keep_existing_and_near_neighbor_durable_claims(self):
        self.assertEqual(len(POSITIVE_CASES), 14)
        for signal_type, text, expected_kind in POSITIVE_CASES:
            with self.subTest(group="existing", signal_type=signal_type, text=text):
                self.assertEqual(build_one(text, signal_type).kind, expected_kind)
        for signal_type, text in TIGHTENED_GATE_POSITIVE_CASES:
            with self.subTest(group="near_neighbor", signal_type=signal_type, text=text):
                candidate = build_one(text, signal_type)
                self.assertEqual(
                    candidate.kind,
                    memory_formation.SIGNAL_KIND_MAPPING[signal_type],
                )
                self.assertEqual(candidate.normalized_content, text)

    def test_exact_signal_to_kind_mapping_and_fixed_server_fields(self):
        for signal_type, text, expected_kind in POSITIVE_CASES:
            with self.subTest(signal_type=signal_type, text=text):
                candidate = build_one(text, signal_type, source_message_id=90210)
                self.assertEqual(candidate.source_message_id, 90210)
                self.assertEqual(candidate.kind, expected_kind)
                self.assertEqual(candidate.scope_type, "global_user")
                self.assertEqual(candidate.scope_ref, "")
                self.assertEqual(candidate.sensitivity, "normal")
        self.assertEqual(
            memory_formation.SIGNAL_KIND_MAPPING,
            {
                "durable_preference": "user_preference",
                "stable_profile": "user_profile",
                "relationship_fact": "relationship",
                "shared_episode": "shared_episode",
                "project_fact": "project",
                "project_decision": "decision",
                "task_progress": "task_or_progress",
            },
        )
        with self.assertRaises(TypeError):
            memory_formation.SIGNAL_KIND_MAPPING["arbitrary"] = "assistant_experience"

    def test_empty_proposal_list_and_tuple_return_the_exact_empty_tuple(self):
        self.assertIs(
            memory_formation.build_auto_memory_candidates(1, "", []),
            (),
        )
        self.assertIs(
            memory_formation.build_auto_memory_candidates(1, "safe", ()),
            (),
        )

    def test_result_is_deterministic_and_frozen_slotted(self):
        text = "I usually prefer window seats."
        proposal = proposal_for(text, "durable_preference")
        left = memory_formation.build_auto_memory_candidates(8, text, [proposal])
        right = memory_formation.build_auto_memory_candidates(8, text, (proposal,))
        self.assertEqual(left, right)
        self.assertIsInstance(left, tuple)
        self.assertFalse(hasattr(proposal, "__dict__"))
        self.assertFalse(hasattr(left[0], "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            proposal.start = 1
        with self.assertRaises(dataclasses.FrozenInstanceError):
            left[0].kind = "assistant_experience"


class AutoMemoryFormationBoundaryTests(unittest.TestCase):
    def assert_category(self, expected: str, callable_, *args, **kwargs):
        with self.assertRaises(memory_formation.MemoryFormationError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.category, expected)
        self.assertEqual(str(raised.exception), expected)

    def test_source_message_id_requires_an_exact_positive_int(self):
        text = "I usually prefer window seats."

        class IntSubclass(int):
            pass

        for invalid in (True, False, 0, -1, 1.0, "1", IntSubclass(1), None):
            with self.subTest(value_type=type(invalid).__name__):
                self.assert_category(
                    "invalid_source_message_id",
                    memory_formation.build_auto_memory_candidates,
                    invalid,
                    text,
                    (),
                )
        self.assertEqual(build_one(text, "durable_preference", source_message_id=1).source_message_id, 1)

    def test_source_text_requires_exact_valid_unicode_with_fixed_budget(self):
        class StrSubclass(str):
            pass

        for invalid in (None, b"text", 7, StrSubclass("text"), "bad\ud800text", "bad\udffftext"):
            with self.subTest(value_type=type(invalid).__name__):
                self.assert_category(
                    "invalid_source_text",
                    memory_formation.build_auto_memory_candidates,
                    1,
                    invalid,
                    (),
                )
        self.assertEqual(
            memory_formation.build_auto_memory_candidates(1, "x" * 8000, ()),
            (),
        )
        self.assert_category(
            "source_text_too_long",
            memory_formation.build_auto_memory_candidates,
            1,
            "x" * 8001,
            (),
        )

    def test_max_item_chars_uses_exact_memory_policy_bounds(self):
        text = "I usually prefer tea."
        proposal = proposal_for(text, "durable_preference")
        for invalid in (True, False, 63, 4097, 1000.0, "1000", None):
            with self.subTest(value=invalid):
                self.assert_category(
                    "invalid_max_item_chars",
                    memory_formation.build_auto_memory_candidates,
                    1,
                    text,
                    (proposal,),
                    max_item_chars=invalid,
                )
        self.assertEqual(
            memory_formation.build_auto_memory_candidates(
                1, text, (proposal,), max_item_chars=64
            )[0].normalized_content,
            text,
        )
        self.assertEqual(
            memory_formation.build_auto_memory_candidates(
                1, text, (proposal,), max_item_chars=4096
            )[0].normalized_content,
            text,
        )

    def test_proposals_require_exact_list_or_tuple_and_exact_proposal_instances(self):
        text = "I usually prefer tea."
        proposal = proposal_for(text, "durable_preference")

        class ListSubclass(list):
            pass

        class ProposalSubclass(memory_formation.AutoMemoryProposalV1):
            pass

        invalid_containers = (
            {"signal_type": "durable_preference", "start": 0, "end": len(text)},
            "not-a-proposal-sequence",
            (item for item in (proposal,)),
            ListSubclass([proposal]),
            None,
        )
        for invalid in invalid_containers:
            with self.subTest(value_type=type(invalid).__name__):
                self.assert_category(
                    "invalid_proposals",
                    memory_formation.build_auto_memory_candidates,
                    1,
                    text,
                    invalid,
                )
        invalid_items = (
            {"signal_type": "durable_preference", "start": 0, "end": len(text)},
            {"signal_type": "durable_preference", "start": 0, "end": len(text), "content": text},
            ProposalSubclass("durable_preference", 0, len(text)),
            object(),
        )
        for invalid in invalid_items:
            with self.subTest(value_type=type(invalid).__name__):
                self.assert_category(
                    "invalid_proposal",
                    memory_formation.build_auto_memory_candidates,
                    1,
                    text,
                    (invalid,),
                )

    def test_signal_type_is_closed_exact_text_and_assistant_experience_is_impossible(self):
        text = "I usually prefer tea."

        class StrSubclass(str):
            pass

        for signal_type in (
            "assistant_experience",
            "user_preference",
            "arbitrary",
            "",
            StrSubclass("durable_preference"),
            7,
            None,
        ):
            with self.subTest(signal_type=signal_type):
                self.assert_category(
                    "invalid_signal_type",
                    memory_formation.build_auto_memory_candidates,
                    1,
                    text,
                    (memory_formation.AutoMemoryProposalV1(signal_type, 0, len(text)),),
                )
        with self.assertRaises(TypeError):
            memory_formation.AutoMemoryProposalV1(
                signal_type="durable_preference",
                start=0,
                end=len(text),
                kind="assistant_experience",
            )

    def test_indices_require_exact_int_and_valid_half_open_bounds(self):
        text = "I usually prefer tea."
        cases = (
            (True, len(text)),
            (0, False),
            (0.0, len(text)),
            (0, float(len(text))),
            (-1, len(text)),
            (0, -1),
            (len(text), 0),
            (2, 2),
            (3, 2),
            (0, len(text) + 1),
        )
        for start, end in cases:
            with self.subTest(start=start, end=end):
                self.assert_category(
                    "invalid_span",
                    memory_formation.build_auto_memory_candidates,
                    1,
                    text,
                    (memory_formation.AutoMemoryProposalV1("durable_preference", start, end),),
                )

    def test_unicode_emoji_indices_use_python_character_positions(self):
        source = "😀 I usually prefer window seats. ✅"
        expected = "I usually prefer window seats."
        start = source.index("I")
        end = start + len(expected)
        candidate = memory_formation.build_auto_memory_candidates(
            1,
            source,
            (memory_formation.AutoMemoryProposalV1("durable_preference", start, end),),
        )[0]
        self.assertEqual(start, 2)
        self.assertEqual(candidate.normalized_content, expected)
        self.assertNotIn("😀", candidate.normalized_content)
        self.assertNotIn("✅", candidate.normalized_content)

    def test_duplicate_overlapping_and_excess_proposals_fail_closed(self):
        text = "I usually prefer tea. I work as a designer."
        first_text = "I usually prefer tea."
        first = memory_formation.AutoMemoryProposalV1(
            "durable_preference", 0, len(first_text)
        )
        duplicate = memory_formation.AutoMemoryProposalV1(
            "durable_preference", 0, len(first_text)
        )
        overlap = memory_formation.AutoMemoryProposalV1(
            "stable_profile", len(first_text) - 2, len(text)
        )
        self.assert_category(
            "duplicate_proposal",
            memory_formation.build_auto_memory_candidates,
            1,
            text,
            (first, duplicate),
        )
        self.assert_category(
            "overlapping_proposals",
            memory_formation.build_auto_memory_candidates,
            1,
            text,
            (overlap, first),
        )
        self.assert_category(
            "too_many_proposals",
            memory_formation.build_auto_memory_candidates,
            1,
            text,
            (first, first, first, first),
        )


class AutoMemoryFormationProvenanceTests(unittest.TestCase):
    def test_output_order_depends_only_on_source_span(self):
        first = "I usually prefer window seats."
        second = "I work as a product designer."
        source = first + " " + second
        first_proposal = memory_formation.AutoMemoryProposalV1(
            "durable_preference", 0, len(first)
        )
        second_start = len(first) + 1
        second_proposal = memory_formation.AutoMemoryProposalV1(
            "stable_profile", second_start, len(source)
        )
        forward = memory_formation.build_auto_memory_candidates(
            3, source, (first_proposal, second_proposal)
        )
        reverse = memory_formation.build_auto_memory_candidates(
            3, source, (second_proposal, first_proposal)
        )
        self.assertEqual(forward, reverse)
        self.assertEqual(
            tuple(item.normalized_content for item in reverse),
            (first, second),
        )

    def test_exact_extracted_span_is_the_only_plaintext_source(self):
        prefix = "UNSELECTED_PREFIX "
        selected = "  I usually prefer\r\n  window seats.  "
        suffix = " UNSELECTED_SUFFIX"
        source = prefix + selected + suffix
        start = len(prefix)
        end = start + len(selected)
        candidate = memory_formation.build_auto_memory_candidates(
            12,
            source,
            (memory_formation.AutoMemoryProposalV1("durable_preference", start, end),),
        )[0]
        self.assertEqual(candidate.normalized_content, "I usually prefer window seats.")
        self.assertNotIn("UNSELECTED_PREFIX", candidate.normalized_content)
        self.assertNotIn("UNSELECTED_SUFFIX", candidate.normalized_content)

    def test_benign_full_source_context_stays_outside_plaintext_and_memory_policy(self):
        source = "顺便说一句，我一直更喜欢无糖拿铁。谢谢。"
        selected = "我一直更喜欢无糖拿铁。"
        start = source.index(selected)
        proposal = memory_formation.AutoMemoryProposalV1(
            "durable_preference", start, start + len(selected)
        )
        policy_inputs: list[str] = []
        original_validate_content = memory_formation.MemoryPolicy.validate_content

        def tracked_validate_content(policy, content, sensitivity, **kwargs):
            policy_inputs.append(content)
            return original_validate_content(policy, content, sensitivity, **kwargs)

        with mock.patch.object(
            memory_formation.MemoryPolicy,
            "validate_content",
            new=tracked_validate_content,
        ):
            candidate = memory_formation.build_auto_memory_candidates(
                14, source, (proposal,)
            )[0]
        self.assertEqual(policy_inputs, [selected])
        self.assertEqual(candidate.normalized_content, selected)
        self.assertNotIn("顺便说一句", candidate.normalized_content)
        self.assertNotIn("谢谢", candidate.normalized_content)

    def test_candidate_content_cannot_be_supplied_or_forged_through_proposals(self):
        text = "I usually prefer tea."
        with self.assertRaises(TypeError):
            memory_formation.AutoMemoryProposalV1(
                signal_type="durable_preference",
                start=0,
                end=len(text),
                normalized_content="FORGED_CANDIDATE_SENTINEL",
            )
        proposal = proposal_for(text, "durable_preference")
        with self.assertRaises((AttributeError, TypeError)):
            proposal.normalized_content = "FORGED_CANDIDATE_SENTINEL"
        forged_candidate = memory_formation.AutoMemoryCandidateV1(
            1,
            "durable_preference",
            "user_preference",
            "global_user",
            "",
            "FORGED_CANDIDATE_SENTINEL",
            "normal",
        )
        with self.assertRaisesRegex(memory_formation.MemoryFormationError, "invalid_proposal"):
            memory_formation.build_auto_memory_candidates(1, text, (forged_candidate,))
        candidate = build_one(text, "durable_preference")
        self.assertEqual(candidate.normalized_content, text)
        self.assertNotEqual(candidate.normalized_content, "FORGED_CANDIDATE_SENTINEL")

    def test_one_invalid_proposal_prevents_any_partial_result(self):
        safe = "I usually prefer tea."
        unsafe = "I usually prefer coffee; api_key=synthetic-secret-value-12345"
        source = safe + " " + unsafe
        proposals = (
            memory_formation.AutoMemoryProposalV1("durable_preference", 0, len(safe)),
            memory_formation.AutoMemoryProposalV1(
                "durable_preference", len(safe) + 1, len(source)
            ),
        )
        with self.assertRaisesRegex(
            memory_formation.MemoryFormationError, "candidate_policy_rejected"
        ):
            memory_formation.build_auto_memory_candidates(1, source, proposals)


class AutoMemoryFormationNegativeTests(unittest.TestCase):
    def test_roleplay_fiction_joke_third_party_forget_and_temporary_content_are_ineligible(self):
        for signal_type, text in ELIGIBILITY_NEGATIVE_CASES:
            with self.subTest(signal_type=signal_type, text=text):
                with self.assertRaises(memory_formation.MemoryFormationError) as raised:
                    build_one(text, signal_type)
                self.assertEqual(raised.exception.category, "ineligible_proposal")

    def test_external_review_blocking_false_positives_are_permanently_rejected(self):
        self.assertEqual(len(REVIEW_BLOCKING_NEGATIVE_CASES), 25)
        for signal_type, text in REVIEW_BLOCKING_NEGATIVE_CASES:
            with self.subTest(signal_type=signal_type, text=text):
                with self.assertRaises(memory_formation.MemoryFormationError) as raised:
                    build_one(text, signal_type)
                self.assertEqual(raised.exception.category, "ineligible_proposal")

    def test_closure_review_claim_binding_ownership_and_state_cases_are_rejected(self):
        self.assertEqual(len(CLOSURE_REVIEW_NEGATIVE_CASES), 19)
        for signal_type, text in CLOSURE_REVIEW_NEGATIVE_CASES:
            with self.subTest(signal_type=signal_type, text=text):
                with self.assertRaises(memory_formation.MemoryFormationError) as raised:
                    build_one(text, signal_type)
                self.assertEqual(raised.exception.category, "ineligible_proposal")

    def test_full_source_context_veto_cannot_be_escaped_by_narrowing_the_span(self):
        self.assertEqual(len(SOURCE_CONTEXT_VETO_CASES), 9)
        for signal_type, source, selected in SOURCE_CONTEXT_VETO_CASES:
            with self.subTest(signal_type=signal_type, source=source):
                start = source.index(selected)
                proposal = memory_formation.AutoMemoryProposalV1(
                    signal_type, start, start + len(selected)
                )
                with self.assertRaises(memory_formation.MemoryFormationError) as raised:
                    memory_formation.build_auto_memory_candidates(
                        15, source, (proposal,)
                    )
                self.assertEqual(raised.exception.category, "ineligible_proposal")

    def test_uncertainty_markers_fail_closed_across_languages(self):
        for signal_type, text in UNCERTAINTY_NEGATIVE_CASES:
            with self.subTest(signal_type=signal_type, text=text):
                with self.assertRaises(memory_formation.MemoryFormationError) as raised:
                    build_one(text, signal_type)
                self.assertEqual(raised.exception.category, "ineligible_proposal")

    def test_third_party_boundary_applies_before_every_signal_gate(self):
        text = "A teammate's project Atlas uses Python."
        for signal_type in memory_formation.SIGNAL_KIND_MAPPING:
            with self.subTest(signal_type=signal_type):
                with self.assertRaises(memory_formation.MemoryFormationError) as raised:
                    build_one(text, signal_type)
                self.assertEqual(raised.exception.category, "ineligible_proposal")

    def test_credentials_and_secrets_are_rejected_by_memory_policy(self):
        cases = (
            "I usually prefer tea; api_key=synthetic-secret-value-12345",
            "I usually prefer tea; Authorization: Bearer synthetic-token-value-12345",
            "我一直喜欢茶，secret_key=synthetic-secret-value-12345",
        )
        for text in cases:
            with self.subTest(text=text), self.assertRaises(
                memory_formation.MemoryFormationError
            ) as raised:
                build_one(text, "durable_preference")
            self.assertEqual(raised.exception.category, "candidate_policy_rejected")

    def test_high_sensitivity_content_cannot_be_formed_as_normal(self):
        text = "I work as a product designer and was diagnosed with diabetes."
        with self.assertRaises(memory_formation.MemoryFormationError) as raised:
            build_one(text, "stable_profile")
        self.assertEqual(raised.exception.category, "candidate_policy_rejected")

    def test_technical_identifiers_test_markers_and_logs_are_policy_rejected(self):
        cases = (
            ("project_fact", "This project uses Python; database id: synthetic-database-77"),
            ("project_fact", "This project uses OPERIT-TEXT-E2E-OK."),
            ("task_progress", "The migration is completed. Traceback (most recent call last)"),
        )
        for signal_type, text in cases:
            with self.subTest(signal_type=signal_type), self.assertRaises(
                memory_formation.MemoryFormationError
            ) as raised:
                build_one(text, signal_type)
            self.assertEqual(raised.exception.category, "candidate_policy_rejected")

    def test_weak_or_transient_text_does_not_pass_a_durability_gate(self):
        cases = (
            ("durable_preference", "I like tea."),
            ("stable_profile", "I am tired."),
            ("relationship_fact", "Alex likes tea."),
            ("shared_episode", "We might visit Kyoto."),
            ("project_fact", "Maybe a project could use Python."),
            ("project_decision", "We are discussing whether to use PostgreSQL."),
            ("task_progress", "The migration may finish later."),
        )
        for signal_type, text in cases:
            with self.subTest(signal_type=signal_type), self.assertRaises(
                memory_formation.MemoryFormationError
            ) as raised:
                build_one(text, signal_type)
            self.assertEqual(raised.exception.category, "ineligible_proposal")


class AutoMemoryFormationBudgetAndSafetyTests(unittest.TestCase):
    def test_per_item_budget_rejects_without_silent_truncation(self):
        prefix = "I usually prefer tea because "
        exact = prefix + ("x" * (64 - len(prefix)))
        self.assertEqual(len(exact), 64)
        candidate = build_one(exact, "durable_preference", max_item_chars=64)
        self.assertEqual(candidate.normalized_content, exact)
        oversized = exact + "x"
        with self.assertRaises(memory_formation.MemoryFormationError) as raised:
            build_one(oversized, "durable_preference", max_item_chars=64)
        self.assertEqual(raised.exception.category, "candidate_policy_rejected")

    def test_aggregate_normalized_character_budget_rejects_all_candidates(self):
        item = "I usually prefer " + ("x" * 684)
        self.assertEqual(len(item), 701)
        separator = " | "
        source = separator.join((item, item, item))
        starts = (0, len(item) + len(separator), 2 * (len(item) + len(separator)))
        proposals = tuple(
            memory_formation.AutoMemoryProposalV1(
                "durable_preference", start, start + len(item)
            )
            for start in reversed(starts)
        )
        with self.assertRaises(memory_formation.MemoryFormationError) as raised:
            memory_formation.build_auto_memory_candidates(1, source, proposals)
        self.assertEqual(raised.exception.category, "candidate_budget_exceeded")

    def test_representations_and_errors_do_not_leak_sentinels_ids_or_addresses(self):
        source_sentinel = "SOURCE_SENTINEL_I usually prefer tea."
        candidate_sentinel = "CANDIDATE_SENTINEL"
        secret_sentinel = "SECRET_SENTINEL"
        proposal = memory_formation.AutoMemoryProposalV1(secret_sentinel, 7, 99)
        candidate = memory_formation.AutoMemoryCandidateV1(
            777777,
            secret_sentinel,
            secret_sentinel,
            secret_sentinel,
            secret_sentinel,
            candidate_sentinel,
            secret_sentinel,
        )
        self.assertEqual(repr(proposal), "<AutoMemoryProposalV1>")
        self.assertEqual(repr(candidate), "<AutoMemoryCandidateV1>")
        for sentinel in (source_sentinel, candidate_sentinel, secret_sentinel, "777777", "99"):
            self.assertNotIn(sentinel, repr(proposal))
            self.assertNotIn(sentinel, repr(candidate))

        error = memory_formation.MemoryFormationError(secret_sentinel)
        self.assertEqual(error.category, "memory_formation_error")
        for rendered in (str(error), repr(error), repr(error.args), repr(error.__dict__) if hasattr(error, "__dict__") else ""):
            self.assertNotIn(secret_sentinel, rendered)
            self.assertNotIn(source_sentinel, rendered)
            self.assertNotRegex(rendered, r"0x[0-9a-fA-F]+")
        error.category = [secret_sentinel]
        self.assertEqual(str(error), "memory_formation_error")
        self.assertEqual(repr(error), "MemoryFormationError('memory_formation_error')")

        text = "I usually prefer tea; api_key=synthetic-secret-value-12345"
        with self.assertRaises(memory_formation.MemoryFormationError) as raised:
            build_one(text, "durable_preference", source_message_id=987654321)
        rendered = repr(raised.exception)
        for forbidden in (text, "synthetic-secret-value-12345", "987654321", str(len(text))):
            self.assertNotIn(forbidden, rendered)
        self.assertNotRegex(rendered, r"0x[0-9a-fA-F]+")

    def test_candidate_formation_emits_no_log_record_on_rejection(self):
        records: list[logging.LogRecord] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = Capture()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            text = "I usually prefer tea; api_key=synthetic-secret-value-12345"
            with self.assertRaises(memory_formation.MemoryFormationError):
                build_one(text, "durable_preference", source_message_id=999999)
        finally:
            root.removeHandler(handler)
        self.assertEqual(records, [])

    def test_module_has_only_pure_contract_dependencies(self):
        module_path = Path(memory_formation.__file__)
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imports.add(node.module)
        self.assertEqual(
            imports,
            {
                "__future__",
                "re",
                "unicodedata",
                "dataclasses",
                "types",
                "typing",
                "backend.memory_policy",
            },
        )
        source = module_path.read_text(encoding="utf-8")
        for test_person_name in ("Alex", "Jordan", "Morgan", "Riley"):
            with self.subTest(test_person_name=test_person_name):
                self.assertNotIn(test_person_name, source)
        for forbidden in (
            "sqlite3",
            "socket",
            "requests",
            "httpx",
            "FastAPI",
            "backend.app",
            "memory_store",
            "channel_store",
            "open(",
            "write_text",
            "write_bytes",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_public_shape_constants_and_repr_false_content_field_are_exact(self):
        self.assertEqual(memory_formation.MAX_PROPOSALS, 3)
        self.assertEqual(memory_formation.SOURCE_MAX_CHARS, 8000)
        self.assertEqual(memory_formation.DEFAULT_MAX_ITEM_CHARS, 1000)
        self.assertEqual(memory_formation.TOTAL_CANDIDATE_MAX_CHARS, 2000)
        self.assertTrue(dataclasses.is_dataclass(memory_formation.AutoMemoryProposalV1))
        self.assertTrue(dataclasses.is_dataclass(memory_formation.AutoMemoryCandidateV1))
        proposal_fields = tuple(field.name for field in dataclasses.fields(memory_formation.AutoMemoryProposalV1))
        candidate_fields = tuple(field.name for field in dataclasses.fields(memory_formation.AutoMemoryCandidateV1))
        self.assertEqual(proposal_fields, ("signal_type", "start", "end"))
        self.assertEqual(
            candidate_fields,
            (
                "source_message_id",
                "signal_type",
                "kind",
                "scope_type",
                "scope_ref",
                "normalized_content",
                "sensitivity",
            ),
        )
        candidate_field_map = {
            field.name: field for field in dataclasses.fields(memory_formation.AutoMemoryCandidateV1)
        }
        self.assertFalse(candidate_field_map["normalized_content"].repr)


if __name__ == "__main__":
    unittest.main()
