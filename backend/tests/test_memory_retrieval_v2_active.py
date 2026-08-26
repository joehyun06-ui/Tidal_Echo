from __future__ import annotations

import ast
import contextlib
import dataclasses
import io
import json
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import (
    channel_store,
    deployment_config,
    kelivo_service,
    memory_context_integration,
    memory_retrieval_v2,
    memory_retrieval_v2_active,
)
from backend.tests._support import NoNetworkMixin, load_app, request


REQUIRED_BASE_SHA = "2fd0c1271cf1dc731d2b3ccdf5a1ebc1de5d285e"
FROZEN_BLOBS = {
    "backend/memory_retrieval.py": "f20f854c9dd16611c56e1f1915ef835617226381",
    "backend/memory_retrieval_v2.py": "230df94888277d9b78b608dfea849b21b213ac44",
    "backend/memory_retrieval_v2_shadow.py": (
        "7357a1cffcc736e222e905af5327cf51b05b1f2d"
    ),
}


def safe_item(
    content: str,
    *,
    marker: str = "A",
    provenance: list | None = None,
) -> dict:
    return {
        "memory_key": marker * 32,
        "kind": "user_preference",
        "scope_type": "global_user",
        "scope_ref": "",
        "normalized_content": content,
        "fingerprint_version": 1,
        "status": "active",
        "explicitness": "explicit",
        "confidence": 1.0,
        "sensitivity": "normal",
        "first_observed_at": "2026-01-01T00:00:00Z",
        "last_confirmed_at": "2026-01-02T00:00:00Z",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "provenance": [] if provenance is None else provenance,
    }


def v2_plan(
    candidates: tuple[dict, ...],
    indexes: tuple[int, ...],
    *,
    modes: tuple[str, ...] | None = None,
    eligible_count: int | None = None,
    query_signal_count: int = 1,
) -> memory_retrieval_v2.MemoryRetrievalPlanV2:
    selected_modes = modes or tuple("direct" for _ in indexes)
    items = tuple(
        memory_retrieval_v2.MemoryRecallItemV2(
            candidates[index],
            selected_modes[position],
        )
        for position, index in enumerate(indexes)
    )
    return memory_retrieval_v2.MemoryRetrievalPlanV2(
        items=items,
        candidate_count=len(candidates),
        eligible_count=(
            len(indexes) if eligible_count is None else eligible_count
        ),
        selected_count=len(items),
        query_signal_count=query_signal_count,
        total_chars=sum(
            len(candidates[index]["normalized_content"])
            for index in indexes
        ),
        direct_count=selected_modes.count("direct"),
        cautious_count=selected_modes.count("cautious"),
        associate_only_count=selected_modes.count("associate_only"),
    )


def planned_selection(
    candidates: tuple[dict, ...],
    indexes: tuple[int, ...],
    *,
    modes: tuple[str, ...] | None = None,
) -> memory_retrieval_v2_active.MemoryRetrievalV2ActiveSelection:
    plan = v2_plan(candidates, indexes, modes=modes)
    with mock.patch.object(
        memory_retrieval_v2_active.memory_retrieval_v2,
        "plan_memory_recall_v2",
        return_value=plan,
    ):
        return memory_retrieval_v2_active.plan_memory_recall_v2_active(
            candidates,
            query_text="current query",
        )


class HostileValue:
    def __init__(self, private: str):
        self.private = private
        self.repr_called = False
        self.deepcopy_called = False

    def __repr__(self):
        self.repr_called = True
        raise RuntimeError(self.private)

    def __deepcopy__(self, _memo):
        self.deepcopy_called = True
        raise RuntimeError(self.private)


class FakeReadService:
    def __init__(self, items=None, error: BaseException | None = None):
        self.items = [] if items is None else items
        self.error = error
        self.calls: list[dict] = []

    def get_active_memories(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.items


class _TelegramDisabled:
    requested = False


class ActiveConfigurationTests(unittest.TestCase):
    def load(self, values: dict[str, str] | None = None):
        return deployment_config.load_deployment_config(
            _TelegramDisabled(),
            environ=values or {},
        ).memory

    @staticmethod
    def enabled_values() -> dict[str, str]:
        return {
            "MEMORY_CORE_ENABLED": "true",
            "MEMORY_CONTEXT_INJECTION_ENABLED": "true",
            "MEMORY_SMART_RETRIEVAL_ENABLED": "true",
            "MEMORY_RETRIEVAL_V2_ACTIVE_ENABLED": "true",
            "KELIVO_ENABLED": "true",
            "KELIVO_API_KEY": "test-kelivo-key-distinct-1234567890",
            "KELIVO_CLIENT_ID": "primary-kelivo",
            "KELIVO_API_SESSION": "shared-test-session",
            "KELIVO_MODEL_ALIAS": "ouou-home",
            "LLM_MODEL": "test-provider-model",
        }

    def test_active_flag_defaults_false(self):
        memory = self.load()
        self.assertFalse(memory.retrieval_v2_active_enabled)
        self.assertFalse(memory.retrieval_v2_shadow_enabled)

    def test_active_flag_uses_strict_bool_validation(self):
        for value in ("", "maybe", " true ", "treu"):
            with self.subTest(value=value), self.assertRaisesRegex(
                deployment_config.DeploymentConfigError,
                r"^invalid_memory_retrieval_v2_active_enabled$",
            ):
                self.load({"MEMORY_RETRIEVAL_V2_ACTIVE_ENABLED": value})

    def test_active_requires_smart_retrieval(self):
        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            r"^memory_retrieval_v2_active_requires_smart_retrieval$",
        ):
            self.load({"MEMORY_RETRIEVAL_V2_ACTIVE_ENABLED": "true"})

    def test_active_conflicts_with_shadow(self):
        values = self.enabled_values()
        values["MEMORY_RETRIEVAL_V2_SHADOW_ENABLED"] = "true"
        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            r"^memory_retrieval_v2_active_conflicts_with_shadow$",
        ):
            self.load(values)

    def test_valid_active_configuration_adds_no_write_authority(self):
        memory = self.load(self.enabled_values())
        self.assertTrue(memory.retrieval_v2_active_enabled)
        self.assertFalse(memory.retrieval_v2_shadow_enabled)
        self.assertFalse(memory.explicit_writes_enabled)
        self.assertFalse(memory.auto_candidate_persistence_enabled)


class ActiveBoundaryTests(unittest.TestCase):
    def test_selection_order_modes_and_report_are_exact(self):
        candidates = tuple(
            safe_item(f"candidate-{index}", marker=chr(65 + index))
            for index in range(3)
        )
        selection = planned_selection(
            candidates,
            (2, 0, 1),
            modes=("direct", "cautious", "associate_only"),
        )
        exported = memory_retrieval_v2_active.validated_active_selection_items(
            selection
        )
        self.assertEqual(
            [candidate["normalized_content"] for candidate, _mode in exported],
            ["candidate-2", "candidate-0", "candidate-1"],
        )
        self.assertEqual(
            [mode for _candidate, mode in exported],
            ["direct", "cautious", "associate_only"],
        )
        report = memory_retrieval_v2_active.active_report_from_selection(selection)
        self.assertEqual(
            memory_retrieval_v2_active
            .render_memory_retrieval_v2_active_telemetry(report),
            "[memory-retrieval-v2-active] status=completed candidates=3 "
            "eligible=3 selected=3 chars=33 direct=1 cautious=1 "
            "associate_only=1",
        )

    def test_caller_and_returned_nested_mutation_cannot_change_selection(self):
        provenance = [{"source": {"channel": "safe"}}]
        candidate = safe_item("immutable content", provenance=provenance)
        selection = planned_selection((candidate,), (0,))
        before = memory_retrieval_v2_active.validated_active_selection_items(
            selection
        )
        candidate["normalized_content"] = "CALLER-MUTATED"
        provenance[0]["source"]["channel"] = "CALLER-MUTATED"
        returned = selection.items[0].candidate
        returned["normalized_content"] = "RETURNED-MUTATED"
        returned["provenance"][0]["source"]["channel"] = "RETURNED-MUTATED"
        after = memory_retrieval_v2_active.validated_active_selection_items(selection)
        self.assertEqual(before, after)
        self.assertEqual(after[0][0]["normalized_content"], "immutable content")
        self.assertEqual(
            after[0][0]["provenance"][0]["source"]["channel"],
            "safe",
        )

    def test_malicious_planner_mutation_cannot_escape_or_invent_plaintext(self):
        private = "PRIVATE-INVENTED-BY-V2"
        provenance = [{"source": {"channel": "safe"}}]
        candidate = safe_item("original content", provenance=provenance)

        def malicious(candidates, **_kwargs):
            candidates[0]["normalized_content"] = private
            candidates[0]["provenance"][0]["source"]["channel"] = private
            return v2_plan(candidates, (0,))

        with mock.patch.object(
            memory_retrieval_v2_active.memory_retrieval_v2,
            "plan_memory_recall_v2",
            side_effect=malicious,
        ), self.assertRaisesRegex(
            memory_retrieval_v2_active.MemoryRetrievalV2ActiveError,
            r"^memory_retrieval_v2_active_unavailable$",
        ) as raised:
            memory_retrieval_v2_active.plan_memory_recall_v2_active(
                (candidate,),
                query_text="current query",
            )
        self.assertEqual(candidate["normalized_content"], "original content")
        self.assertEqual(provenance[0]["source"]["channel"], "safe")
        self.assertNotIn(private, repr(raised.exception))

    def test_forged_candidate_is_rejected(self):
        candidate = safe_item("original", marker="A")
        forged = safe_item("forged private", marker="Z")
        plan = v2_plan((forged,), (0,))
        with mock.patch.object(
            memory_retrieval_v2_active.memory_retrieval_v2,
            "plan_memory_recall_v2",
            return_value=plan,
        ), self.assertRaisesRegex(
            memory_retrieval_v2_active.MemoryRetrievalV2ActiveError,
            r"^memory_retrieval_v2_active_unavailable$",
        ):
            memory_retrieval_v2_active.plan_memory_recall_v2_active(
                (candidate,),
                query_text="current query",
            )

    def test_duplicate_candidate_reuse_is_rejected(self):
        first = safe_item("first", marker="A")
        second = safe_item("second", marker="B")
        reused = memory_retrieval_v2.MemoryRecallItemV2(first, "direct")
        plan = memory_retrieval_v2.MemoryRetrievalPlanV2(
            items=(reused, reused),
            candidate_count=2,
            eligible_count=2,
            selected_count=2,
            query_signal_count=1,
            total_chars=len("first") * 2,
            direct_count=2,
            cautious_count=0,
            associate_only_count=0,
        )
        with mock.patch.object(
            memory_retrieval_v2_active.memory_retrieval_v2,
            "plan_memory_recall_v2",
            return_value=plan,
        ), self.assertRaises(memory_retrieval_v2_active.MemoryRetrievalV2ActiveError):
            memory_retrieval_v2_active.plan_memory_recall_v2_active(
                (first, second),
                query_text="current query",
            )

    def test_forged_structural_counts_are_rejected(self):
        candidate = safe_item("candidate")
        fields = (
            ("candidate_count", 2),
            ("eligible_count", 2),
            ("selected_count", 0),
            ("total_chars", 999),
            ("direct_count", 0),
        )
        for field, value in fields:
            plan = v2_plan((candidate,), (0,))
            object.__setattr__(plan, field, value)
            with self.subTest(field=field), mock.patch.object(
                memory_retrieval_v2_active.memory_retrieval_v2,
                "plan_memory_recall_v2",
                return_value=plan,
            ), self.assertRaises(
                memory_retrieval_v2_active.MemoryRetrievalV2ActiveError
            ):
                memory_retrieval_v2_active.plan_memory_recall_v2_active(
                    (candidate,),
                    query_text="current query",
                )

    def test_zero_signal_requires_empty_eligible_and_selected_counts(self):
        candidate = safe_item("candidate")
        plans = (
            v2_plan(
                (candidate,),
                (0,),
                eligible_count=1,
                query_signal_count=0,
            ),
            v2_plan(
                (candidate,),
                (),
                eligible_count=1,
                query_signal_count=0,
            ),
        )
        for plan in plans:
            with mock.patch.object(
                memory_retrieval_v2_active.memory_retrieval_v2,
                "plan_memory_recall_v2",
                return_value=plan,
            ), self.assertRaises(
                memory_retrieval_v2_active.MemoryRetrievalV2ActiveError
            ):
                memory_retrieval_v2_active.plan_memory_recall_v2_active(
                    (candidate,),
                    query_text="current query",
                )
        empty = planned_selection((), ())
        self.assertEqual((empty.eligible_count, empty.selected_count), (0, 0))

    def test_forged_recall_use_is_rejected(self):
        candidate = safe_item("candidate")
        plan = v2_plan((candidate,), (0,))
        object.__setattr__(plan.items[0], "recall_use", "forged")
        with mock.patch.object(
            memory_retrieval_v2_active.memory_retrieval_v2,
            "plan_memory_recall_v2",
            return_value=plan,
        ), self.assertRaises(memory_retrieval_v2_active.MemoryRetrievalV2ActiveError):
            memory_retrieval_v2_active.plan_memory_recall_v2_active(
                (candidate,),
                query_text="current query",
            )

    def test_more_than_ten_and_more_than_2000_chars_are_rejected(self):
        many = tuple(
            safe_item(f"item-{index}", marker=chr(65 + index))
            for index in range(11)
        )
        many_plan = v2_plan(many, tuple(range(11)))
        long_candidate = safe_item("x" * 2001)
        long_plan = v2_plan((long_candidate,), (0,))
        for candidates, plan in ((many, many_plan), ((long_candidate,), long_plan)):
            with mock.patch.object(
                memory_retrieval_v2_active.memory_retrieval_v2,
                "plan_memory_recall_v2",
                return_value=plan,
            ), self.assertRaises(
                memory_retrieval_v2_active.MemoryRetrievalV2ActiveError
            ):
                memory_retrieval_v2_active.plan_memory_recall_v2_active(
                    candidates,
                    query_text="current query",
                )

    def test_hostile_nested_values_never_execute_hooks_or_leak(self):
        private = "PRIVATE-HOSTILE-ACTIVE"
        hostile = HostileValue(private)
        candidate = safe_item("candidate", provenance=[hostile])
        with self.assertRaisesRegex(
            memory_retrieval_v2_active.MemoryRetrievalV2ActiveError,
            r"^memory_retrieval_v2_active_unavailable$",
        ) as raised:
            memory_retrieval_v2_active.plan_memory_recall_v2_active(
                (candidate,),
                query_text="current query",
            )
        self.assertFalse(hostile.repr_called)
        self.assertFalse(hostile.deepcopy_called)
        self.assertNotIn(private, str(raised.exception))
        self.assertNotIn(private, repr(raised.exception))

    def test_planner_exception_and_invalid_type_are_fixed_data_free(self):
        private = "PRIVATE-PLANNER-EXCEPTION"
        candidate = safe_item("candidate")
        for side_effect, return_value in (
            (RuntimeError(private), mock.DEFAULT),
            (None, object()),
        ):
            with mock.patch.object(
                memory_retrieval_v2_active.memory_retrieval_v2,
                "plan_memory_recall_v2",
                side_effect=side_effect,
                return_value=return_value,
            ), self.assertRaisesRegex(
                memory_retrieval_v2_active.MemoryRetrievalV2ActiveError,
                r"^memory_retrieval_v2_active_unavailable$",
            ) as raised:
                memory_retrieval_v2_active.plan_memory_recall_v2_active(
                    (candidate,),
                    query_text="current query",
                )
            self.assertNotIn(private, repr(raised.exception))

    def test_selection_and_report_repr_are_data_free_and_tamper_safe(self):
        private = "PRIVATE-ACTIVE-REPR"
        selection = planned_selection((safe_item(private),), (0,))
        report = memory_retrieval_v2_active.active_report_from_selection(selection)
        for value in (selection, report, *selection.items):
            self.assertNotIn(private, repr(value))
            self.assertNotIn("memory_key", repr(value))
        object.__setattr__(report, "candidate_count", HostileValue(private))
        self.assertEqual(repr(report), "<MemoryRetrievalV2ActiveReport invalid>")
        self.assertIsNone(
            memory_retrieval_v2_active
            .render_memory_retrieval_v2_active_telemetry(report)
        )


class ActiveDispatchTests(unittest.TestCase):
    def setUp(self):
        self.base = (
            {"role": "system", "content": "persona"},
            {"role": "assistant", "content": "prior"},
            {"role": "user", "content": "alpha current question"},
        )
        self.item = safe_item("alpha current question remembered preference")

    def dispatch(self, service=None, **kwargs):
        return memory_context_integration.prepare_transient_memory_dispatch(
            service or FakeReadService([self.item]),
            self.base,
            enabled=True,
            smart_retrieval_enabled=True,
            retrieval_v2_active_enabled=True,
            **kwargs,
        )

    def test_active_reads_once_uses_final_query_and_skips_v1_and_shadow(self):
        service = FakeReadService([self.item])
        seen = {}
        real = (
            memory_context_integration.memory_retrieval_v2_active
            .memory_retrieval_v2.plan_memory_recall_v2
        )

        def capture(candidates, **kwargs):
            seen["candidates"] = candidates
            seen.update(kwargs)
            return real(candidates, **kwargs)

        with (
            mock.patch.object(
                memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
            ) as v1,
            mock.patch.object(
                memory_context_integration.memory_retrieval_v2_shadow,
                "compare_memory_retrieval_v2_shadow",
            ) as shadow,
            mock.patch.object(
                memory_context_integration.memory_retrieval_v2_active
                .memory_retrieval_v2,
                "plan_memory_recall_v2",
                side_effect=capture,
            ),
        ):
            result = self.dispatch(service)
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(service.calls[0], {
            "scope_type": "global_user",
            "scope_ref": "",
            "limit": 20,
            "character_budget": 8000,
            "include_sensitive": False,
        })
        self.assertEqual(seen["query_text"], self.base[-1]["content"])
        self.assertEqual(seen["scope_type"], "global_user")
        self.assertEqual((seen["max_items"], seen["character_budget"]), (10, 2000))
        v1.assert_not_called()
        shadow.assert_not_called()
        self.assertTrue(result.memory_applied)
        decoded = json.loads(result.provider_messages[-2]["content"])
        self.assertEqual(decoded["memory_context"]["version"], "memory_context/v2")
        self.assertEqual(result.provider_messages[-1], self.base[-1])
        self.assertIsNotNone(result.retrieval_v2_active_report)
        self.assertIsNone(result.retrieval_v2_shadow_report)

    def test_active_empty_plan_has_report_but_no_memory_message(self):
        base = (*self.base[:-1], {"role": "user", "content": "what are you and how"})
        result = memory_context_integration.prepare_transient_memory_dispatch(
            FakeReadService([self.item]),
            base,
            enabled=True,
            smart_retrieval_enabled=True,
            retrieval_v2_active_enabled=True,
        )
        self.assertIs(result.provider_messages, base)
        self.assertFalse(result.memory_applied)
        self.assertEqual(result.retrieval_v2_active_report.selected_count, 0)

    def test_explicit_active_false_is_byte_exact_v1_and_shadow_unchanged(self):
        default = memory_context_integration.prepare_transient_memory_dispatch(
            FakeReadService([self.item]),
            self.base,
            enabled=True,
            smart_retrieval_enabled=True,
        )
        explicit = memory_context_integration.prepare_transient_memory_dispatch(
            FakeReadService([self.item]),
            self.base,
            enabled=True,
            smart_retrieval_enabled=True,
            retrieval_v2_active_enabled=False,
        )
        self.assertEqual(
            json.dumps(default.provider_messages, ensure_ascii=False).encode(),
            json.dumps(explicit.provider_messages, ensure_ascii=False).encode(),
        )
        with mock.patch.object(
            memory_context_integration.memory_retrieval_v2_active,
            "plan_memory_recall_v2_active",
        ) as active:
            shadow = memory_context_integration.prepare_transient_memory_dispatch(
                FakeReadService([self.item]),
                self.base,
                enabled=True,
                smart_retrieval_enabled=True,
                retrieval_v2_shadow_enabled=True,
                retrieval_v2_active_enabled=False,
            )
        active.assert_not_called()
        self.assertIsNotNone(shadow.retrieval_v2_shadow_report)

    def test_active_relationships_and_exact_bool_fail_before_read(self):
        service = FakeReadService(error=AssertionError("must not read"))
        cases = (
            {"retrieval_v2_active_enabled": "true", "smart_retrieval_enabled": True},
            {"retrieval_v2_active_enabled": True, "smart_retrieval_enabled": False},
            {
                "retrieval_v2_active_enabled": True,
                "retrieval_v2_shadow_enabled": True,
                "smart_retrieval_enabled": True,
            },
        )
        for changes in cases:
            values = {
                "enabled": True,
                "smart_retrieval_enabled": changes.pop("smart_retrieval_enabled"),
                "retrieval_v2_active_enabled": changes.pop(
                    "retrieval_v2_active_enabled"
                ),
                **changes,
            }
            with self.subTest(values=values), self.assertRaises(
                memory_context_integration.MemoryContextIntegrationError
            ):
                memory_context_integration.prepare_transient_memory_dispatch(
                    service,
                    self.base,
                    **values,
                )
        self.assertEqual(service.calls, [])

    def test_active_failures_never_fall_back_to_v1_or_render_provider_context(self):
        private = "PRIVATE-ACTIVE-FAILURE"
        with (
            mock.patch.object(
                memory_context_integration.memory_retrieval_v2_active,
                "plan_memory_recall_v2_active",
                side_effect=RuntimeError(private),
            ),
            mock.patch.object(
                memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
            ) as v1,
            mock.patch.object(
                memory_context_integration.memory_context_v2,
                "render_memory_developer_message_v2",
            ) as renderer,
            self.assertRaisesRegex(
                memory_context_integration.MemoryContextIntegrationError,
                r"^memory_context_unavailable$",
            ) as raised,
        ):
            self.dispatch()
        v1.assert_not_called()
        renderer.assert_not_called()
        self.assertNotIn(private, repr(raised.exception))

    def test_active_validation_failure_collapses_to_context_unavailable(self):
        forged = v2_plan((self.item,), (0,))
        object.__setattr__(forged, "eligible_count", 2)
        with (
            mock.patch.object(
                memory_context_integration.memory_retrieval_v2_active
                .memory_retrieval_v2,
                "plan_memory_recall_v2",
                return_value=forged,
            ),
            mock.patch.object(
                memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
            ) as v1,
            self.assertRaisesRegex(
                memory_context_integration.MemoryContextIntegrationError,
                r"^memory_context_unavailable$",
            ),
        ):
            self.dispatch()
        v1.assert_not_called()

    def test_context_v2_renderer_failure_has_no_v1_fallback(self):
        private = "PRIVATE-CONTEXT-V2-FAILURE"
        with (
            mock.patch.object(
                memory_context_integration.memory_context_v2,
                "render_memory_developer_message_v2",
                side_effect=RuntimeError(private),
            ),
            mock.patch.object(
                memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
            ) as v1,
            self.assertRaisesRegex(
                memory_context_integration.MemoryContextIntegrationError,
                r"^memory_context_unavailable$",
            ) as raised,
        ):
            self.dispatch()
        v1.assert_not_called()
        self.assertNotIn(private, repr(raised.exception))

    def test_active_report_is_repr_hidden_and_not_in_provider_messages(self):
        result = self.dispatch()
        rendered = repr(result)
        encoded = json.dumps(result.provider_messages, ensure_ascii=False)
        self.assertEqual(rendered, "<TransientMemoryDispatch memory_applied=True>")
        self.assertNotIn("retrieval_v2_active_report", rendered)
        self.assertNotIn("candidate_count", encoded)
        self.assertNotIn("memory-retrieval-v2-active", encoded)


class ActiveSourceBoundaryTests(unittest.TestCase):
    def test_active_module_is_pure(self):
        source = Path(memory_retrieval_v2_active.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        self.assertTrue(imports.issubset({
            "__future__",
            "math",
            "dataclasses",
            "typing",
            "memory_retrieval_v2",
        }))
        for forbidden in (
            "sqlite3",
            "socket",
            "httpx",
            "requests",
            "open(",
            "os.environ",
            "datetime",
            "random",
            "print(",
            "deepcopy",
        ):
            self.assertNotIn(forbidden, source)
        with (
            mock.patch.object(sqlite3, "connect") as database,
            mock.patch.object(socket, "create_connection") as network,
        ):
            planned_selection((), ())
        database.assert_not_called()
        network.assert_not_called()

    def test_frozen_retrieval_blobs_are_unchanged(self):
        repo_root = Path(__file__).resolve().parents[2]
        for path, expected in FROZEN_BLOBS.items():
            with self.subTest(path=path):
                actual = subprocess.check_output(
                    ["git", "rev-parse", f":{path}"],
                    cwd=repo_root,
                    text=True,
                    encoding="utf-8",
                ).strip()
                self.assertEqual(actual, expected)

    def test_no_migration_011_and_schema_maximum_is_010(self):
        self.assertEqual(max(version for version, _name, _apply in channel_store.MIGRATIONS), 10)
        repo_root = Path(__file__).resolve().parents[2]
        self.assertEqual(tuple(repo_root.glob("backend/**/*011*")), ())

    def test_source_baselines_pin_active_and_shadow_false(self):
        repo_root = Path(__file__).resolve().parents[2]
        blueprint = json.loads((repo_root / "render.yaml").read_text(encoding="utf-8"))
        env = {
            item["key"]: item
            for item in blueprint["services"][0]["envVars"]
        }
        self.assertEqual(env["MEMORY_RETRIEVAL_V2_ACTIVE_ENABLED"]["value"], "false")
        self.assertEqual(env["MEMORY_RETRIEVAL_V2_SHADOW_ENABLED"]["value"], "false")
        workflow = (repo_root / ".github/workflows/python-ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'env["MEMORY_RETRIEVAL_V2_ACTIVE_ENABLED"].get("value") == "false"',
            workflow,
        )

    def test_active_report_does_not_enter_frozen_request_contracts(self):
        for contract in (
            kelivo_service.PreparedRequest,
            kelivo_service.FrozenRequestContract,
        ):
            names = {field.name for field in dataclasses.fields(contract)}
            self.assertTrue(names.isdisjoint({
                "retrieval_v2_active_report",
                "recall_use",
                "retrieval_v2_active",
            }))


class ActiveAppIntegrationTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(
            self.temp.name,
            kelivo=True,
            auto_idempotency=True,
            operit_share=True,
            memory=True,
            memory_context=True,
            memory_smart=True,
        )
        self.module.DEPLOYMENT = dataclasses.replace(
            self.module.DEPLOYMENT,
            memory=dataclasses.replace(
                self.module.DEPLOYMENT.memory,
                retrieval_v2_shadow_enabled=False,
                retrieval_v2_active_enabled=True,
            ),
        )
        self.item = safe_item("alpha current question remembered preference")
        self.service = FakeReadService([self.item])
        self.module.MEMORY_SERVICE = self.service
        self.provider_calls = []

        async def generate(
            messages,
            api_session,
            provider_model,
            temperature,
            max_tokens,
            context,
        ):
            self.provider_calls.append((messages, dict(context)))
            return {"text": "model reply", "usage": {}}

        self.module.KELIVO_GENERATOR = generate

    @staticmethod
    def payload(text: str = "alpha current question") -> dict:
        return {
            "model": "ouou-home",
            "messages": [{"role": "user", "content": text}],
            "stream": False,
        }

    async def post(self, key: str, text: str = "alpha current question"):
        return await request(
            self.module,
            "POST",
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer test-kelivo-key-distinct-1234567890",
                "Idempotency-Key": key,
            },
            json=self.payload(text),
        )

    async def test_active_logs_bounded_success_and_dispatches_context_v2(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            response = await self.post("active-log-key-0001")
        self.assertEqual(response.status_code, 200)
        lines = [
            line for line in output.getvalue().splitlines()
            if line.startswith("[memory-retrieval-v2-active]")
        ]
        self.assertEqual(len(lines), 1)
        self.assertIn("status=completed candidates=1 eligible=1 selected=1", lines[0])
        self.assertNotIn("remembered preference", lines[0])
        self.assertNotIn("memory_key", lines[0])
        messages, context = self.provider_calls[0]
        decoded = json.loads(messages[-2]["content"])
        self.assertEqual(decoded["memory_context"]["version"], "memory_context/v2")
        self.assertEqual(
            context["transient_memory_dispatch"],
            "kelivo-transient-memory-dispatch-v1",
        )
        self.assertTrue(all("retrieval_v2" not in key for key in context))

    async def test_active_failure_returns_503_without_provider_or_v1_fallback(self):
        private = "PRIVATE-ACTIVE-APP-FAILURE"
        output = io.StringIO()
        with (
            mock.patch.object(
                self.module.memory_context_integration.memory_retrieval_v2_active,
                "plan_memory_recall_v2_active",
                side_effect=RuntimeError(private),
            ),
            mock.patch.object(
                self.module.memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
            ) as v1,
            contextlib.redirect_stdout(output),
        ):
            response = await self.post("active-failure-key-0001")
        self.assertEqual(
            (response.status_code, response.json()["error"]["code"]),
            (503, "memory_context_unavailable"),
        )
        self.assertEqual(self.provider_calls, [])
        v1.assert_not_called()
        self.assertNotIn(private, output.getvalue())
        self.assertNotIn("[memory-retrieval-v2-active]", output.getvalue())

    async def test_replay_and_operit_perform_no_additional_active_planning(self):
        real = (
            self.module.memory_context_integration.memory_retrieval_v2_active
            .plan_memory_recall_v2_active
        )
        with mock.patch.object(
            self.module.memory_context_integration.memory_retrieval_v2_active,
            "plan_memory_recall_v2_active",
            wraps=real,
        ) as planner:
            first = await self.post("active-replay-key-0001")
            replay = await self.post("active-replay-key-0001")
            operit = await request(
                self.module,
                "POST",
                "/v1/operit/share",
                headers={
                    "Authorization": (
                        "Bearer test-operit-share-key-distinct-1234567890"
                    )
                },
                json=self.payload("operit alpha"),
            )
        self.assertEqual(
            (first.status_code, replay.status_code, operit.status_code),
            (200, 200, 200),
        )
        self.assertEqual(first.json(), replay.json())
        self.assertEqual(planner.call_count, 1)
        self.assertEqual(len(self.service.calls), 1)

    async def test_disabled_active_has_no_dynamic_call_or_log(self):
        self.module.DEPLOYMENT = dataclasses.replace(
            self.module.DEPLOYMENT,
            memory=dataclasses.replace(
                self.module.DEPLOYMENT.memory,
                retrieval_v2_active_enabled=False,
            ),
        )
        output = io.StringIO()
        with (
            mock.patch.object(
                self.module.memory_context_integration.memory_retrieval_v2_active,
                "plan_memory_recall_v2_active",
            ) as active,
            contextlib.redirect_stdout(output),
        ):
            response = await self.post("active-disabled-key-0001")
        self.assertEqual(response.status_code, 200)
        active.assert_not_called()
        self.assertNotIn("[memory-retrieval-v2-active]", output.getvalue())

    async def test_active_report_does_not_change_frozen_request_identity(self):
        active = await self.post("active-identity-on-0001")
        self.module.DEPLOYMENT = dataclasses.replace(
            self.module.DEPLOYMENT,
            memory=dataclasses.replace(
                self.module.DEPLOYMENT.memory,
                retrieval_v2_active_enabled=False,
            ),
        )
        v1 = await self.post("active-identity-off-0001")
        self.assertEqual((active.status_code, v1.status_code), (200, 200))
        with self.module.db() as conn:
            rows = conn.execute(
                """SELECT request_payload_hash,request_identity_hash,
                          provider_messages_json,context_bundle_json,
                          context_bundle_hash,prompt_contract_version
                     FROM kelivo_requests ORDER BY id"""
            ).fetchall()
        for name in rows[0].keys():
            self.assertEqual(rows[0][name], rows[1][name], name)


if __name__ == "__main__":
    unittest.main()
