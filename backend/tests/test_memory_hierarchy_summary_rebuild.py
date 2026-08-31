from __future__ import annotations

import dataclasses
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import (
    memory_hierarchy_episode_refinement as episode,
    memory_hierarchy_projection as hierarchy,
    memory_hierarchy_projection_store as hierarchy_store,
    memory_hierarchy_snapshot,
    memory_hierarchy_summary_rebuild as rebuild,
    memory_hierarchy_summary_store as summary_store,
)


P1 = "summary_rebuild_atomic_000000000001"
P2 = "summary_rebuild_atomic_000000000002"
P3 = "summary_rebuild_atomic_000000000003"


def atomic(
    key: str,
    kind: str,
    content: str,
    *,
    sensitivity: str = "normal",
    updated: str = "2026-08-31T12:00:00+00:00",
):
    return hierarchy.AtomicMemoryProjectionInputV1(
        memory_key=key,
        kind=kind,
        scope_type="global_user",
        scope_ref="",
        normalized_content=content,
        fingerprint_version=1,
        status="active",
        explicitness="inferred",
        confidence=1.0,
        sensitivity=sensitivity,
        first_observed_at="2026-08-31T10:00:00+00:00",
        last_confirmed_at="2026-08-31T11:00:00+00:00",
        updated_at=updated,
    )


def atomics():
    return (
        atomic(P1, "project", "The project backend runs on Render."),
        atomic(P2, "decision", "Web Memory formation authority uses V2."),
        atomic(P3, "task_or_progress", "The V2 authority cutover is live."),
    )


def topics():
    return (
        hierarchy.TopicGroupingV1(
            "topic.project",
            tuple(sorted((P1, P2, P3))),
        ),
    )


def plan_with_episode(items=None):
    source = atomics() if items is None else items
    return episode.build_hierarchy_plan_with_episodes_v1(
        source,
        topics(),
        (episode.EpisodeMembershipProposalV1((P2, P3)),),
    )


def plan_without_episode(items=None):
    source = atomics() if items is None else items
    return hierarchy.plan_hierarchy_projection_v1(source, topics(), ())


def topic_node(plan):
    return next(node for node in plan.nodes if node.node_type == "topic")


def state_node(plan):
    return next(node for node in plan.nodes if node.node_type == "canonical_state")


def snapshot(items):
    return memory_hierarchy_snapshot.HierarchyAtomicSnapshotV1(tuple(items))


class MemoryHierarchySummaryRebuildTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.relay = (self.root / "relay.db").resolve()
        self.hierarchy = (self.root / "memory-hierarchy.db").resolve()
        self.cache = (self.root / "memory-hierarchy-summary.db").resolve()
        self.relay.write_bytes(b"authoritative-relay-sentinel")
        self.reader = memory_hierarchy_snapshot.MemoryHierarchySnapshotReader(
            self.relay,
            fingerprint_key_id="summary-rebuild-key",
            fingerprint_hmac_secret="SummaryRebuild-HMAC-0123456789-abcdef-XYZ!",
            max_item_chars=4096,
            sensitive_storage_enabled=False,
        )
        hierarchy_store.initialize_projection_store(self.hierarchy)
        hierarchy_store.apply_projection_plan(self.hierarchy, plan_with_episode())

    async def asyncTearDown(self):
        self.temp.cleanup()

    def patch_snapshot(self, items):
        return mock.patch.object(
            memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
            "load_active_snapshot",
            return_value=snapshot(items),
        )

    @staticmethod
    def good_provider(call_log):
        async def generate(messages, _session, _model, _temperature, _tokens, context):
            payload = json.loads(messages[1]["content"])
            call_log.append(context["summary_target_type"])
            clauses = [
                {
                    "memory_keys": [record["memory_key"]],
                    "text": record["content"],
                }
                for record in payload["records"]
            ]
            return {
                "text": json.dumps(
                    {
                        "version": "memory-hierarchy-summary-extractor-v1",
                        "clauses": clauses,
                    },
                    separators=(",", ":"),
                )
            }

        return generate

    async def run_rebuild(self, items, provider):
        with self.patch_snapshot(items):
            return await rebuild.rebuild_current_hierarchy_summaries_v1(
                self.reader,
                self.hierarchy,
                self.cache,
                provider,
                provider_model="test-model",
                provider_prompt_contract_version="test-prompt-v1",
            )

    async def test_first_run_generates_all_targets_and_second_run_calls_provider_zero(self):
        relay_before = self.relay.read_bytes()
        hierarchy_before = self.hierarchy.read_bytes()
        first_calls = []
        first = await self.run_rebuild(
            atomics(),
            self.good_provider(first_calls),
        )
        self.assertEqual(first.status, "completed")
        self.assertEqual(first.target_count, 2)
        self.assertEqual(first.cache_hit_count, 0)
        self.assertEqual(first.generated_count, 2)
        self.assertEqual(first.failed_count, 0)
        self.assertEqual(first.provider_call_count, 2)
        self.assertEqual(sorted(first_calls), ["canonical_state", "topic"])
        self.assertEqual(self.relay.read_bytes(), relay_before)
        self.assertEqual(self.hierarchy.read_bytes(), hierarchy_before)

        current_plan = plan_with_episode()
        self.assertIsNotNone(
            summary_store.load_current_summary(self.cache, topic_node(current_plan))
        )
        self.assertIsNotNone(
            summary_store.load_current_summary(self.cache, state_node(current_plan))
        )

        second_calls = []
        second = await self.run_rebuild(
            atomics(),
            self.good_provider(second_calls),
        )
        self.assertEqual(second.status, "completed")
        self.assertEqual(second.cache_hit_count, 2)
        self.assertEqual(second.generated_count, 0)
        self.assertEqual(second.failed_count, 0)
        self.assertEqual(second.pruned_count, 0)
        self.assertEqual(second.provider_call_count, 0)
        self.assertEqual(second_calls, [])

    async def test_episode_regrouping_regenerates_topic_only(self):
        await self.run_rebuild(atomics(), self.good_provider([]))
        new_plan = plan_without_episode()
        hierarchy_store.apply_projection_plan(self.hierarchy, new_plan)
        calls = []
        receipt = await self.run_rebuild(
            atomics(),
            self.good_provider(calls),
        )
        self.assertEqual(receipt.target_count, 2)
        self.assertEqual(receipt.pruned_count, 1)
        self.assertEqual(receipt.cache_hit_count, 1)
        self.assertEqual(receipt.generated_count, 1)
        self.assertEqual(receipt.failed_count, 0)
        self.assertEqual(receipt.provider_call_count, 1)
        self.assertEqual(calls, ["topic"])
        self.assertIsNotNone(
            summary_store.load_current_summary(self.cache, state_node(new_plan))
        )
        self.assertIsNotNone(
            summary_store.load_current_summary(self.cache, topic_node(new_plan))
        )

    async def test_atomic_change_regenerates_topic_and_state(self):
        await self.run_rebuild(atomics(), self.good_provider([]))
        changed = tuple(
            dataclasses.replace(
                item,
                normalized_content="The project backend runs on a changed platform.",
                updated_at="2026-09-01T00:00:00+00:00",
            ) if item.memory_key == P1 else item
            for item in atomics()
        )
        changed_plan = plan_with_episode(changed)
        hierarchy_store.apply_projection_plan(self.hierarchy, changed_plan)
        calls = []
        receipt = await self.run_rebuild(
            changed,
            self.good_provider(calls),
        )
        self.assertEqual(receipt.pruned_count, 2)
        self.assertEqual(receipt.cache_hit_count, 0)
        self.assertEqual(receipt.generated_count, 2)
        self.assertEqual(receipt.provider_call_count, 2)
        self.assertEqual(sorted(calls), ["canonical_state", "topic"])

    async def test_one_provider_failure_is_resumable_and_other_target_commits(self):
        calls = []

        async def partial(messages, session, model, temperature, tokens, context):
            calls.append(context["summary_target_type"])
            if context["summary_target_type"] == "topic":
                raise RuntimeError("temporary provider failure")
            return await self.good_provider([])(
                messages,
                session,
                model,
                temperature,
                tokens,
                context,
            )

        receipt = await self.run_rebuild(atomics(), partial)
        self.assertEqual(receipt.status, "completed_with_failures")
        self.assertEqual(receipt.generated_count, 1)
        self.assertEqual(receipt.failed_count, 1)
        self.assertEqual(receipt.provider_call_count, 2)
        current_plan = plan_with_episode()
        self.assertIsNone(
            summary_store.load_current_summary(self.cache, topic_node(current_plan))
        )
        self.assertIsNotNone(
            summary_store.load_current_summary(self.cache, state_node(current_plan))
        )

        retry_calls = []
        retry = await self.run_rebuild(
            atomics(),
            self.good_provider(retry_calls),
        )
        self.assertEqual(retry.status, "completed")
        self.assertEqual(retry.cache_hit_count, 1)
        self.assertEqual(retry.generated_count, 1)
        self.assertEqual(retry.failed_count, 0)
        self.assertEqual(retry.provider_call_count, 1)
        self.assertEqual(retry_calls, ["topic"])

    async def test_sensitive_targets_fail_before_provider_but_rebuild_remains_optional(self):
        sensitive = tuple(
            dataclasses.replace(item, sensitivity="sensitive")
            if item.memory_key == P1 else item
            for item in atomics()
        )
        hierarchy_store.apply_projection_plan(
            self.hierarchy,
            plan_with_episode(sensitive),
        )
        calls = []
        receipt = await self.run_rebuild(
            sensitive,
            self.good_provider(calls),
        )
        self.assertEqual(receipt.status, "completed_with_failures")
        self.assertEqual(receipt.target_count, 2)
        self.assertEqual(receipt.generated_count, 0)
        self.assertEqual(receipt.failed_count, 2)
        self.assertEqual(receipt.provider_call_count, 0)
        self.assertEqual(calls, [])

    async def test_invalid_authoritative_snapshot_does_not_create_summary_cache(self):
        with mock.patch.object(
            memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
            "load_active_snapshot",
            side_effect=memory_hierarchy_snapshot.MemoryHierarchySnapshotError(
                "hierarchy_snapshot_state_invalid"
            ),
        ):
            with self.assertRaises(rebuild.MemoryHierarchySummaryRebuildError) as raised:
                await rebuild.rebuild_current_hierarchy_summaries_v1(
                    self.reader,
                    self.hierarchy,
                    self.cache,
                    self.good_provider([]),
                    provider_model="test-model",
                    provider_prompt_contract_version="test-prompt-v1",
                )
        self.assertEqual(raised.exception.category, "hierarchy_summary_source_invalid")
        self.assertFalse(self.cache.exists())

    async def test_stale_hierarchy_sidecar_fails_before_cache_creation(self):
        changed = tuple(
            dataclasses.replace(
                item,
                normalized_content="Changed authoritative project fact.",
                updated_at="2026-09-01T00:00:00+00:00",
            ) if item.memory_key == P1 else item
            for item in atomics()
        )
        with self.patch_snapshot(changed):
            with self.assertRaises(rebuild.MemoryHierarchySummaryRebuildError) as raised:
                await rebuild.rebuild_current_hierarchy_summaries_v1(
                    self.reader,
                    self.hierarchy,
                    self.cache,
                    self.good_provider([]),
                    provider_model="test-model",
                    provider_prompt_contract_version="test-prompt-v1",
                )
        self.assertEqual(
            raised.exception.category,
            "hierarchy_summary_projection_invalid",
        )
        self.assertFalse(self.cache.exists())

    async def test_current_cache_corruption_is_fatal_and_never_regenerated_over(self):
        await self.run_rebuild(atomics(), self.good_provider([]))
        current_plan = plan_with_episode()
        current_topic = topic_node(current_plan)
        with sqlite3.connect(self.cache) as conn:
            conn.execute(
                """UPDATE summary_support SET support_ordinal=9
                     WHERE node_key=? AND clause_ordinal=0 AND support_ordinal=0""",
                (current_topic.node_key,),
            )
            conn.commit()
        calls = []
        with self.assertRaises(rebuild.MemoryHierarchySummaryRebuildError) as raised:
            await self.run_rebuild(atomics(), self.good_provider(calls))
        self.assertEqual(raised.exception.category, "hierarchy_summary_cache_invalid")
        self.assertEqual(calls, [])

    async def test_empty_authority_and_hierarchy_prune_old_cache_without_provider(self):
        await self.run_rebuild(atomics(), self.good_provider([]))
        empty_plan = hierarchy.plan_hierarchy_projection_v1((), (), ())
        hierarchy_store.apply_projection_plan(self.hierarchy, empty_plan)
        calls = []
        receipt = await self.run_rebuild((), self.good_provider(calls))
        self.assertEqual(receipt.status, "completed")
        self.assertEqual(receipt.target_count, 0)
        self.assertEqual(receipt.pruned_count, 2)
        self.assertEqual(receipt.provider_call_count, 0)
        self.assertEqual(calls, [])

    async def test_path_aliases_and_invalid_reader_fail_before_provider(self):
        with self.assertRaises(rebuild.MemoryHierarchySummaryRebuildError) as raised:
            await rebuild.rebuild_current_hierarchy_summaries_v1(
                self.reader,
                self.hierarchy,
                self.hierarchy,
                self.good_provider([]),
                provider_model="test-model",
                provider_prompt_contract_version="test-prompt-v1",
            )
        self.assertEqual(
            raised.exception.category,
            "hierarchy_summary_rebuild_configuration_invalid",
        )
        with self.assertRaises(rebuild.MemoryHierarchySummaryRebuildError) as raised:
            await rebuild.rebuild_current_hierarchy_summaries_v1(
                object(),
                self.hierarchy,
                self.cache,
                self.good_provider([]),
                provider_model="test-model",
                provider_prompt_contract_version="test-prompt-v1",
            )
        self.assertEqual(
            raised.exception.category,
            "hierarchy_summary_rebuild_configuration_invalid",
        )


if __name__ == "__main__":
    unittest.main()
