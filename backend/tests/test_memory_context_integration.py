from __future__ import annotations

import asyncio
import ast
import contextlib
import dataclasses
import io
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import httpx

from backend import kelivo_service, memory_context_integration
from backend.tests._support import NoNetworkMixin, load_app, request


def safe_item(
    content: str,
    *,
    kind: str = "user_preference",
    status: str = "active",
    sensitivity: str = "normal",
    marker: str = "A",
) -> dict:
    return {
        "memory_key": marker * 32,
        "kind": kind,
        "scope_type": "global_user",
        "scope_ref": "",
        "normalized_content": content,
        "fingerprint_version": 1,
        "status": status,
        "explicitness": "explicit",
        "confidence": 1.0,
        "sensitivity": sensitivity,
        "first_observed_at": "2026-01-01T00:00:00Z",
        "last_confirmed_at": "2026-01-02T00:00:00Z",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "provenance": [],
    }


class FakeReadService:
    def __init__(self, items=None, error: Exception | None = None):
        self.items = [] if items is None else items
        self.error = error
        self.calls = []

    def get_active_memories(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.items


class MemoryContextIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.base = (
            {"role": "system", "content": "persona"},
            {"role": "assistant", "content": "prior"},
            {"role": "user", "content": "current"},
        )

    def test_disabled_is_exact_noop_and_never_reads(self):
        service = FakeReadService(error=AssertionError("must not read"))
        with (
            mock.patch.object(
                memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
            ) as selector,
            mock.patch.object(
                memory_context_integration.memory_context,
                "render_memory_developer_message",
            ) as renderer,
        ):
            result = memory_context_integration.prepare_transient_memory_dispatch(
                service, self.base, enabled=False, smart_retrieval_enabled=False
            )
        self.assertIs(result.provider_messages, self.base)
        self.assertFalse(result.memory_applied)
        self.assertEqual(service.calls, [])
        selector.assert_not_called()
        renderer.assert_not_called()

    def test_flags_must_be_exact_bool_and_invalid_combination_never_reads(self):
        service = FakeReadService(error=AssertionError("must not read"))
        for enabled, smart in ((0, False), (True, 1), ("true", False)):
            with self.subTest(enabled=enabled, smart=smart), self.assertRaisesRegex(
                memory_context_integration.MemoryContextIntegrationError,
                r"^memory_context_unavailable$",
            ):
                memory_context_integration.prepare_transient_memory_dispatch(
                    service,
                    self.base,
                    enabled=enabled,
                    smart_retrieval_enabled=smart,
                )

        with (
            mock.patch.object(
                memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
            ) as selector,
            mock.patch.object(
                memory_context_integration.memory_context,
                "render_memory_developer_message",
            ) as renderer,
            self.assertRaisesRegex(
                memory_context_integration.MemoryContextIntegrationError,
                r"^memory_context_unavailable$",
            ),
        ):
            memory_context_integration.prepare_transient_memory_dispatch(
                service,
                self.base,
                enabled=False,
                smart_retrieval_enabled=True,
            )
        self.assertEqual(service.calls, [])
        selector.assert_not_called()
        renderer.assert_not_called()

    def test_empty_memory_preserves_original_tuple_and_dicts(self):
        service = FakeReadService([])
        with mock.patch.object(
            memory_context_integration.memory_retrieval,
            "select_relevant_memory_items",
        ) as selector:
            result = memory_context_integration.prepare_transient_memory_dispatch(
                service, self.base, enabled=True, smart_retrieval_enabled=False
            )
        self.assertIs(result.provider_messages, self.base)
        self.assertFalse(result.memory_applied)
        selector.assert_not_called()
        self.assertEqual(service.calls, [{
            "scope_type": "global_user",
            "scope_ref": "",
            "limit": 10,
            "character_budget": 2000,
            "include_sensitive": False,
        }])

    def test_active_normal_memory_is_inserted_before_final_user(self):
        plaintext = "User prefers blue."
        service = FakeReadService([safe_item(plaintext)])
        original = tuple(dict(message) for message in self.base)
        result = memory_context_integration.prepare_transient_memory_dispatch(
            service, self.base, enabled=True, smart_retrieval_enabled=False
        )
        self.assertTrue(result.memory_applied)
        self.assertEqual(len(result.provider_messages), len(self.base) + 1)
        self.assertEqual(result.provider_messages[-1], self.base[-1])
        self.assertEqual(result.provider_messages[-2]["role"], "developer")
        self.assertIn(plaintext, result.provider_messages[-2]["content"])
        self.assertEqual(self.base, original)
        self.assertTrue(all(
            result.provider_messages[index] is self.base[index]
            for index in range(len(self.base) - 1)
        ))
        self.assertIs(result.provider_messages[-1], self.base[-1])
        self.assertNotIn(plaintext, repr(result))

    def test_legacy_path_is_phase2_renderer_output_and_never_selects(self):
        items = [
            safe_item("first legacy memory", marker="A"),
            safe_item("second legacy memory", marker="B"),
        ]
        expected_message = (
            memory_context_integration.memory_context
            .render_memory_developer_message(
                items,
                scope_type="global_user",
                max_items=10,
                character_budget=2000,
            )
        )
        with mock.patch.object(
            memory_context_integration.memory_retrieval,
            "select_relevant_memory_items",
        ) as selector:
            result = memory_context_integration.prepare_transient_memory_dispatch(
                FakeReadService(items),
                self.base,
                enabled=True,
                smart_retrieval_enabled=False,
            )
        selector.assert_not_called()
        self.assertEqual(
            result.provider_messages,
            (*self.base[:-1], expected_message, self.base[-1]),
        )

    def test_renderer_revalidates_order_and_budgets(self):
        items = [safe_item(str(index) * 10, marker=chr(65 + index)) for index in range(11)]
        result = memory_context_integration.prepare_transient_memory_dispatch(
            FakeReadService(items), self.base, enabled=True,
            smart_retrieval_enabled=False,
        )
        decoded = json.loads(result.provider_messages[-2]["content"])
        self.assertEqual(decoded["memory_context"]["item_count"], 10)
        self.assertEqual(
            [item["normalized_content"] for item in decoded["memory_context"]["items"]],
            [item["normalized_content"] for item in items[:10]],
        )

    def test_sensitive_and_non_active_items_fail_data_free(self):
        cases = (
            safe_item("SENSITIVE-PLAINTEXT", sensitivity="sensitive"),
            safe_item("FORGOTTEN-PLAINTEXT", status="forgotten"),
            safe_item("SUPERSEDED-PLAINTEXT", status="superseded"),
            {"normalized_content": "FAKE-PLAINTEXT"},
        )
        for item in cases:
            plaintext = item["normalized_content"]
            with self.subTest(plaintext=plaintext):
                try:
                    memory_context_integration.prepare_transient_memory_dispatch(
                        FakeReadService([item]), self.base, enabled=True,
                        smart_retrieval_enabled=False,
                    )
                except memory_context_integration.MemoryContextIntegrationError as error:
                    self.assertEqual(error.category, "memory_context_unavailable")
                    self.assertNotIn(plaintext, str(error))
                    self.assertNotIn(plaintext, repr(error))
                else:
                    self.fail("unsafe item was accepted")

    def test_read_and_render_failures_are_fixed_and_data_free(self):
        plaintext = "READ-FAILURE-PLAINTEXT"
        service = FakeReadService(error=RuntimeError(plaintext))
        with self.assertRaisesRegex(
            memory_context_integration.MemoryContextIntegrationError,
            r"^memory_context_unavailable$",
        ) as raised:
            memory_context_integration.prepare_transient_memory_dispatch(
                service, self.base, enabled=True, smart_retrieval_enabled=False
            )
        self.assertNotIn(plaintext, repr(raised.exception))

        with mock.patch.object(
            memory_context_integration.memory_context,
            "render_memory_developer_message",
            side_effect=RuntimeError(plaintext),
        ):
            with self.assertRaisesRegex(
                memory_context_integration.MemoryContextIntegrationError,
                r"^memory_context_unavailable$",
            ):
                memory_context_integration.prepare_transient_memory_dispatch(
                    FakeReadService([safe_item("valid")]), self.base, enabled=True,
                    smart_retrieval_enabled=False,
                )

    def test_smart_path_uses_only_final_user_and_fixed_two_stage_budgets(self):
        base = (
            {"role": "user", "content": "orchid history"},
            {"role": "assistant", "content": "older blue discussion"},
            {"role": "user", "content": "blue tea today"},
        )
        relevant = safe_item("The user prefers blue tea", marker="A")
        unrelated = safe_item("orchid history", marker="B")
        service = FakeReadService([unrelated, relevant])
        selections = []
        rendered_items = []
        real_selector = (
            memory_context_integration.memory_retrieval.select_relevant_memory_items
        )
        real_renderer = (
            memory_context_integration.memory_context.render_memory_developer_message
        )

        def select(*args, **kwargs):
            selection = real_selector(*args, **kwargs)
            selections.append(selection)
            return selection

        def render(items, **kwargs):
            rendered_items.append(items)
            return real_renderer(items, **kwargs)

        with (
            mock.patch.object(
                memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
                side_effect=select,
            ) as selector,
            mock.patch.object(
                memory_context_integration.memory_context,
                "render_memory_developer_message",
                side_effect=render,
            ) as renderer,
        ):
            result = memory_context_integration.prepare_transient_memory_dispatch(
                service,
                base,
                enabled=True,
                smart_retrieval_enabled=True,
            )

        self.assertEqual(service.calls, [{
            "scope_type": "global_user",
            "scope_ref": "",
            "limit": 20,
            "character_budget": 8000,
            "include_sensitive": False,
        }])
        self.assertEqual(selector.call_count, 1)
        selector_input = selector.call_args.args[0]
        self.assertIs(type(selector_input), tuple)
        self.assertEqual(selector_input, tuple(service.items))
        self.assertTrue(all(
            selector_item is not original
            for selector_item, original in zip(selector_input, service.items)
        ))
        self.assertEqual(service.items, [unrelated, relevant])
        self.assertEqual(selector.call_args.kwargs, {
            "query_text": "blue tea today",
            "scope_type": "global_user",
            "max_items": 10,
            "character_budget": 2000,
        })
        self.assertEqual(renderer.call_count, 1)
        self.assertIs(rendered_items[0], selections[0].items)
        self.assertEqual(
            [item["normalized_content"] for item in selections[0].items],
            [relevant["normalized_content"]],
        )
        self.assertTrue(result.memory_applied)
        self.assertEqual(result.provider_messages[-1], base[-1])
        rendered = result.provider_messages[-2]["content"]
        self.assertIn(relevant["normalized_content"], rendered)
        self.assertNotIn(unrelated["normalized_content"], rendered)

    def test_smart_selector_mutation_cannot_invent_candidate_plaintext(self):
        original_text = "original memory"
        invented_text = "INVENTED-BY-SELECTOR"
        original_item = safe_item(original_text)
        service = FakeReadService([original_item])

        def mutate_and_forge(items, **_kwargs):
            items[0]["normalized_content"] = invented_text
            return (
                memory_context_integration.memory_retrieval
                .MemoryRetrievalSelectionV1(
                    items=(dict(items[0]),),
                    candidate_count=1,
                    selected_count=1,
                    query_signal_count=1,
                )
            )

        with (
            mock.patch.object(
                memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
                side_effect=mutate_and_forge,
            ),
            mock.patch.object(
                memory_context_integration.memory_context,
                "render_memory_developer_message",
            ) as renderer,
            self.assertRaisesRegex(
                memory_context_integration.MemoryContextIntegrationError,
                r"^memory_context_unavailable$",
            ) as raised,
        ):
            memory_context_integration.prepare_transient_memory_dispatch(
                service,
                self.base,
                enabled=True,
                smart_retrieval_enabled=True,
            )

        renderer.assert_not_called()
        self.assertEqual(original_item["normalized_content"], original_text)
        self.assertEqual(service.items, [original_item])
        for private in (original_text, invented_text, original_item["memory_key"]):
            self.assertNotIn(private, str(raised.exception))
            self.assertNotIn(private, repr(raised.exception))

    def test_smart_legal_selector_uses_separate_input_and_validation_snapshot(self):
        original_item = safe_item("current original memory")
        service = FakeReadService([original_item])
        captured = {}
        real_selector = (
            memory_context_integration.memory_retrieval.select_relevant_memory_items
        )
        real_validator = memory_context_integration._validated_selection_items

        def select(items, **kwargs):
            captured["selector_input"] = items
            return real_selector(items, **kwargs)

        def validate(selection, *, candidates):
            captured["candidate_snapshot"] = candidates
            return real_validator(selection, candidates=candidates)

        with (
            mock.patch.object(
                memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
                side_effect=select,
            ),
            mock.patch.object(
                memory_context_integration,
                "_validated_selection_items",
                side_effect=validate,
            ),
        ):
            result = memory_context_integration.prepare_transient_memory_dispatch(
                service,
                self.base,
                enabled=True,
                smart_retrieval_enabled=True,
            )

        selector_input = captured["selector_input"]
        candidate_snapshot = captured["candidate_snapshot"]
        self.assertIsNot(selector_input, candidate_snapshot)
        self.assertEqual(selector_input, candidate_snapshot)
        self.assertIsNot(selector_input[0], candidate_snapshot[0])
        self.assertIsNot(selector_input[0], original_item)
        self.assertIsNot(candidate_snapshot[0], original_item)
        self.assertEqual(original_item["normalized_content"], "current original memory")
        self.assertTrue(result.memory_applied)
        self.assertIn(
            original_item["normalized_content"],
            result.provider_messages[-2]["content"],
        )

    def test_smart_no_match_is_exact_noop_without_recency_fallback(self):
        base = (
            {"role": "user", "content": "orchid history"},
            {"role": "assistant", "content": "orchid answer"},
            {"role": "user", "content": "weather today"},
        )
        service = FakeReadService([safe_item("orchid history")])
        result = memory_context_integration.prepare_transient_memory_dispatch(
            service,
            base,
            enabled=True,
            smart_retrieval_enabled=True,
        )
        self.assertIs(result.provider_messages, base)
        self.assertFalse(result.memory_applied)
        self.assertEqual(len(service.calls), 1)

    def test_smart_selector_failures_and_invalid_results_are_data_free(self):
        private_text = "PRIVATE-QUERY-MEMORY-TOKEN-SCORE"
        uninitialized_selection = object.__new__(
            memory_context_integration.memory_retrieval.MemoryRetrievalSelectionV1
        )
        invalid_selection = (
            memory_context_integration.memory_retrieval.MemoryRetrievalSelectionV1(
                items=(safe_item(private_text),),
                candidate_count=1,
                selected_count=0,
                query_signal_count=1,
            )
        )
        failures = (
            memory_context_integration.memory_retrieval.MemoryRetrievalError(
                "invalid_query"
            ),
            RuntimeError(private_text),
            object(),
            uninitialized_selection,
            invalid_selection,
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                side_effect = failure if isinstance(failure, Exception) else None
                return_value = failure if side_effect is None else mock.DEFAULT
                with mock.patch.object(
                    memory_context_integration.memory_retrieval,
                    "select_relevant_memory_items",
                    side_effect=side_effect,
                    return_value=return_value,
                ), self.assertRaisesRegex(
                    memory_context_integration.MemoryContextIntegrationError,
                    r"^memory_context_unavailable$",
                ) as raised:
                    memory_context_integration.prepare_transient_memory_dispatch(
                        FakeReadService([safe_item("current")]),
                        self.base,
                        enabled=True,
                        smart_retrieval_enabled=True,
                    )
                self.assertNotIn(private_text, str(raised.exception))
                self.assertNotIn(private_text, repr(raised.exception))

    def test_smart_forged_non_candidate_fails_before_renderer_data_free(self):
        query = "PRIVATE-QUERY-TOKEN-SCORE"
        candidate = safe_item(f"{query} real candidate", marker="A")
        forged = safe_item(f"{query} FORGED-MEMORY-PLAINTEXT", marker="Z")
        selection = (
            memory_context_integration.memory_retrieval.MemoryRetrievalSelectionV1(
                items=(dict(forged),),
                candidate_count=1,
                selected_count=1,
                query_signal_count=1,
            )
        )
        with (
            mock.patch.object(
                memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
                return_value=selection,
            ),
            mock.patch.object(
                memory_context_integration.memory_context,
                "render_memory_developer_message",
            ) as renderer,
            self.assertRaisesRegex(
                memory_context_integration.MemoryContextIntegrationError,
                r"^memory_context_unavailable$",
            ) as raised,
        ):
            memory_context_integration.prepare_transient_memory_dispatch(
                FakeReadService([candidate]),
                ({"role": "user", "content": query},),
                enabled=True,
                smart_retrieval_enabled=True,
            )
        renderer.assert_not_called()
        for private in (
            query,
            candidate["normalized_content"],
            forged["normalized_content"],
            forged["memory_key"],
            "token",
            "score",
        ):
            self.assertNotIn(private, str(raised.exception))
            self.assertNotIn(private, repr(raised.exception))

    def test_smart_duplicate_selection_cannot_consume_one_candidate_twice(self):
        candidate = safe_item("current duplicate candidate")
        selection = (
            memory_context_integration.memory_retrieval.MemoryRetrievalSelectionV1(
                items=(dict(candidate), dict(candidate)),
                candidate_count=1,
                selected_count=2,
                query_signal_count=1,
            )
        )
        with (
            mock.patch.object(
                memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
                return_value=selection,
            ),
            mock.patch.object(
                memory_context_integration.memory_context,
                "render_memory_developer_message",
            ) as renderer,
            self.assertRaisesRegex(
                memory_context_integration.MemoryContextIntegrationError,
                r"^memory_context_unavailable$",
            ),
        ):
            memory_context_integration.prepare_transient_memory_dispatch(
                FakeReadService([candidate]),
                self.base,
                enabled=True,
                smart_retrieval_enabled=True,
            )
        renderer.assert_not_called()

    def test_smart_duplicate_selection_can_consume_two_identical_candidates(self):
        candidate = safe_item("current duplicate candidate")
        candidates = [dict(candidate), dict(candidate)]
        selection = (
            memory_context_integration.memory_retrieval.MemoryRetrievalSelectionV1(
                items=(dict(candidate), dict(candidate)),
                candidate_count=2,
                selected_count=2,
                query_signal_count=1,
            )
        )
        real_renderer = (
            memory_context_integration.memory_context.render_memory_developer_message
        )
        with (
            mock.patch.object(
                memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
                return_value=selection,
            ),
            mock.patch.object(
                memory_context_integration.memory_context,
                "render_memory_developer_message",
                wraps=real_renderer,
            ) as renderer,
        ):
            result = memory_context_integration.prepare_transient_memory_dispatch(
                FakeReadService(candidates),
                self.base,
                enabled=True,
                smart_retrieval_enabled=True,
            )
        renderer.assert_called_once()
        decoded = json.loads(result.provider_messages[-2]["content"])
        self.assertEqual(decoded["memory_context"]["item_count"], 2)
        self.assertEqual(candidates, [candidate, candidate])

    def test_smart_nonempty_selection_requires_bounded_query_signal(self):
        candidate = safe_item("current signal candidate")
        selections = (
            memory_context_integration.memory_retrieval.MemoryRetrievalSelectionV1(
                items=(dict(candidate),),
                candidate_count=1,
                selected_count=1,
                query_signal_count=0,
            ),
            memory_context_integration.memory_retrieval.MemoryRetrievalSelectionV1(
                items=(),
                candidate_count=1,
                selected_count=0,
                query_signal_count=(
                    memory_context_integration.memory_retrieval.QUERY_MAX_CHARS + 1
                ),
            ),
        )
        for selection in selections:
            with (
                self.subTest(query_signal_count=selection.query_signal_count),
                mock.patch.object(
                    memory_context_integration.memory_retrieval,
                    "select_relevant_memory_items",
                    return_value=selection,
                ),
                mock.patch.object(
                    memory_context_integration.memory_context,
                    "render_memory_developer_message",
                ) as renderer,
                self.assertRaisesRegex(
                    memory_context_integration.MemoryContextIntegrationError,
                    r"^memory_context_unavailable$",
                ),
            ):
                memory_context_integration.prepare_transient_memory_dispatch(
                    FakeReadService([candidate]),
                    self.base,
                    enabled=True,
                    smart_retrieval_enabled=True,
                )
            renderer.assert_not_called()

    def test_smart_combined_final_budget_excess_fails_instead_of_truncating(self):
        first = safe_item("current " + "A" * 1095, marker="A")
        second = safe_item("current " + "B" * 1095, marker="B")
        self.assertLessEqual(len(first["normalized_content"]), 2000)
        self.assertLessEqual(len(second["normalized_content"]), 2000)
        self.assertGreater(
            len(first["normalized_content"]) + len(second["normalized_content"]),
            2000,
        )
        selection = (
            memory_context_integration.memory_retrieval.MemoryRetrievalSelectionV1(
                items=(dict(first), dict(second)),
                candidate_count=2,
                selected_count=2,
                query_signal_count=1,
            )
        )
        with (
            mock.patch.object(
                memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
                return_value=selection,
            ),
            mock.patch.object(
                memory_context_integration.memory_context,
                "render_memory_developer_message",
            ) as renderer,
            self.assertRaisesRegex(
                memory_context_integration.MemoryContextIntegrationError,
                r"^memory_context_unavailable$",
            ),
        ):
            memory_context_integration.prepare_transient_memory_dispatch(
                FakeReadService([first, second]),
                self.base,
                enabled=True,
                smart_retrieval_enabled=True,
            )
        renderer.assert_not_called()

    def test_smart_single_item_over_final_budget_fails_before_renderer(self):
        candidate = safe_item("current " + "X" * 2000)
        self.assertGreater(len(candidate["normalized_content"]), 2000)
        selection = (
            memory_context_integration.memory_retrieval.MemoryRetrievalSelectionV1(
                items=(dict(candidate),),
                candidate_count=1,
                selected_count=1,
                query_signal_count=1,
            )
        )
        with (
            mock.patch.object(
                memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
                return_value=selection,
            ),
            mock.patch.object(
                memory_context_integration.memory_context,
                "render_memory_developer_message",
            ) as renderer,
            self.assertRaisesRegex(
                memory_context_integration.MemoryContextIntegrationError,
                r"^memory_context_unavailable$",
            ),
        ):
            memory_context_integration.prepare_transient_memory_dispatch(
                FakeReadService([candidate]),
                self.base,
                enabled=True,
                smart_retrieval_enabled=True,
            )
        renderer.assert_not_called()

    def test_smart_legal_reordered_candidate_subset_preserves_result_order(self):
        first = safe_item("current alpha memory", marker="A")
        second = safe_item("current beta memory", marker="B")
        third = safe_item("current gamma memory", marker="C")
        candidates = [first, second, third]
        selection = (
            memory_context_integration.memory_retrieval.MemoryRetrievalSelectionV1(
                items=(dict(second), dict(first)),
                candidate_count=3,
                selected_count=2,
                query_signal_count=1,
            )
        )
        with mock.patch.object(
            memory_context_integration.memory_retrieval,
            "select_relevant_memory_items",
            return_value=selection,
        ):
            result = memory_context_integration.prepare_transient_memory_dispatch(
                FakeReadService(candidates),
                self.base,
                enabled=True,
                smart_retrieval_enabled=True,
            )
        decoded = json.loads(result.provider_messages[-2]["content"])
        self.assertEqual(
            [
                item["normalized_content"]
                for item in decoded["memory_context"]["items"]
            ],
            [second["normalized_content"], first["normalized_content"]],
        )
        self.assertEqual(candidates, [first, second, third])

    def test_smart_candidate_equality_exception_is_fixed_and_data_free(self):
        private_text = "PRIVATE-EQUALITY-PLAINTEXT-MEMORY-KEY-TOKEN-SCORE"

        class ExplodingEquality:
            def __eq__(self, _other):
                raise RuntimeError(private_text)

        candidate = safe_item("current equality candidate")
        candidate["comparison"] = ExplodingEquality()
        selected = dict(candidate)
        selected["comparison"] = ExplodingEquality()
        selection = (
            memory_context_integration.memory_retrieval.MemoryRetrievalSelectionV1(
                items=(selected,),
                candidate_count=1,
                selected_count=1,
                query_signal_count=1,
            )
        )
        with (
            mock.patch.object(
                memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
                return_value=selection,
            ),
            mock.patch.object(
                memory_context_integration.memory_context,
                "render_memory_developer_message",
            ) as renderer,
            self.assertRaisesRegex(
                memory_context_integration.MemoryContextIntegrationError,
                r"^memory_context_unavailable$",
            ) as raised,
        ):
            memory_context_integration.prepare_transient_memory_dispatch(
                FakeReadService([candidate]),
                self.base,
                enabled=True,
                smart_retrieval_enabled=True,
            )
        renderer.assert_not_called()
        self.assertNotIn(private_text, str(raised.exception))
        self.assertNotIn(private_text, repr(raised.exception))

    def test_smart_renderer_revalidates_selector_output(self):
        private_text = "SENSITIVE-SELECTOR-PLAINTEXT"
        sensitive_item = safe_item(private_text, sensitivity="sensitive")
        forged = (
            memory_context_integration.memory_retrieval.MemoryRetrievalSelectionV1(
                items=(dict(sensitive_item),),
                candidate_count=1,
                selected_count=1,
                query_signal_count=1,
            )
        )
        real_renderer = (
            memory_context_integration.memory_context.render_memory_developer_message
        )
        with (
            mock.patch.object(
                memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
                return_value=forged,
            ),
            mock.patch.object(
                memory_context_integration.memory_context,
                "render_memory_developer_message",
                wraps=real_renderer,
            ) as renderer,
            self.assertRaisesRegex(
                memory_context_integration.MemoryContextIntegrationError,
                r"^memory_context_unavailable$",
            ) as raised,
        ):
            memory_context_integration.prepare_transient_memory_dispatch(
                FakeReadService([sensitive_item]),
                self.base,
                enabled=True,
                smart_retrieval_enabled=True,
            )
        renderer.assert_called_once()
        self.assertNotIn(private_text, str(raised.exception))
        self.assertNotIn(private_text, repr(raised.exception))

    def test_unicode_query_failure_is_fixed_and_happens_before_read(self):
        service = FakeReadService(error=AssertionError("must not read"))
        base = ({"role": "user", "content": "\ud800"},)
        with self.assertRaisesRegex(
            memory_context_integration.MemoryContextIntegrationError,
            r"^memory_context_unavailable$",
        ) as raised:
            memory_context_integration.prepare_transient_memory_dispatch(
                service,
                base,
                enabled=True,
                smart_retrieval_enabled=True,
            )
        self.assertEqual(service.calls, [])
        self.assertNotIn("UnicodeEncodeError", repr(raised.exception))

    def test_100_client_messages_without_persona_become_101_with_memory(self):
        self.assertEqual(
            memory_context_integration.CLIENT_MAX_MESSAGES,
            kelivo_service.MAX_MESSAGES,
        )
        self.assertEqual(
            memory_context_integration.BASE_PROVIDER_MAX_MESSAGES, 101
        )
        self.assertEqual(
            memory_context_integration.TRANSIENT_DISPATCH_MAX_MESSAGES, 102
        )
        client_messages = tuple(
            [{"role": "assistant", "content": f"history-{index}"} for index in range(99)]
            + [{"role": "user", "content": "current"}]
        )
        result = memory_context_integration.prepare_transient_memory_dispatch(
            FakeReadService([safe_item("memory")]), client_messages, enabled=True,
            smart_retrieval_enabled=False,
        )
        self.assertEqual(len(result.provider_messages), 101)
        self.assertEqual(result.provider_messages[-1]["role"], "user")
        self.assertLess(len(result.provider_messages[-2]["content"]), 32_000)

    def test_101_base_with_persona_stays_101_empty_and_becomes_102_with_memory(self):
        client_messages = tuple(
            [{"role": "assistant", "content": f"history-{index}"} for index in range(99)]
            + [{"role": "user", "content": "current"}]
        )
        base = ({"role": "system", "content": "server persona"}, *client_messages)
        empty = memory_context_integration.prepare_transient_memory_dispatch(
            FakeReadService([]), base, enabled=True, smart_retrieval_enabled=False
        )
        applied = memory_context_integration.prepare_transient_memory_dispatch(
            FakeReadService([safe_item("memory")]), base, enabled=True,
            smart_retrieval_enabled=False,
        )

        self.assertIs(empty.provider_messages, base)
        self.assertEqual(len(empty.provider_messages), 101)
        self.assertFalse(empty.memory_applied)
        self.assertEqual(len(applied.provider_messages), 102)
        self.assertTrue(applied.memory_applied)
        self.assertEqual(applied.provider_messages[-2]["role"], "developer")
        self.assertEqual(applied.provider_messages[-1]["role"], "user")

    def test_102_base_and_nominal_103_dispatch_fail_closed(self):
        base = tuple(
            [{"role": "assistant", "content": f"history-{index}"} for index in range(101)]
            + [{"role": "user", "content": "current"}]
        )
        no_memory_read = FakeReadService([])
        with self.assertRaisesRegex(
            memory_context_integration.MemoryContextIntegrationError,
            r"^memory_context_unavailable$",
        ):
            memory_context_integration.prepare_transient_memory_dispatch(
                no_memory_read, base, enabled=True, smart_retrieval_enabled=False
            )
        self.assertEqual(no_memory_read.calls, [])

        with mock.patch.object(
            memory_context_integration,
            "_validate_base_messages",
            return_value=base,
        ):
            with self.assertRaisesRegex(
                memory_context_integration.MemoryContextIntegrationError,
                r"^memory_context_unavailable$",
            ):
                memory_context_integration.prepare_transient_memory_dispatch(
                    FakeReadService([safe_item("memory")]), base, enabled=True,
                    smart_retrieval_enabled=False,
                )

    def test_module_has_no_database_network_log_or_hash_behavior(self):
        source = Path(memory_context_integration.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".", 1)[0])
        self.assertEqual(
            imports,
            {
                "__future__",
                "dataclasses",
                "typing",
                "memory_context",
                "memory_retrieval",
                "memory_retrieval_v2_shadow",
            },
        )
        for forbidden in (
            "sqlite3",
            "httpx",
            "requests",
            "socket",
            "print(",
            "logging",
            "hash(",
            "bundle_hash",
            "os.environ",
        ):
            self.assertNotIn(forbidden, source)

        with (
            mock.patch.object(sqlite3, "connect") as database,
            mock.patch.object(socket, "create_connection") as network,
        ):
            memory_context_integration.prepare_transient_memory_dispatch(
                FakeReadService([]), self.base, enabled=True,
                smart_retrieval_enabled=False,
            )
        database.assert_not_called()
        network.assert_not_called()


class MemoryContextDispatchIntegrationTests(
    NoNetworkMixin, unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self):
        module_names = (
            "backend.app",
            "backend.telegram_integration",
            "backend.channel_store",
            "backend.kelivo_service",
            "backend.heartbeat_service",
            "backend.memory_policy",
            "backend.memory_runtime",
            "backend.memory_store",
            "backend.memory_service",
            "backend.memory_explicit_actions",
            "backend.memory_context",
            "backend.memory_context_integration",
            "backend.memory_retrieval",
        )
        missing = object()
        modules_before = {
            name: sys.modules.get(name, missing) for name in module_names
        }
        package = sys.modules["backend"]
        attributes_before = {
            name.rsplit(".", 1)[-1]: getattr(
                package, name.rsplit(".", 1)[-1], missing
            )
            for name in module_names
        }

        def restore_import_state():
            for name, value in modules_before.items():
                if value is missing:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value
            for attribute, value in attributes_before.items():
                if value is missing:
                    if hasattr(package, attribute):
                        delattr(package, attribute)
                else:
                    setattr(package, attribute, value)

        self.addCleanup(restore_import_state)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(
            self.temp.name,
            kelivo=True,
            auto_idempotency=True,
            operit_share=True,
            memory=True,
            memory_context=True,
        )
        self.headers = {
            "Authorization": "Bearer test-kelivo-key-distinct-1234567890",
            "Idempotency-Key": "memory-context-key-0001",
        }
        self.provider_calls = []

        async def generate(
            messages, api_session, provider_model, temperature, max_tokens, context
        ):
            self.provider_calls.append((messages, context))
            return {
                "text": "model reply",
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            }

        self.module.KELIVO_GENERATOR = generate

    def enable_smart_retrieval(self):
        self.module.DEPLOYMENT = dataclasses.replace(
            self.module.DEPLOYMENT,
            memory=dataclasses.replace(
                self.module.DEPLOYMENT.memory,
                smart_retrieval_enabled=True,
            ),
        )

    @staticmethod
    def payload(text: str = "current question") -> dict:
        return {
            "model": "ouou-home",
            "messages": [{"role": "user", "content": text}],
            "stream": False,
        }

    async def post(self, *, key: str, text: str = "current question"):
        headers = dict(self.headers)
        headers["Idempotency-Key"] = key
        return await request(
            self.module,
            "POST",
            "/v1/chat/completions",
            headers=headers,
            json=self.payload(text),
        )

    def test_real_memory_read_has_zero_sqlite_total_changes(self):
        total_changes = []
        original_exit = self.module.channel_store.ClosingConnection.__exit__

        def tracking_exit(connection, exc_type, exc_value, traceback):
            total_changes.append(connection.total_changes)
            return original_exit(connection, exc_type, exc_value, traceback)

        with mock.patch.object(
            self.module.channel_store.ClosingConnection, "__exit__", new=tracking_exit
        ):
            result = self.module.MEMORY_SERVICE.get_active_memories(
                scope_type="global_user",
                scope_ref="",
                limit=10,
                character_budget=2000,
                include_sensitive=False,
            )
        self.assertEqual(result, [])
        self.assertGreaterEqual(len(total_changes), 1)
        self.assertEqual(total_changes, [0] * len(total_changes))

    async def test_active_memory_is_transient_and_inserted_before_final_user(self):
        plaintext = "MEMORY-PLAINTEXT-NEVER-PERSIST"
        service = FakeReadService([safe_item(plaintext)])
        self.module.MEMORY_SERVICE = service
        self.assertFalse(self.module.DEPLOYMENT.memory.smart_retrieval_enabled)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            response = await self.post(key="memory-transient-key-0001")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(service.calls[0], {
            "scope_type": "global_user",
            "scope_ref": "",
            "limit": 10,
            "character_budget": 2000,
            "include_sensitive": False,
        })
        self.assertEqual(len(self.provider_calls), 1)
        messages, context = self.provider_calls[0]
        self.assertEqual(messages[-1], {"role": "user", "content": "current question"})
        self.assertEqual(messages[-2]["role"], "developer")
        self.assertIn(plaintext, messages[-2]["content"])
        self.assertEqual(
            context["transient_memory_dispatch"],
            "kelivo-transient-memory-dispatch-v1",
        )
        with self.module.db() as conn:
            row = conn.execute(
                """SELECT provider_messages_json,context_bundle_json,
                          context_bundle_hash,request_identity_hash,prompt_contract_version
                   FROM kelivo_requests"""
            ).fetchone()
            persisted_rows_dump = "\n".join(
                statement
                for statement in conn.iterdump()
                if statement.startswith("INSERT INTO")
            )
        self.assertEqual(row["prompt_contract_version"], "kelivo-provider-prompt-v1")
        self.assertNotIn(plaintext, row["provider_messages_json"])
        self.assertNotIn(plaintext, row["context_bundle_json"])
        self.assertNotIn("transient_memory_dispatch", row["context_bundle_json"])
        self.assertNotIn(plaintext, persisted_rows_dump)
        self.assertNotIn(
            "kelivo-transient-memory-dispatch-v1",
            persisted_rows_dump,
        )
        self.assertNotIn(plaintext, stdout.getvalue())
        self.assertNotIn(plaintext, stderr.getvalue())

    async def test_smart_dispatch_uses_final_user_and_persists_no_memory_metadata(self):
        self.enable_smart_retrieval()
        relevant_text = "blue tea PRIVATE-MEMORY-ONLY"
        early_only_text = "orchid history PRIVATE-EARLY-MEMORY"
        service = FakeReadService([
            safe_item(early_only_text, marker="A"),
            safe_item(relevant_text, marker="B"),
        ])
        self.module.MEMORY_SERVICE = service
        response = await request(
            self.module,
            "POST",
            "/v1/chat/completions",
            headers={
                **self.headers,
                "Idempotency-Key": "smart-final-query-key-0001",
            },
            json={
                "model": "ouou-home",
                "messages": [
                    {"role": "user", "content": "orchid history"},
                    {"role": "assistant", "content": "earlier reply"},
                    {"role": "user", "content": "blue tea today"},
                ],
                "stream": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(service.calls, [{
            "scope_type": "global_user",
            "scope_ref": "",
            "limit": 20,
            "character_budget": 8000,
            "include_sensitive": False,
        }])
        self.assertEqual(len(self.provider_calls), 1)
        messages, context = self.provider_calls[0]
        self.assertEqual(messages[-1], {"role": "user", "content": "blue tea today"})
        self.assertEqual(messages[-2]["role"], "developer")
        self.assertIn(relevant_text, messages[-2]["content"])
        self.assertNotIn(early_only_text, messages[-2]["content"])
        self.assertEqual(
            context["transient_memory_dispatch"],
            "kelivo-transient-memory-dispatch-v1",
        )
        with self.module.db() as conn:
            row = conn.execute(
                """SELECT provider_messages_json,context_bundle_json,
                          context_bundle_hash,request_identity_hash
                   FROM kelivo_requests"""
            ).fetchone()
            snapshot_count = conn.execute(
                "SELECT count(*) FROM companion_context_snapshots"
            ).fetchone()[0]
            persisted_rows_dump = "\n".join(
                statement
                for statement in conn.iterdump()
                if statement.startswith("INSERT INTO")
            )
        self.assertEqual(snapshot_count, 0)
        for plaintext in (relevant_text, early_only_text):
            self.assertNotIn(plaintext, row["provider_messages_json"])
            self.assertNotIn(plaintext, row["context_bundle_json"])
            self.assertNotIn(plaintext, persisted_rows_dump)
        for metadata in (
            "candidate_count",
            "selected_count",
            "query_signal_count",
            "transient_memory_dispatch",
        ):
            self.assertNotIn(metadata, persisted_rows_dump)

    async def test_smart_no_match_dispatches_base_without_marker_or_fallback(self):
        self.enable_smart_retrieval()
        private_text = "orchid-only-memory"
        self.module.MEMORY_SERVICE = FakeReadService([safe_item(private_text)])
        response = await self.post(
            key="smart-no-match-key-0001",
            text="weather today",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.provider_calls), 1)
        messages, context = self.provider_calls[0]
        self.assertEqual(messages[-1], {"role": "user", "content": "weather today"})
        self.assertFalse(any(message["role"] == "developer" for message in messages[1:]))
        self.assertNotIn("transient_memory_dispatch", context)
        self.assertNotIn(private_text, json.dumps(self.provider_calls))

    async def test_smart_selector_runs_after_begin_dispatch_before_provider(self):
        self.enable_smart_retrieval()
        self.module.MEMORY_SERVICE = FakeReadService([
            safe_item("current question memory")
        ])
        events = []
        original_begin = self.module.kelivo_service.begin_dispatch
        original_selector = (
            self.module.memory_context_integration.memory_retrieval
            .select_relevant_memory_items
        )

        def begin(*args, **kwargs):
            events.append("begin_dispatch")
            return original_begin(*args, **kwargs)

        def select(*args, **kwargs):
            events.append("selector")
            return original_selector(*args, **kwargs)

        async def generate(*args):
            events.append("provider")
            self.provider_calls.append((args[0], args[-1]))
            return {"text": "ordered", "usage": {}}

        self.module.KELIVO_GENERATOR = generate
        with (
            mock.patch.object(
                self.module.kelivo_service,
                "begin_dispatch",
                side_effect=begin,
            ),
            mock.patch.object(
                self.module.memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
                side_effect=select,
            ),
        ):
            response = await self.post(key="smart-order-key-0001")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(events, ["begin_dispatch", "selector", "provider"])

    async def test_smart_selector_failure_is_pre_provider_deterministic_failed(self):
        self.enable_smart_retrieval()
        private_text = "PRIVATE-SELECTOR-FAILURE"
        self.module.MEMORY_SERVICE = FakeReadService([
            safe_item("current question memory")
        ])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                self.module.memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
                side_effect=RuntimeError(private_text),
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            response = await self.post(key="smart-selector-failure-key-0001")

        self.assertEqual(
            (response.status_code, response.json()["error"]["code"]),
            (503, "memory_context_unavailable"),
        )
        self.assertEqual(self.provider_calls, [])
        with self.module.db() as conn:
            row = conn.execute(
                "SELECT status,error_category FROM kelivo_requests"
            ).fetchone()
            database_dump = "\n".join(conn.iterdump())
        self.assertEqual(
            (row["status"], row["error_category"]),
            ("failed", "memory_context_unavailable"),
        )
        self.assertNotIn(private_text, database_dump)
        self.assertNotIn(private_text, stdout.getvalue())
        self.assertNotIn(private_text, stderr.getvalue())

    async def test_100_client_messages_persist_base_101_but_dispatch_102(self):
        self.enable_smart_retrieval()
        plaintext = "current TRANSIENT-102ND-MESSAGE"
        service = FakeReadService([safe_item(plaintext)])
        self.module.MEMORY_SERVICE = service
        client_messages = [
            {"role": "assistant", "content": f"history-{index}"}
            for index in range(99)
        ] + [{"role": "user", "content": "current"}]
        response = await request(
            self.module,
            "POST",
            "/v1/chat/completions",
            headers={
                **self.headers,
                "Idempotency-Key": "memory-102-boundary-key-0001",
            },
            json={
                "model": "ouou-home",
                "messages": client_messages,
                "stream": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.provider_calls), 1)
        dispatched_messages, _context = self.provider_calls[0]
        self.assertEqual(len(dispatched_messages), 102)
        self.assertEqual(dispatched_messages[-2]["role"], "developer")
        self.assertEqual(dispatched_messages[-1]["role"], "user")
        self.assertIn(plaintext, dispatched_messages[-2]["content"])
        with self.module.db() as conn:
            row = conn.execute(
                "SELECT provider_messages_json FROM kelivo_requests"
            ).fetchone()
        persisted_messages = json.loads(row["provider_messages_json"])
        self.assertEqual(len(persisted_messages), 101)
        self.assertEqual(persisted_messages[0]["role"], "system")
        self.assertEqual(persisted_messages[-1]["role"], "user")
        self.assertNotIn(plaintext, row["provider_messages_json"])

    async def test_empty_and_forgotten_visibility_are_reread_per_new_dispatch(self):
        self.enable_smart_retrieval()
        plaintext = "same question VISIBLE-ONLY-ON-FIRST-DISPATCH"
        service = FakeReadService([safe_item(plaintext)])
        self.module.MEMORY_SERVICE = service
        first = await self.post(key="memory-visible-key-0001", text="same question")
        service.items = []
        second = await self.post(key="memory-visible-key-0002", text="same question")

        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(len(service.calls), 2)
        self.assertIn(plaintext, json.dumps(self.provider_calls[0]))
        self.assertNotIn(plaintext, json.dumps(self.provider_calls[1]))
        self.assertNotIn("transient_memory_dispatch", self.provider_calls[1][1])
        self.assertEqual(
            self.provider_calls[1][0][-1],
            {"role": "user", "content": "same question"},
        )
        with self.module.db() as conn:
            rows = conn.execute(
                """SELECT provider_messages_json,context_bundle_json,
                          context_bundle_hash,request_identity_hash
                   FROM kelivo_requests ORDER BY id"""
            ).fetchall()
            snapshot_count = conn.execute(
                "SELECT count(*) FROM companion_context_snapshots"
            ).fetchone()[0]
        self.assertEqual(snapshot_count, 0)
        for field in (
            "provider_messages_json",
            "context_bundle_json",
            "context_bundle_hash",
            "request_identity_hash",
        ):
            self.assertEqual(rows[0][field], rows[1][field])

    async def test_explicit_and_automatic_replays_do_not_reread_memory(self):
        self.enable_smart_retrieval()
        service = FakeReadService([safe_item("current question automatic memory")])
        self.module.MEMORY_SERVICE = service
        real_selector = (
            self.module.memory_context_integration.memory_retrieval
            .select_relevant_memory_items
        )
        with mock.patch.object(
            self.module.memory_context_integration.memory_retrieval,
            "select_relevant_memory_items",
            wraps=real_selector,
        ) as selector:
            explicit_first = await self.post(key="memory-replay-key-0001")
            explicit_replay = await self.post(key="memory-replay-key-0001")
            automatic_headers = {"Authorization": self.headers["Authorization"]}
            automatic_first = await request(
                self.module,
                "POST",
                "/v1/chat/completions",
                headers=automatic_headers,
                json=self.payload("automatic"),
            )
            automatic_replay = await request(
                self.module,
                "POST",
                "/v1/chat/completions",
                headers=automatic_headers,
                json=self.payload("automatic"),
            )

        self.assertEqual(
            [
                explicit_first.status_code,
                explicit_replay.status_code,
                automatic_first.status_code,
                automatic_replay.status_code,
            ],
            [200, 200, 200, 200],
        )
        self.assertEqual(explicit_first.json(), explicit_replay.json())
        self.assertEqual(automatic_first.json(), automatic_replay.json())
        self.assertEqual(len(service.calls), 2)
        self.assertEqual(selector.call_count, 2)
        self.assertEqual(len(self.provider_calls), 2)

    async def test_blocked_duplicate_does_not_reread_memory(self):
        self.enable_smart_retrieval()
        service = FakeReadService([safe_item("current question one read")])
        self.module.MEMORY_SERVICE = service
        started = asyncio.Event()
        release = asyncio.Event()

        async def pending(*args):
            self.provider_calls.append((args[0], args[-1]))
            started.set()
            await release.wait()
            return {"text": "done", "usage": {}}

        self.module.KELIVO_GENERATOR = pending
        real_selector = (
            self.module.memory_context_integration.memory_retrieval
            .select_relevant_memory_items
        )
        with mock.patch.object(
            self.module.memory_context_integration.memory_retrieval,
            "select_relevant_memory_items",
            wraps=real_selector,
        ) as selector:
            first = asyncio.create_task(self.post(key="memory-blocked-key-0001"))
            await started.wait()
            blocked = await self.post(key="memory-blocked-key-0001")
            release.set()
            first_response = await first
        self.assertEqual(
            (blocked.status_code, blocked.json()["error"]["code"]),
            (409, "idempotency_in_progress"),
        )
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(selector.call_count, 1)
        self.assertEqual(first_response.status_code, 200)

    async def test_read_and_render_failures_fail_closed_without_provider(self):
        private_text = "PRIVATE-MEMORY-FAILURE-DETAIL"
        self.module.MEMORY_SERVICE = FakeReadService(error=RuntimeError(private_text))
        read_failure = await self.post(key="memory-read-failure-0001")

        self.module.MEMORY_SERVICE = FakeReadService([safe_item("safe input")])
        with mock.patch.object(
            self.module.memory_context_integration.memory_context,
            "render_memory_developer_message",
            side_effect=RuntimeError(private_text),
        ):
            render_failure = await self.post(key="memory-render-failure-0001")

        for response in (read_failure, render_failure):
            self.assertEqual(
                (response.status_code, response.json()["error"]["code"]),
                (503, "memory_context_unavailable"),
            )
        self.assertEqual(self.provider_calls, [])
        with self.module.db() as conn:
            rows = conn.execute(
                "SELECT status,error_category FROM kelivo_requests ORDER BY id"
            ).fetchall()
            database_dump = "\n".join(conn.iterdump())
        self.assertEqual(
            [(row["status"], row["error_category"]) for row in rows],
            [("failed", "memory_context_unavailable")] * 2,
        )
        self.assertNotIn(private_text, database_dump)

    async def test_cancellation_during_memory_read_is_before_dispatch(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingReadService:
            def get_active_memories(_self, **_kwargs):
                started.set()
                release.wait(5)
                return []

        self.module.MEMORY_SERVICE = BlockingReadService()
        task = asyncio.create_task(self.post(key="memory-cancel-key-0001"))
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        task.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        with self.module.db() as conn:
            row = conn.execute(
                "SELECT status,error_category FROM kelivo_requests"
            ).fetchone()
        self.assertEqual(
            (row["status"], row["error_category"]),
            ("failed", "request_cancelled_before_dispatch"),
        )
        self.assertEqual(self.provider_calls, [])

    async def test_cancellation_after_provider_start_keeps_uncertain_semantics(self):
        service = FakeReadService([safe_item("transient before provider")])
        self.module.MEMORY_SERVICE = service
        started = asyncio.Event()

        async def pending(*args):
            self.provider_calls.append((args[0], args[-1]))
            started.set()
            await asyncio.Future()

        self.module.KELIVO_GENERATOR = pending
        task = asyncio.create_task(self.post(key="memory-provider-cancel-key-0001"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.module.db() as conn:
            row = conn.execute(
                "SELECT status,error_category FROM kelivo_requests"
            ).fetchone()
        self.assertEqual(
            (row["status"], row["error_category"]),
            ("dispatch_uncertain", "client_cancelled_after_dispatch"),
        )
        replay = await self.post(key="memory-provider-cancel-key-0001")
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(len(self.provider_calls), 1)

    async def test_operit_and_disabled_kelivo_never_read_memory(self):
        self.enable_smart_retrieval()
        service = FakeReadService(error=AssertionError("memory must not be read"))
        self.module.MEMORY_SERVICE = service
        with mock.patch.object(
            self.module.memory_context_integration.memory_retrieval,
            "select_relevant_memory_items",
        ) as selector:
            operit = await request(
                self.module,
                "POST",
                "/v1/operit/share",
                headers={
                    "Authorization": "Bearer test-operit-share-key-distinct-1234567890"
                },
                json=self.payload("operit share"),
            )
            self.module.DEPLOYMENT = dataclasses.replace(
                self.module.DEPLOYMENT,
                memory=dataclasses.replace(
                    self.module.DEPLOYMENT.memory,
                    context_injection_enabled=False,
                ),
            )
            disabled = await self.post(
                key="memory-disabled-key-0001", text="disabled"
            )
        self.assertEqual(operit.status_code, 200)
        self.assertEqual(disabled.status_code, 200)
        self.assertEqual(service.calls, [])
        self.assertNotIn("transient_memory_dispatch", self.provider_calls[-2][1])
        self.assertNotIn("transient_memory_dispatch", self.provider_calls[-1][1])
        selector.assert_not_called()

    async def test_disabled_100_client_messages_plus_persona_dispatch_101(self):
        service = FakeReadService(error=AssertionError("memory must not be read"))
        self.module.MEMORY_SERVICE = service
        self.module.DEPLOYMENT = dataclasses.replace(
            self.module.DEPLOYMENT,
            memory=dataclasses.replace(
                self.module.DEPLOYMENT.memory,
                context_injection_enabled=False,
            ),
        )
        client_messages = [
            {"role": "assistant", "content": f"history-{index}"}
            for index in range(99)
        ] + [{"role": "user", "content": "current"}]
        response = await request(
            self.module,
            "POST",
            "/v1/chat/completions",
            headers={
                **self.headers,
                "Idempotency-Key": "memory-disabled-boundary-key-0001",
            },
            json={
                "model": "ouou-home",
                "messages": client_messages,
                "stream": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(service.calls, [])
        self.assertEqual(len(self.provider_calls), 1)
        messages, context = self.provider_calls[0]
        self.assertEqual(len(messages), 101)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertNotIn("transient_memory_dispatch", context)


class MemoryContextLoopClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_payload_marker_is_optional_fixed_and_transient(self):
        payloads = []

        def handler(request_message):
            payloads.append(json.loads(request_message.content))
            return httpx.Response(
                200,
                json={"ok": True, "reply": "ok", "api": {"usage": {}}},
            )

        client = kelivo_service.LoopGenerationClient(
            "http://127.0.0.1:9/loop/ingest",
            2,
            "test-internal-token",
            transport=httpx.MockTransport(handler),
        )
        no_marker_101 = tuple(
            [{"role": "assistant", "content": f"history-{index}"} for index in range(100)]
            + [{"role": "user", "content": "current"}]
        )
        marker_102 = tuple(
            [{"role": "assistant", "content": f"history-{index}"} for index in range(101)]
            + [{"role": "user", "content": "current"}]
        )
        marker_103 = tuple(
            [{"role": "assistant", "content": f"history-{index}"} for index in range(102)]
            + [{"role": "user", "content": "current"}]
        )
        base_context = {"prompt_contract_version": "kelivo-provider-prompt-v1"}
        await client.generate(
            no_marker_101, "session", "provider", 0.7, 2000, base_context
        )
        await client.generate(
            marker_102,
            "session",
            "provider",
            0.7,
            2000,
            {
                **base_context,
                "transient_memory_dispatch": "kelivo-transient-memory-dispatch-v1",
            },
        )

        self.assertNotIn("transient_memory_dispatch", payloads[0])
        self.assertEqual(
            payloads[1]["transient_memory_dispatch"],
            "kelivo-transient-memory-dispatch-v1",
        )
        self.assertEqual(
            [len(payload["provider_messages"]) for payload in payloads],
            [101, 102],
        )
        no_marker_102 = marker_102
        with self.assertRaisesRegex(
            kelivo_service.GenerationError, "invalid_loopback_message_count"
        ) as no_marker_error:
            await client.generate(
                no_marker_102, "session", "provider", 0.7, 2000, base_context
            )
        self.assertFalse(no_marker_error.exception.uncertain)
        with self.assertRaisesRegex(
            kelivo_service.GenerationError, "invalid_loopback_message_count"
        ) as marker_count_error:
            await client.generate(
                marker_103,
                "session",
                "provider",
                0.7,
                2000,
                {
                    **base_context,
                    "transient_memory_dispatch": "kelivo-transient-memory-dispatch-v1",
                },
            )
        self.assertFalse(marker_count_error.exception.uncertain)
        for invalid_marker in (None, "", "not-valid"):
            with self.subTest(marker=invalid_marker), self.assertRaisesRegex(
                kelivo_service.GenerationError,
                "invalid_transient_memory_dispatch",
            ) as marker_error:
                await client.generate(
                    no_marker_101,
                    "session",
                    "provider",
                    0.7,
                    2000,
                    {
                        **base_context,
                        "transient_memory_dispatch": invalid_marker,
                    },
                )
            self.assertFalse(marker_error.exception.uncertain)
        self.assertEqual(len(payloads), 2)


if __name__ == "__main__":
    unittest.main()
