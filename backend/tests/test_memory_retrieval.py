from __future__ import annotations

import ast
import copy
import json
import socket
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from backend import memory_context, memory_retrieval


def safe_item(
    content: str,
    *,
    kind: str = "user_preference",
    scope_type: str = "global_user",
    scope_ref: str = "",
    status: str = "active",
    sensitivity: str = "normal",
    marker: str = "A",
) -> dict:
    return {
        "memory_key": marker * 32,
        "kind": kind,
        "scope_type": scope_type,
        "scope_ref": scope_ref,
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
        "provenance": [{"source": "opaque"}],
    }


def select(items, query_text, **kwargs):
    return memory_retrieval.select_relevant_memory_items(
        items,
        query_text=query_text,
        scope_type="global_user",
        **kwargs,
    )


class MemoryRetrievalSelectionTests(unittest.TestCase):
    def test_empty_query_returns_empty_selection(self):
        result = select([safe_item("User prefers blue")], "")
        self.assertEqual(result.items, ())
        self.assertEqual(result.candidate_count, 1)
        self.assertEqual(result.selected_count, 0)
        self.assertEqual(result.query_signal_count, 0)

    def test_query_without_strong_signals_returns_empty_selection(self):
        result = select([safe_item("Punctuation exists: !")], " \r\n! … ")
        self.assertEqual(result.items, ())
        self.assertEqual(result.query_signal_count, 0)

    def test_chinese_bigram_selects_lexically_related_memory(self):
        related = safe_item("被问候时被称作海潮。", marker="A")
        unrelated = safe_item("最喜欢的颜色是蓝色。", marker="B")
        result = select([unrelated, related], "偏好称呼/问候")
        self.assertEqual(result.selected_count, 1)
        self.assertEqual(result.items[0]["memory_key"], related["memory_key"])

    def test_single_han_character_overlap_is_not_a_match(self):
        result = select([safe_item("甲好")], "偏甲")
        self.assertGreater(result.query_signal_count, 0)
        self.assertEqual(result.items, ())

    def test_containment_rejects_signal_free_contained_memory(self):
        for query, content in (
            ("alpha 甲", "甲"),
            ("alpha!", "!"),
            ("alpha", "a"),
            ("valid signal 乙", "乙"),
        ):
            with self.subTest(query=query, content=content):
                result = select([safe_item(content)], query)
                self.assertEqual(result.items, ())

    def test_containment_accepts_a_contained_side_with_strong_signal(self):
        for query, content in (
            ("alpha", "alpha beta"),
            ("alpha beta", "alpha"),
            ("偏好称呼", "称呼"),
        ):
            with self.subTest(query=query, content=content):
                result = select([safe_item(content)], query)
                self.assertEqual(result.selected_count, 1)

    def _assert_extension_bigram_only_match(
        self,
        first_codepoint: int,
        second_codepoint: int,
    ) -> None:
        first = chr(first_codepoint)
        second = chr(second_codepoint)
        shared_bigram = first + second
        query = "甲" + shared_bigram + "乙"
        content = "丙" + shared_bigram + "丁"
        query_signals = memory_retrieval._text_signals(query)
        content_signals = memory_retrieval._text_signals(content)

        self.assertNotIn(query, content)
        self.assertNotIn(content, query)
        self.assertTrue(
            set(query_signals.alphanumeric).isdisjoint(
                content_signals.alphanumeric
            )
        )
        self.assertEqual(
            set(query_signals.cjk_bigrams).intersection(
                content_signals.cjk_bigrams
            ),
            {shared_bigram},
        )
        result = select([safe_item(content)], query)
        self.assertEqual(result.selected_count, 1)

    def test_extension_i_shared_bigram_matches_different_full_segments(self):
        self._assert_extension_bigram_only_match(0x2EBF0, 0x2EBF1)

    def test_extension_j_shared_bigram_matches_different_full_segments(self):
        self._assert_extension_bigram_only_match(0x323B0, 0x323B1)

    def test_ascii_tokens_are_case_insensitive(self):
        result = select([safe_item("The preferred COLOR is blue.")], "color")
        self.assertEqual(result.selected_count, 1)

    def test_punctuation_differences_do_not_break_token_matches(self):
        result = select(
            [safe_item("Favorite, color: blue.")],
            "What is the favorite-color?",
        )
        self.assertEqual(result.selected_count, 1)

    def test_nfc_line_endings_casefold_and_whitespace_are_normalized(self):
        result = select([safe_item("cafe\u0301 blue")], "CAFÉ\r\n  BLUE")
        self.assertEqual(result.selected_count, 1)

    def test_exact_and_containment_relationships_receive_higher_scores(self):
        containing = safe_item("alpha beta extra", marker="A")
        exact = safe_item("ALPHA BETA", marker="B")
        overlap_only = safe_item("alpha elsewhere", marker="C")
        result = select([containing, overlap_only, exact], "alpha beta")
        self.assertEqual(
            [item["memory_key"] for item in result.items],
            [exact["memory_key"], containing["memory_key"], overlap_only["memory_key"]],
        )

    def test_multiple_candidates_are_sorted_by_fixed_score(self):
        one = safe_item("alpha only", marker="A")
        two = safe_item("alpha beta only", marker="B")
        three = safe_item("alpha beta gamma", marker="C")
        result = select([one, two, three], "alpha beta gamma")
        self.assertEqual(
            [item["memory_key"] for item in result.items],
            [three["memory_key"], two["memory_key"], one["memory_key"]],
        )

    def test_equal_scores_preserve_original_candidate_order(self):
        first = safe_item("alpha first", marker="A")
        second = safe_item("alpha second", marker="B")
        result = select([first, second], "alpha query")
        self.assertEqual(
            [item["memory_key"] for item in result.items],
            [first["memory_key"], second["memory_key"]],
        )

    def test_kind_does_not_add_a_score_bonus(self):
        preference = safe_item("alpha first", kind="user_preference", marker="A")
        decision = safe_item("alpha second", kind="decision", marker="B")
        result = select([decision, preference], "alpha query")
        self.assertEqual(
            [item["memory_key"] for item in result.items],
            [decision["memory_key"], preference["memory_key"]],
        )

    def test_timestamps_are_not_parsed_or_used_for_ranking(self):
        first = safe_item("alpha first", marker="A")
        second = safe_item("alpha second", marker="B")
        first["updated_at"] = "not-a-date-first"
        second["updated_at"] = "0000-later-looking"
        result = select([first, second], "alpha query")
        self.assertEqual(
            [item["memory_key"] for item in result.items],
            [first["memory_key"], second["memory_key"]],
        )

    def test_overlong_high_score_item_is_skipped_for_shorter_later_item(self):
        too_long = safe_item("alpha beta " + "x" * 40, marker="A")
        short = safe_item("alpha", marker="B")
        result = select(
            [too_long, short],
            "alpha beta",
            character_budget=10,
        )
        self.assertEqual(result.items, (short,))

    def test_item_and_character_budgets_are_strict(self):
        items = [
            safe_item("alpha one", marker="A"),
            safe_item("alpha two", marker="B"),
            safe_item("alpha x", marker="C"),
        ]
        by_items = select(items, "alpha", max_items=2, character_budget=100)
        by_chars = select(items, "alpha", max_items=10, character_budget=16)
        self.assertEqual(by_items.selected_count, 2)
        self.assertLessEqual(
            sum(len(item["normalized_content"]) for item in by_chars.items),
            16,
        )
        self.assertEqual(by_chars.selected_count, 2)

    def test_candidate_count_includes_unrelated_candidates(self):
        result = select(
            [safe_item("unrelated memory", marker="A"), safe_item("alpha", marker="B")],
            "alpha",
        )
        self.assertEqual(result.candidate_count, 2)
        self.assertEqual(result.selected_count, 1)

    def test_twenty_candidates_and_eight_thousand_chars_are_allowed(self):
        content = "alpha" + "x" * 3995
        items = [safe_item(content, marker="A"), safe_item(content, marker="B")]
        result = select(items, "alpha", max_items=20, character_budget=8000)
        self.assertEqual(result.selected_count, 2)
        self.assertEqual(
            sum(len(item["normalized_content"]) for item in result.items),
            8000,
        )

    def test_more_than_twenty_candidates_is_rejected(self):
        items = [safe_item("alpha", marker=chr(65 + index)) for index in range(21)]
        with self.assertRaisesRegex(
            memory_retrieval.MemoryRetrievalError,
            r"^invalid_candidates$",
        ):
            select(items, "alpha")

    def test_more_than_eight_thousand_candidate_chars_is_rejected(self):
        items = [
            safe_item("a" * 4001, marker="A"),
            safe_item("b" * 4000, marker="B"),
        ]
        with self.assertRaisesRegex(
            memory_retrieval.MemoryRetrievalError,
            r"^invalid_candidates$",
        ):
            select(items, "alpha")

    def test_query_over_thirty_two_thousand_chars_is_rejected(self):
        with self.assertRaisesRegex(
            memory_retrieval.MemoryRetrievalError,
            r"^invalid_query$",
        ):
            select([], "q" * 32001)

    def test_query_must_be_an_exact_string(self):
        class StringSubclass(str):
            pass

        for query in (StringSubclass("alpha"), 123, None):
            with self.subTest(query_type=type(query).__name__), self.assertRaisesRegex(
                memory_retrieval.MemoryRetrievalError,
                r"^invalid_query$",
            ):
                select([], query)

    def test_query_surrogates_fail_closed_and_data_free(self):
        for surrogate in ("\ud800", "\udfff"):
            marker = "PRIVATE-QUERY-MARKER"
            try:
                select([], marker + surrogate)
            except memory_retrieval.MemoryRetrievalError as error:
                self.assertEqual(error.category, "invalid_query")
                self.assertNotIn(marker, str(error))
                self.assertNotIn(marker, repr(error))
            except UnicodeError as error:
                self.fail(f"native Unicode error escaped: {type(error).__name__}")
            else:
                self.fail("surrogate query was accepted")

    def test_candidate_content_surrogates_fail_closed(self):
        for surrogate in ("\ud800", "\udfff"):
            marker = "PRIVATE-CANDIDATE-MARKER"
            with self.subTest(surrogate=ord(surrogate)), self.assertRaisesRegex(
                memory_retrieval.MemoryRetrievalError,
                r"^invalid_candidates$",
            ) as raised:
                select([safe_item(marker + surrogate)], "alpha")
            self.assertNotIn(marker, str(raised.exception))
            self.assertNotIn(marker, repr(raised.exception))

    def test_sensitive_and_restricted_candidates_are_rejected(self):
        for sensitivity in ("sensitive", "restricted"):
            with self.subTest(sensitivity=sensitivity), self.assertRaisesRegex(
                memory_retrieval.MemoryRetrievalError,
                r"^invalid_candidates$",
            ):
                select([safe_item("alpha", sensitivity=sensitivity)], "alpha")

    def test_forgotten_candidate_is_rejected(self):
        with self.assertRaisesRegex(
            memory_retrieval.MemoryRetrievalError,
            r"^invalid_candidates$",
        ):
            select([safe_item("alpha", status="forgotten")], "alpha")

    def test_superseded_candidate_is_rejected(self):
        with self.assertRaisesRegex(
            memory_retrieval.MemoryRetrievalError,
            r"^invalid_candidates$",
        ):
            select([safe_item("alpha", status="superseded")], "alpha")

    def test_candidate_and_other_inactive_states_are_rejected(self):
        for status in ("candidate", "rejected", "unknown"):
            with self.subTest(status=status), self.assertRaisesRegex(
                memory_retrieval.MemoryRetrievalError,
                r"^invalid_candidates$",
            ):
                select([safe_item("alpha", status=status)], "alpha")

    def test_unknown_kind_and_missing_fields_are_rejected(self):
        unknown = safe_item("alpha", kind="future_kind")
        missing = safe_item("alpha")
        del missing["provenance"]
        for item in (unknown, missing, {"normalized_content": "alpha"}):
            with self.subTest(keys=tuple(item)), self.assertRaisesRegex(
                memory_retrieval.MemoryRetrievalError,
                r"^invalid_candidates$",
            ):
                select([item], "alpha")

    def test_string_subclass_content_is_rejected(self):
        class StringSubclass(str):
            pass

        item = safe_item("alpha")
        item["normalized_content"] = StringSubclass("alpha")
        with self.assertRaisesRegex(
            memory_retrieval.MemoryRetrievalError,
            r"^invalid_candidates$",
        ):
            select([item], "alpha")

    def test_late_invalid_candidate_fails_before_tokenization(self):
        invalid = safe_item("private invalid")
        del invalid["provenance"]
        with mock.patch.object(memory_retrieval, "_text_signals") as signals:
            with self.assertRaisesRegex(
                memory_retrieval.MemoryRetrievalError,
                r"^invalid_candidates$",
            ):
                select([safe_item("alpha"), invalid], "alpha")
        signals.assert_not_called()

    def test_empty_query_does_not_hide_a_late_invalid_candidate(self):
        invalid = safe_item("private invalid")
        del invalid["provenance"]
        with self.assertRaisesRegex(
            memory_retrieval.MemoryRetrievalError,
            r"^invalid_candidates$",
        ):
            select([safe_item("valid"), invalid], "")

    def test_global_user_candidate_requires_empty_scope_ref(self):
        with self.assertRaisesRegex(
            memory_retrieval.MemoryRetrievalError,
            r"^invalid_candidates$",
        ):
            select([safe_item("alpha", scope_ref="private-ref")], "alpha")

    def test_only_global_user_scope_is_supported(self):
        for scope_type in ("channel", "session", "project", "GLOBAL_USER", None):
            with self.subTest(scope_type=scope_type), self.assertRaisesRegex(
                memory_retrieval.MemoryRetrievalError,
                r"^invalid_scope$",
            ):
                memory_retrieval.select_relevant_memory_items(
                    [],
                    query_text="alpha",
                    scope_type=scope_type,
                )

    def test_non_global_candidate_scopes_are_rejected(self):
        for scope_type in ("channel", "session", "project"):
            with self.subTest(scope_type=scope_type), self.assertRaisesRegex(
                memory_retrieval.MemoryRetrievalError,
                r"^invalid_candidates$",
            ):
                select(
                    [safe_item("alpha", scope_type=scope_type, scope_ref="opaque")],
                    "alpha",
                )

    def test_bool_zero_and_over_hard_limit_budgets_are_rejected(self):
        invalid = (
            {"max_items": True},
            {"max_items": 0},
            {"max_items": 21},
            {"character_budget": False},
            {"character_budget": 0},
            {"character_budget": 8001},
        )
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(
                memory_retrieval.MemoryRetrievalError,
                r"^invalid_budget$",
            ):
                select([], "alpha", **kwargs)

    def test_items_must_be_an_exact_list_or_tuple(self):
        private = "PRIVATE-ITERATOR-DETAIL"

        class ExplodingIterator:
            def __iter__(self):
                raise RuntimeError(private)

        for items in (ExplodingIterator(), {"item": safe_item("alpha")}, None):
            try:
                select(items, "alpha")
            except memory_retrieval.MemoryRetrievalError as error:
                self.assertEqual(error.category, "invalid_candidates")
                self.assertNotIn(private, str(error))
                self.assertNotIn(private, repr(error))
            else:
                self.fail("invalid candidate container was accepted")

    def test_dict_subclasses_and_native_mapping_errors_do_not_escape(self):
        private = "PRIVATE-MAPPING-DETAIL"

        class ExplodingDict(dict):
            def __getitem__(self, key):
                raise RuntimeError(private)

        try:
            select([ExplodingDict(safe_item("alpha"))], "alpha")
        except memory_retrieval.MemoryRetrievalError as error:
            self.assertEqual(error.category, "invalid_candidates")
            self.assertNotIn(private, str(error))
            self.assertNotIn(private, repr(error))
        else:
            self.fail("dict subclass was accepted")

    def test_input_list_dicts_and_nested_values_are_not_modified(self):
        items = [safe_item("alpha one", marker="A"), safe_item("alpha two", marker="B")]
        before = copy.deepcopy(items)
        select(items, "alpha")
        self.assertEqual(items, before)

    def test_output_items_are_new_dict_copies(self):
        item = safe_item("alpha")
        result = select([item], "alpha")
        self.assertIsNot(result.items[0], item)
        result.items[0]["normalized_content"] = "changed output"
        self.assertEqual(item["normalized_content"], "alpha")

    def test_raw_safe_item_fields_and_extensions_are_preserved_without_additions(self):
        item = safe_item("alpha")
        item["opaque_extension"] = {"value": "retained"}
        result = select([item], "alpha")
        self.assertEqual(result.items[0], item)
        self.assertEqual(set(result.items[0]), set(item))

    def test_result_repr_contains_counts_only(self):
        query = "PRIVATE-QUERY-REPR"
        item = safe_item("private-query-repr memory", marker="K")
        item["scope_ref"] = ""
        result = select([item], query)
        representation = repr(result)
        for private in (
            query,
            item["normalized_content"],
            item["memory_key"],
            "opaque",
            "score",
            "token",
        ):
            self.assertNotIn(private, representation)
        self.assertIn("candidate_count=1", representation)
        self.assertIn("selected_count=1", representation)

    def test_forged_result_repr_is_data_free(self):
        private = "PRIVATE-FORGED-RESULT"
        forged = memory_retrieval.MemoryRetrievalSelectionV1(
            items=({"plaintext": private},),
            candidate_count=private,
            selected_count=1,
            query_signal_count=1,
        )
        self.assertEqual(repr(forged), "<MemoryRetrievalSelectionV1 invalid>")
        self.assertNotIn(private, repr(forged))

    def test_custom_error_category_is_fixed_data_free_and_immutable(self):
        private = "PRIVATE-CALLER-CATEGORY"
        error = memory_retrieval.MemoryRetrievalError(private)
        self.assertEqual(error.category, "invalid_candidates")
        self.assertEqual(str(error), "invalid_candidates")
        self.assertEqual(
            repr(error),
            "MemoryRetrievalError('invalid_candidates')",
        )
        with self.assertRaises(AttributeError):
            error.category = private
        with self.assertRaises(AttributeError):
            error.args = (private,)
        object.__setattr__(error, "_category_code", private)
        self.assertEqual(error.category, "invalid_candidates")
        self.assertNotIn(private, str(error))
        self.assertNotIn(private, repr(error))

    def test_phase_two_renderer_revalidates_a_legal_selection(self):
        content = "User prefers concise alpha answers."
        result = select([safe_item(content)], "alpha")
        message = memory_context.render_memory_developer_message(
            result.items,
            scope_type="global_user",
            max_items=10,
            character_budget=2000,
        )
        decoded = json.loads(message["content"])
        self.assertEqual(
            decoded["memory_context"]["items"],
            [{"kind": "user_preference", "normalized_content": content}],
        )

    def test_selector_result_is_not_a_renderer_trust_boundary(self):
        result = select([safe_item("alpha")], "alpha")
        result.items[0]["sensitivity"] = "sensitive"
        with self.assertRaisesRegex(
            memory_context.MemoryContextError,
            r"^invalid_item_sensitivity$",
        ):
            memory_context.render_memory_developer_message(
                result.items,
                scope_type="global_user",
            )

    def test_selector_rejects_memory_context_bundles_as_candidates(self):
        bundle = memory_context.build_memory_context_bundle(
            [safe_item("alpha")],
            scope_type="global_user",
        )
        with self.assertRaisesRegex(
            memory_retrieval.MemoryRetrievalError,
            r"^invalid_candidates$",
        ):
            select(bundle, "alpha")

    def test_query_signals_are_deduplicated_in_stable_order(self):
        signals = memory_retrieval._text_signals("alpha alpha 海潮海潮")
        self.assertEqual(signals.alphanumeric, ("alpha", "海潮海潮"))
        self.assertEqual(signals.cjk_bigrams, ("海潮", "潮海"))

    def test_repeated_execution_is_byte_for_byte_stable(self):
        items = [
            safe_item("alpha 海潮 first", marker="A"),
            safe_item("海潮 alpha second", marker="B"),
        ]
        first = select(items, "ALPHA 海潮")
        second = select(items, "ALPHA 海潮")
        encode = lambda result: json.dumps(
            {
                "items": result.items,
                "candidate_count": result.candidate_count,
                "selected_count": result.selected_count,
                "query_signal_count": result.query_signal_count,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(encode(first), encode(second))
        self.assertEqual(repr(first), repr(second))

    def test_mixed_chinese_english_signal_count_and_order_are_stable(self):
        query = "Project 海潮 PROJECT 海潮"
        items = [
            safe_item("海潮 project beta", marker="A"),
            safe_item("project only", marker="B"),
        ]
        first = select(items, query)
        second = select(tuple(items), query)
        self.assertEqual(first.query_signal_count, 3)
        self.assertEqual(first.items, second.items)
        self.assertEqual(first.items[0]["memory_key"], items[0]["memory_key"])


class MemoryRetrievalPurityTests(unittest.TestCase):
    def test_module_imports_only_pure_standard_library_dependencies(self):
        source_path = Path(memory_retrieval.__file__)
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported_roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(
                    alias.name.split(".", 1)[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
        self.assertEqual(
            imported_roots,
            {"__future__", "unicodedata", "dataclasses", "typing"},
        )
        self.assertTrue(imported_roots.isdisjoint({
            "sqlite3",
            "httpx",
            "requests",
            "socket",
            "logging",
            "hashlib",
            "app",
            "kelivo_service",
            "memory_store",
        }))

    def test_module_has_no_print_hash_or_integration_construction_calls(self):
        source_path = Path(memory_retrieval.__file__)
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        called_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertTrue(called_names.isdisjoint({"print", "hash"}))
        source = source_path.read_text(encoding="utf-8")
        for forbidden in (
            "MemoryContextBundleV1",
            "render_memory_developer_message",
            "transient_memory_dispatch",
            "provider_messages",
        ):
            self.assertNotIn(forbidden, source)

    def test_selection_performs_no_database_or_network_calls(self):
        with (
            mock.patch.object(sqlite3, "connect") as database_connect,
            mock.patch.object(socket, "create_connection") as network_connect,
        ):
            result = select([safe_item("alpha")], "alpha")
        self.assertEqual(result.selected_count, 1)
        database_connect.assert_not_called()
        network_connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
