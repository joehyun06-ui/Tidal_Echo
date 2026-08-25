from __future__ import annotations

import ast
import asyncio
import contextlib
import dataclasses
import io
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import (
    channel_store,
    deployment_config,
    memory_context_integration,
    memory_retrieval,
    memory_retrieval_v2,
    memory_retrieval_v2_shadow,
)
from backend.tests._support import NoNetworkMixin, load_app, request


REQUIRED_BASE_SHA = "a0255abe3db611f6626a9d75f2959c656c210f72"
V1_BLOB_SHA = "f20f854c9dd16611c56e1f1915ef835617226381"
V2_BLOB_SHA = "230df94888277d9b78b608dfea849b21b213ac44"


def safe_item(
    content: str,
    *,
    marker: str = "A",
    explicitness: str = "explicit",
    confidence: float = 1.0,
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
        "explicitness": explicitness,
        "confidence": confidence,
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


def relation_report(
    v1_indexes: tuple[int, ...],
    v2_indexes: tuple[int, ...],
    *,
    modes: tuple[str, ...] | None = None,
) -> memory_retrieval_v2_shadow.MemoryRetrievalV2ShadowReport:
    candidates = tuple(
        safe_item(f"candidate-{index}", marker=chr(65 + index))
        for index in range(5)
    )
    selected = tuple(candidates[index] for index in v1_indexes)
    planned = v2_plan(candidates, v2_indexes, modes=modes)
    with mock.patch.object(
        memory_retrieval_v2_shadow.memory_retrieval_v2,
        "plan_memory_recall_v2",
        return_value=planned,
    ):
        return memory_retrieval_v2_shadow.compare_memory_retrieval_v2_shadow(
            candidates,
            selected,
            query_text="current query",
        )


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

    def readiness(self):
        return True, ""


class _TelegramDisabled:
    requested = False


class ShadowConfigurationTests(unittest.TestCase):
    def load(self, values: dict[str, str] | None = None):
        return deployment_config.load_deployment_config(
            _TelegramDisabled(),
            environ=values or {},
        ).memory

    def test_flag_defaults_false(self):
        self.assertFalse(self.load().retrieval_v2_shadow_enabled)

    def test_flag_strict_bool_typo_is_rejected(self):
        for value in ("", "maybe", " true ", "treu"):
            with self.subTest(value=value), self.assertRaisesRegex(
                deployment_config.DeploymentConfigError,
                r"^invalid_memory_retrieval_v2_shadow_enabled$",
            ):
                self.load({"MEMORY_RETRIEVAL_V2_SHADOW_ENABLED": value})

    def test_shadow_requires_smart_retrieval(self):
        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            r"^memory_retrieval_v2_shadow_requires_smart_retrieval$",
        ):
            self.load({"MEMORY_RETRIEVAL_V2_SHADOW_ENABLED": "true"})

    def test_enabled_shadow_needs_no_new_write_authority(self):
        config = self.load({
            "MEMORY_RETRIEVAL_V2_SHADOW_ENABLED": "true",
            "MEMORY_SMART_RETRIEVAL_ENABLED": "true",
            "MEMORY_CONTEXT_INJECTION_ENABLED": "true",
            "MEMORY_CORE_ENABLED": "true",
            "KELIVO_ENABLED": "true",
            "KELIVO_API_KEY": "test-kelivo-key-distinct-1234567890",
            "KELIVO_CLIENT_ID": "primary-kelivo",
            "KELIVO_API_SESSION": "shared-test-session",
            "KELIVO_MODEL_ALIAS": "ouou-home",
            "LLM_MODEL": "test-provider-model",
        })
        self.assertTrue(config.retrieval_v2_shadow_enabled)
        self.assertTrue(config.smart_retrieval_enabled)
        self.assertFalse(config.explicit_writes_enabled)
        self.assertFalse(config.auto_candidate_persistence_enabled)


class ShadowReportTests(unittest.TestCase):
    def test_both_empty_relation(self):
        report = relation_report((), ())
        self.assertEqual(report.relation, "both_empty")

    def test_identical_relation(self):
        report = relation_report((0, 1), (0, 1))
        self.assertEqual(report.relation, "identical")

    def test_reordered_relation(self):
        report = relation_report((0, 1), (1, 0))
        self.assertEqual(report.relation, "reordered")

    def test_v2_subset_relation(self):
        report = relation_report((0, 1), (1,))
        self.assertEqual(report.relation, "v2_subset")

    def test_v2_superset_relation(self):
        report = relation_report((1,), (0, 1))
        self.assertEqual(report.relation, "v2_superset")

    def test_mixed_relation(self):
        report = relation_report((0, 1), (1, 2))
        self.assertEqual(report.relation, "mixed")

    def test_v1_empty_v2_nonempty_is_mixed(self):
        report = relation_report((), (0,))
        self.assertEqual(
            (report.relation, report.v1_selected_count, report.v2_selected_count),
            ("v2_superset", 0, 1),
        )

    def test_v1_nonempty_v2_empty_is_subset(self):
        report = relation_report((0,), ())
        self.assertEqual(
            (report.relation, report.v1_selected_count, report.v2_selected_count),
            ("v2_subset", 1, 0),
        )

    def test_overlap_and_only_counts_are_exact(self):
        report = relation_report((0, 1, 2), (1, 2, 3))
        self.assertEqual(
            (report.overlap_count, report.v1_only_count, report.v2_only_count),
            (2, 1, 1),
        )

    def test_v2_mode_counts_are_exact(self):
        report = relation_report(
            (0, 1, 2),
            (0, 1, 2),
            modes=("direct", "cautious", "associate_only"),
        )
        self.assertEqual(
            (
                report.direct_count,
                report.cautious_count,
                report.associate_only_count,
            ),
            (1, 1, 1),
        )

    def test_completed_repr_is_structural_and_data_free(self):
        hostile = "PRIVATE-HOSTILE-QUERY"
        report = relation_report((0,), (0,))
        rendered = repr(report)
        self.assertIn("status=completed", rendered)
        self.assertNotIn(hostile, rendered)
        self.assertNotIn("memory_key", rendered)

    def test_failed_repr_and_category_are_fixed(self):
        report = memory_retrieval_v2_shadow.MemoryRetrievalV2ShadowReport.failed()
        self.assertEqual(report.status, "failed")
        self.assertEqual(
            report.category,
            "memory_retrieval_v2_shadow_unavailable",
        )
        self.assertEqual(
            repr(report),
            "<MemoryRetrievalV2ShadowReport status=failed "
            "category=memory_retrieval_v2_shadow_unavailable>",
        )

    def test_tampered_report_repr_remains_data_free(self):
        hostile = "PRIVATE-TAMPERED-REPORT"
        report = relation_report((0,), (0,))
        object.__setattr__(report, "relation", HostileValue(hostile))
        self.assertEqual(repr(report), "<MemoryRetrievalV2ShadowReport invalid>")
        self.assertNotIn(hostile, repr(report))

    def test_impossible_relation_count_combinations_are_rejected(self):
        hostile = "PRIVATE-IMPOSSIBLE-REPORT"
        cases = (
            ("both_empty", 1, 0, 0, 1, 0),
            ("identical", 2, 2, 1, 1, 1),
            ("reordered", 1, 2, 1, 0, 1),
            ("v2_subset", 2, 1, 0, 2, 1),
            ("v2_superset", 1, 2, 0, 1, 2),
            ("mixed", 1, 1, 1, 0, 0),
            ("mixed", 2, 1, 1, 1, 0),
        )
        for relation, v1_count, v2_count, overlap, v1_only, v2_only in cases:
            with self.subTest(relation=relation), self.assertRaises(
                RuntimeError
            ) as caught:
                memory_retrieval_v2_shadow.MemoryRetrievalV2ShadowReport(
                    status="completed",
                    relation=relation,
                    candidate_count=max(v1_count, v2_count, 1),
                    v1_selected_count=v1_count,
                    v2_eligible_count=v2_count,
                    v2_selected_count=v2_count,
                    overlap_count=overlap,
                    v1_only_count=v1_only,
                    v2_only_count=v2_only,
                    direct_count=v2_count,
                )
            self.assertEqual(
                str(caught.exception),
                "memory_retrieval_v2_shadow_unavailable",
            )
            self.assertNotIn(hostile, repr(caught.exception))


class HostileValue:
    def __init__(self, private: str):
        self.private = private

    def __repr__(self):
        raise RuntimeError(self.private)

    def __deepcopy__(self, _memo):
        raise RuntimeError(self.private)


class ShadowIsolationTests(unittest.TestCase):
    def test_zero_signal_plan_with_selected_item_is_fixed_failure(self):
        candidate = safe_item("alpha")
        plan = v2_plan(
            (candidate,),
            (0,),
            eligible_count=1,
            query_signal_count=0,
        )
        with mock.patch.object(
            memory_retrieval_v2_shadow.memory_retrieval_v2,
            "plan_memory_recall_v2",
            return_value=plan,
        ):
            report = memory_retrieval_v2_shadow.compare_memory_retrieval_v2_shadow(
                (candidate,),
                (candidate,),
                query_text="alpha",
            )
        self.assertEqual(report.status, "failed")
        self.assertEqual(
            report.category,
            "memory_retrieval_v2_shadow_unavailable",
        )

    def test_zero_signal_plan_with_unselected_eligible_item_is_fixed_failure(self):
        candidate = safe_item("alpha")
        plan = v2_plan(
            (candidate,),
            (),
            eligible_count=1,
            query_signal_count=0,
        )
        with mock.patch.object(
            memory_retrieval_v2_shadow.memory_retrieval_v2,
            "plan_memory_recall_v2",
            return_value=plan,
        ):
            report = memory_retrieval_v2_shadow.compare_memory_retrieval_v2_shadow(
                (candidate,),
                (),
                query_text="alpha",
            )
        self.assertEqual(report.status, "failed")
        self.assertEqual(
            report.category,
            "memory_retrieval_v2_shadow_unavailable",
        )

    def test_genuine_zero_signal_empty_plan_remains_valid(self):
        candidate = safe_item("private candidate")
        report = memory_retrieval_v2_shadow.compare_memory_retrieval_v2_shadow(
            (candidate,),
            (),
            query_text="what are you and how",
        )
        self.assertEqual(report.status, "completed")
        self.assertEqual(report.relation, "both_empty")
        self.assertEqual(report.v2_eligible_count, 0)
        self.assertEqual(report.v2_selected_count, 0)

    def test_hostile_zero_signal_value_is_rejected_data_free(self):
        private = "PRIVATE-HOSTILE-ZERO-SIGNAL"
        plan = v2_plan((), (), query_signal_count=0)
        object.__setattr__(plan, "query_signal_count", HostileValue(private))
        with mock.patch.object(
            memory_retrieval_v2_shadow.memory_retrieval_v2,
            "plan_memory_recall_v2",
            return_value=plan,
        ):
            report = memory_retrieval_v2_shadow.compare_memory_retrieval_v2_shadow(
                (),
                (),
                query_text="alpha",
            )
        self.assertEqual(report.status, "failed")
        self.assertEqual(
            repr(report),
            "<MemoryRetrievalV2ShadowReport status=failed "
            "category=memory_retrieval_v2_shadow_unavailable>",
        )
        self.assertNotIn(private, repr(report))

    def test_planner_receives_isolated_structural_copy(self):
        provenance = [{"source": "safe"}]
        candidate = safe_item("alpha", provenance=provenance)
        captured = {}

        def planner(candidates, **_kwargs):
            captured["candidates"] = candidates
            return v2_plan(candidates, (0,))

        with mock.patch.object(
            memory_retrieval_v2_shadow.memory_retrieval_v2,
            "plan_memory_recall_v2",
            side_effect=planner,
        ):
            report = memory_retrieval_v2_shadow.compare_memory_retrieval_v2_shadow(
                (candidate,),
                (candidate,),
                query_text="alpha",
            )
        planner_candidate = captured["candidates"][0]
        self.assertEqual(report.status, "completed")
        self.assertIsNot(planner_candidate, candidate)
        self.assertIsNot(planner_candidate["provenance"], provenance)
        self.assertIsNot(planner_candidate["provenance"][0], provenance[0])

    def test_v2_mutation_attempt_cannot_mutate_v1_or_source(self):
        provenance = [{"source": "safe"}]
        candidate = safe_item("alpha", provenance=provenance)
        v1_selected = (dict(candidate),)

        def planner(candidates, **_kwargs):
            candidates[0]["normalized_content"] = "MUTATED"
            candidates[0]["provenance"][0]["source"] = "MUTATED"
            return v2_plan(candidates, (0,))

        with mock.patch.object(
            memory_retrieval_v2_shadow.memory_retrieval_v2,
            "plan_memory_recall_v2",
            side_effect=planner,
        ):
            report = memory_retrieval_v2_shadow.compare_memory_retrieval_v2_shadow(
                (candidate,),
                v1_selected,
                query_text="alpha",
            )
        self.assertEqual(report.status, "failed")
        self.assertEqual(candidate["normalized_content"], "alpha")
        self.assertEqual(provenance, [{"source": "safe"}])
        self.assertEqual(v1_selected[0]["normalized_content"], "alpha")

    def test_hostile_nested_provenance_is_shadow_failure_only(self):
        private = "PRIVATE-HOSTILE-PROVENANCE"
        candidate = safe_item("alpha", provenance=[HostileValue(private)])
        report = memory_retrieval_v2_shadow.compare_memory_retrieval_v2_shadow(
            (candidate,),
            (candidate,),
            query_text="alpha",
        )
        self.assertEqual(report.status, "failed")
        self.assertNotIn(private, repr(report))

    def test_hostile_query_is_not_retained_or_exposed(self):
        private = "PRIVATE-PROMPT-INJECTION"
        candidate = safe_item("alpha")
        report = memory_retrieval_v2_shadow.compare_memory_retrieval_v2_shadow(
            (candidate,),
            (candidate,),
            query_text=private,
        )
        self.assertNotIn(private, repr(report))
        self.assertFalse(hasattr(report, "query_text"))

    def test_planner_raise_becomes_fixed_failure(self):
        private = "PRIVATE-PLANNER-FAILURE"
        candidate = safe_item("alpha")
        with mock.patch.object(
            memory_retrieval_v2_shadow.memory_retrieval_v2,
            "plan_memory_recall_v2",
            side_effect=RuntimeError(private),
        ):
            report = memory_retrieval_v2_shadow.compare_memory_retrieval_v2_shadow(
                (candidate,),
                (candidate,),
                query_text="alpha",
            )
        self.assertEqual(report.status, "failed")
        self.assertNotIn(private, repr(report))

    def test_malformed_plan_becomes_fixed_failure(self):
        candidate = safe_item("alpha")
        with mock.patch.object(
            memory_retrieval_v2_shadow.memory_retrieval_v2,
            "plan_memory_recall_v2",
            return_value=object(),
        ):
            report = memory_retrieval_v2_shadow.compare_memory_retrieval_v2_shadow(
                (candidate,),
                (candidate,),
                query_text="alpha",
            )
        self.assertEqual(report.status, "failed")

    def test_forged_candidate_becomes_fixed_failure(self):
        candidate = safe_item("alpha", marker="A")
        forged = safe_item("alpha", marker="Z")
        plan = v2_plan((forged,), (0,))
        object.__setattr__(plan, "candidate_count", 1)
        with mock.patch.object(
            memory_retrieval_v2_shadow.memory_retrieval_v2,
            "plan_memory_recall_v2",
            return_value=plan,
        ):
            report = memory_retrieval_v2_shadow.compare_memory_retrieval_v2_shadow(
                (candidate,),
                (candidate,),
                query_text="alpha",
            )
        self.assertEqual(report.status, "failed")

    def test_reused_candidate_beyond_snapshot_occurrence_fails(self):
        candidate = safe_item("alpha")
        item = memory_retrieval_v2.MemoryRecallItemV2(candidate, "direct")
        plan = memory_retrieval_v2.MemoryRetrievalPlanV2(
            items=(item, item),
            candidate_count=1,
            eligible_count=1,
            selected_count=2,
            query_signal_count=1,
            total_chars=10,
            direct_count=2,
            cautious_count=0,
            associate_only_count=0,
        )
        with mock.patch.object(
            memory_retrieval_v2_shadow.memory_retrieval_v2,
            "plan_memory_recall_v2",
            return_value=plan,
        ):
            report = memory_retrieval_v2_shadow.compare_memory_retrieval_v2_shadow(
                (candidate,),
                (candidate,),
                query_text="alpha",
            )
        self.assertEqual(report.status, "failed")


class ShadowMemoryContextIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.base = (
            {"role": "system", "content": "persona"},
            {"role": "assistant", "content": "prior"},
            {"role": "user", "content": "alpha current question"},
        )
        self.item = safe_item("alpha remembered preference")

    def dispatch(self, *, shadow: bool = True):
        service = FakeReadService([self.item])
        result = memory_context_integration.prepare_transient_memory_dispatch(
            service,
            self.base,
            enabled=True,
            smart_retrieval_enabled=True,
            retrieval_v2_shadow_enabled=shadow,
        )
        return service, result

    def test_direct_flag_validation_is_exact_and_pre_read(self):
        service = FakeReadService(error=AssertionError("must not read"))
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(
                memory_context_integration.MemoryContextIntegrationError
            ):
                memory_context_integration.prepare_transient_memory_dispatch(
                    service,
                    self.base,
                    enabled=True,
                    smart_retrieval_enabled=True,
                    retrieval_v2_shadow_enabled=value,
                )
        with self.assertRaises(memory_context_integration.MemoryContextIntegrationError):
            memory_context_integration.prepare_transient_memory_dispatch(
                service,
                self.base,
                enabled=True,
                smart_retrieval_enabled=False,
                retrieval_v2_shadow_enabled=True,
            )
        self.assertEqual(service.calls, [])

    def test_disabled_shadow_is_exact_v1_noop(self):
        service = FakeReadService([self.item])
        with mock.patch.object(
            memory_context_integration.memory_retrieval_v2_shadow,
            "compare_memory_retrieval_v2_shadow",
        ) as shadow:
            result = memory_context_integration.prepare_transient_memory_dispatch(
                service,
                self.base,
                enabled=True,
                smart_retrieval_enabled=True,
                retrieval_v2_shadow_enabled=False,
            )
        shadow.assert_not_called()
        self.assertIsNone(result.retrieval_v2_shadow_report)

    def test_shadow_uses_one_memory_read(self):
        service, result = self.dispatch()
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(result.retrieval_v2_shadow_report.status, "completed")

    def test_v1_and_v2_use_equal_isolated_candidate_snapshots(self):
        service = FakeReadService([self.item])
        seen = {}
        real_v1 = memory_context_integration.memory_retrieval.select_relevant_memory_items
        real_v2 = (
            memory_context_integration.memory_retrieval_v2_shadow
            .memory_retrieval_v2.plan_memory_recall_v2
        )

        def capture_v1(items, **kwargs):
            seen["v1"] = items
            return real_v1(items, **kwargs)

        def capture_v2(items, **kwargs):
            seen["v2"] = items
            return real_v2(items, **kwargs)

        with (
            mock.patch.object(
                memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
                side_effect=capture_v1,
            ),
            mock.patch.object(
                memory_context_integration.memory_retrieval_v2_shadow
                .memory_retrieval_v2,
                "plan_memory_recall_v2",
                side_effect=capture_v2,
            ),
        ):
            result = memory_context_integration.prepare_transient_memory_dispatch(
                service,
                self.base,
                enabled=True,
                smart_retrieval_enabled=True,
                retrieval_v2_shadow_enabled=True,
            )
        self.assertEqual(seen["v1"], seen["v2"])
        self.assertIsNot(seen["v1"], seen["v2"])
        self.assertIsNot(seen["v1"][0], seen["v2"][0])
        self.assertEqual(result.retrieval_v2_shadow_report.status, "completed")

    def test_only_final_current_user_query_reaches_v2(self):
        seen = {}
        real = (
            memory_context_integration.memory_retrieval_v2_shadow
            .memory_retrieval_v2.plan_memory_recall_v2
        )

        def capture(items, **kwargs):
            seen.update(kwargs)
            return real(items, **kwargs)

        with mock.patch.object(
            memory_context_integration.memory_retrieval_v2_shadow
            .memory_retrieval_v2,
            "plan_memory_recall_v2",
            side_effect=capture,
        ):
            self.dispatch()
        self.assertEqual(seen["query_text"], self.base[-1]["content"])
        self.assertNotEqual(seen["query_text"], self.base[-2]["content"])

    def test_provider_messages_are_byte_identical_on_and_off(self):
        _service_off, off = self.dispatch(shadow=False)
        _service_on, on = self.dispatch(shadow=True)
        encoded_off = json.dumps(off.provider_messages, ensure_ascii=False)
        encoded_on = json.dumps(on.provider_messages, ensure_ascii=False)
        self.assertEqual(encoded_on.encode(), encoded_off.encode())

    def test_v1_developer_message_bytes_are_identical_on_and_off(self):
        _service_off, off = self.dispatch(shadow=False)
        _service_on, on = self.dispatch(shadow=True)
        self.assertEqual(
            off.provider_messages[-2]["content"].encode(),
            on.provider_messages[-2]["content"].encode(),
        )

    def test_v2_failure_keeps_qualified_v1_rendering(self):
        with mock.patch.object(
            memory_context_integration.memory_retrieval_v2_shadow
            .memory_retrieval_v2,
            "plan_memory_recall_v2",
            side_effect=RuntimeError("PRIVATE-V2-FAILURE"),
        ):
            _service, result = self.dispatch()
        self.assertTrue(result.memory_applied)
        self.assertIn(
            self.item["normalized_content"],
            result.provider_messages[-2]["content"],
        )
        self.assertEqual(result.retrieval_v2_shadow_report.status, "failed")

    def test_malformed_v2_plan_keeps_qualified_v1_rendering(self):
        with mock.patch.object(
            memory_context_integration.memory_retrieval_v2_shadow
            .memory_retrieval_v2,
            "plan_memory_recall_v2",
            return_value=object(),
        ):
            _service, result = self.dispatch()
        self.assertTrue(result.memory_applied)
        self.assertEqual(result.retrieval_v2_shadow_report.status, "failed")

    def test_v1_failure_retains_existing_fail_closed_behavior(self):
        with mock.patch.object(
            memory_context_integration.memory_retrieval,
            "select_relevant_memory_items",
            side_effect=RuntimeError("PRIVATE-V1-FAILURE"),
        ):
            with self.assertRaises(memory_context_integration.MemoryContextIntegrationError):
                self.dispatch()

    def test_v1_empty_v2_nonempty_is_observable_before_renderer_return(self):
        selection = memory_retrieval.MemoryRetrievalSelectionV1(
            items=(),
            candidate_count=1,
            selected_count=0,
            query_signal_count=1,
        )
        plan = v2_plan((self.item,), (0,))
        with (
            mock.patch.object(
                memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
                return_value=selection,
            ),
            mock.patch.object(
                memory_context_integration.memory_retrieval_v2_shadow
                .memory_retrieval_v2,
                "plan_memory_recall_v2",
                return_value=plan,
            ),
        ):
            _service, result = self.dispatch()
        self.assertFalse(result.memory_applied)
        self.assertIs(result.provider_messages, self.base)
        self.assertEqual(result.retrieval_v2_shadow_report.relation, "v2_superset")

    def test_v1_nonempty_v2_empty_remains_v1_rendered(self):
        empty_plan = v2_plan((self.item,), ())
        with mock.patch.object(
            memory_context_integration.memory_retrieval_v2_shadow
            .memory_retrieval_v2,
            "plan_memory_recall_v2",
            return_value=empty_plan,
        ):
            _service, result = self.dispatch()
        self.assertTrue(result.memory_applied)
        self.assertEqual(result.retrieval_v2_shadow_report.relation, "v2_subset")

    def test_no_recall_use_or_v2_marker_reaches_provider(self):
        _service, result = self.dispatch()
        encoded = json.dumps(result.provider_messages, ensure_ascii=False)
        self.assertNotIn("recall_use", encoded)
        self.assertNotIn("retrieval_v2", encoded)
        self.assertNotIn("shadow", encoded)

    def test_transient_dispatch_repr_is_unchanged(self):
        _service, result = self.dispatch()
        self.assertEqual(
            repr(result),
            "<TransientMemoryDispatch memory_applied=True>",
        )


class ShadowPurityAndFrozenContractTests(unittest.TestCase):
    def test_shadow_module_imports_only_v2_and_pure_standard_library(self):
        source = Path(memory_retrieval_v2_shadow.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".", 1)[0])
        self.assertEqual(
            roots,
            {"__future__", "math", "dataclasses", "typing", "memory_retrieval_v2"},
        )

    def test_shadow_module_has_no_io_clock_random_or_background_calls(self):
        source = Path(memory_retrieval_v2_shadow.__file__).read_text(encoding="utf-8")
        folded = source.casefold()
        for forbidden in (
            "sqlite3",
            "socket",
            "requests",
            "httpx",
            "pathlib",
            "open(",
            "print(",
            "datetime",
            "time.",
            "random",
            "asyncio",
            "create_task",
            "memory_store",
            "memory_service",
            "heartbeat",
            "continuity",
        ):
            self.assertNotIn(forbidden, folded)

    def test_shadow_execution_performs_no_database_or_network(self):
        candidate = safe_item("alpha")
        with (
            mock.patch.object(sqlite3, "connect") as database,
            mock.patch.object(socket, "create_connection") as network,
        ):
            report = memory_retrieval_v2_shadow.compare_memory_retrieval_v2_shadow(
                (candidate,),
                (candidate,),
                query_text="alpha",
            )
        self.assertEqual(report.status, "completed")
        database.assert_not_called()
        network.assert_not_called()

    def test_v1_and_v2_git_blobs_are_frozen(self):
        repo_root = Path(__file__).resolve().parents[2]
        for path, expected in (
            ("backend/memory_retrieval.py", V1_BLOB_SHA),
            ("backend/memory_retrieval_v2.py", V2_BLOB_SHA),
        ):
            with self.subTest(path=path):
                actual = subprocess.check_output(
                    ["git", "rev-parse", f":{path}"],
                    cwd=repo_root,
                    text=True,
                    encoding="utf-8",
                ).strip()
                self.assertEqual(actual, expected)

    def test_maximum_migration_remains_010_and_no_011_exists(self):
        self.assertEqual(max(version for version, _name, _apply in channel_store.MIGRATIONS), 10)
        repo_root = Path(__file__).resolve().parents[2]
        migration_files = tuple(repo_root.glob("backend/**/*011*"))
        self.assertEqual(migration_files, ())

    def test_loop_chat_and_non_memory_contract_sources_have_no_shadow(self):
        repo_root = Path(__file__).resolve().parents[2]
        for relative in (
            "examples/api_loop.py",
            "backend/kelivo_service.py",
            "backend/channel_store.py",
            "backend/heartbeat_service.py",
            "backend/continuity_context.py",
        ):
            with self.subTest(relative=relative):
                source = (repo_root / relative).read_text(encoding="utf-8")
                self.assertNotIn("retrieval_v2_shadow", source)

    def test_render_blueprint_and_ci_pin_shadow_false(self):
        repo_root = Path(__file__).resolve().parents[2]
        blueprint = json.loads((repo_root / "render.yaml").read_text(encoding="utf-8"))
        env = {
            item["key"]: item
            for item in blueprint["services"][0]["envVars"]
        }
        self.assertEqual(
            env["MEMORY_RETRIEVAL_V2_SHADOW_ENABLED"]["value"],
            "false",
        )
        workflow = (repo_root / ".github/workflows/python-ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'env["MEMORY_RETRIEVAL_V2_SHADOW_ENABLED"].get("value") == "false"',
            workflow,
        )

    def test_no_shadow_report_field_enters_frozen_request_types(self):
        from backend import kelivo_service

        for contract in (
            kelivo_service.PreparedRequest,
            kelivo_service.FrozenRequestContract,
        ):
            names = {field.name for field in dataclasses.fields(contract)}
            self.assertTrue(names.isdisjoint({
                "retrieval_v2_shadow_report",
                "recall_use",
                "retrieval_v2",
            }))


class ShadowAppIntegrationTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
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
                retrieval_v2_shadow_enabled=True,
            ),
        )
        self.service = FakeReadService([
            safe_item("alpha current question remembered preference")
        ])
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
        self.headers = {
            "Authorization": "Bearer test-kelivo-key-distinct-1234567890",
            "Idempotency-Key": "shadow-key-0001",
        }

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
            headers={**self.headers, "Idempotency-Key": key},
            json=self.payload(text),
        )

    async def test_shadow_logs_data_free_and_v1_marker_is_unchanged(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            response = await self.post("shadow-log-key-0001")
        self.assertEqual(response.status_code, 200)
        line = next(
            line
            for line in output.getvalue().splitlines()
            if line.startswith("[memory-retrieval-v2-shadow]")
        )
        self.assertIn("status=completed", line)
        self.assertNotIn("remembered preference", line)
        self.assertNotIn("memory_key", line)
        messages, context = self.provider_calls[0]
        self.assertEqual(
            context["transient_memory_dispatch"],
            "kelivo-transient-memory-dispatch-v1",
        )
        self.assertTrue(all(
            "shadow" not in key and "retrieval_v2" not in key
            for key in context
        ))
        self.assertNotIn("recall_use", json.dumps(messages))

    async def test_shadow_failure_log_is_fixed_and_v1_still_dispatches(self):
        private = "PRIVATE-SHADOW-FAILURE"
        output = io.StringIO()
        with (
            mock.patch.object(
                self.module.memory_context_integration.memory_retrieval_v2_shadow
                .memory_retrieval_v2,
                "plan_memory_recall_v2",
                side_effect=RuntimeError(private),
            ),
            contextlib.redirect_stdout(output),
        ):
            response = await self.post("shadow-failure-key-0001")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.provider_calls), 1)
        self.assertIn(
            "[memory-retrieval-v2-shadow] status=failed "
            "category=memory_retrieval_v2_shadow_unavailable",
            output.getvalue(),
        )
        self.assertNotIn(private, output.getvalue())

    async def test_logging_failure_cannot_affect_generation(self):
        with mock.patch("builtins.print", side_effect=RuntimeError("PRIVATE-LOG")):
            print_failure = await self.post("shadow-log-failure-key-0001")
        with mock.patch.object(
            self.module,
            "_log_memory_retrieval_v2_shadow",
            side_effect=RuntimeError("PRIVATE-REPLACED-LOGGER"),
        ):
            replaced_logger = await self.post("shadow-log-failure-key-0002")
        self.assertEqual(
            (print_failure.status_code, replaced_logger.status_code),
            (200, 200),
        )
        self.assertEqual(len(self.provider_calls), 2)

    async def test_replay_and_operit_never_rerun_shadow(self):
        real = (
            self.module.memory_context_integration.memory_retrieval_v2_shadow
            .memory_retrieval_v2.plan_memory_recall_v2
        )
        with mock.patch.object(
            self.module.memory_context_integration.memory_retrieval_v2_shadow
            .memory_retrieval_v2,
            "plan_memory_recall_v2",
            wraps=real,
        ) as planner:
            first = await self.post("shadow-replay-key-0001")
            replay = await self.post("shadow-replay-key-0001")
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
        self.assertEqual((first.status_code, replay.status_code, operit.status_code), (200, 200, 200))
        self.assertEqual(first.json(), replay.json())
        self.assertEqual(planner.call_count, 1)
        self.assertEqual(len(self.service.calls), 1)

    async def test_shadow_does_not_change_frozen_identity_or_provider_bytes(self):
        self.module.DEPLOYMENT = dataclasses.replace(
            self.module.DEPLOYMENT,
            memory=dataclasses.replace(
                self.module.DEPLOYMENT.memory,
                retrieval_v2_shadow_enabled=False,
            ),
        )
        off = await self.post("shadow-identity-off-0001")
        self.module.DEPLOYMENT = dataclasses.replace(
            self.module.DEPLOYMENT,
            memory=dataclasses.replace(
                self.module.DEPLOYMENT.memory,
                retrieval_v2_shadow_enabled=True,
            ),
        )
        on = await self.post("shadow-identity-on-0001")
        self.assertEqual((off.status_code, on.status_code), (200, 200))
        self.assertEqual(self.provider_calls[0], self.provider_calls[1])
        with self.module.db() as conn:
            rows = conn.execute(
                """SELECT request_payload_hash,request_identity_hash,
                          provider_messages_json,context_bundle_json,
                          context_bundle_hash,prompt_contract_version
                     FROM kelivo_requests ORDER BY id"""
            ).fetchall()
        for name in rows[0].keys():
            self.assertEqual(rows[0][name], rows[1][name], name)

    async def test_readyz_shape_is_unchanged_by_shadow_toggle(self):
        enabled = await request(self.module, "GET", "/readyz")
        self.module.DEPLOYMENT = dataclasses.replace(
            self.module.DEPLOYMENT,
            memory=dataclasses.replace(
                self.module.DEPLOYMENT.memory,
                retrieval_v2_shadow_enabled=False,
            ),
        )
        disabled = await request(self.module, "GET", "/readyz")
        self.assertEqual(enabled.status_code, disabled.status_code)
        self.assertEqual(enabled.json(), disabled.json())


if __name__ == "__main__":
    unittest.main()
