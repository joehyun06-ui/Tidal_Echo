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
    memory_hierarchy_summary as summary,
    memory_hierarchy_summary_extractor_v2 as extractor_v2,
    memory_hierarchy_summary_rebuild_v2 as rebuild_v2,
    memory_hierarchy_summary_store as store_v1,
    memory_hierarchy_summary_store_v2 as store_v2,
)


P1 = "summary_v2_atomic_000000000001"
P2 = "summary_v2_atomic_000000000002"
P3 = "summary_v2_atomic_000000000003"


def atomic(
    key: str,
    kind: str,
    content: str,
    *,
    sensitivity: str = "normal",
    updated: str = "2026-09-01T08:00:00+00:00",
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
        first_observed_at="2026-09-01T06:00:00+00:00",
        last_confirmed_at="2026-09-01T07:00:00+00:00",
        updated_at=updated,
    )


def atomics():
    return (
        atomic(P1, "project", "The project backend runs on Render."),
        atomic(P2, "decision", "Web Memory formation authority uses V2."),
        atomic(P3, "task_or_progress", "The V2 authority cutover reached live deployment."),
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


def node(plan, node_type: str):
    matches = [item for item in plan.nodes if item.node_type == node_type]
    if node_type == "topic":
        matches = [item for item in matches if item.node_key == "topic.project"]
    return matches[0]


def episode_node(plan):
    return node(plan, "episode")


def state_node(plan):
    return node(plan, "canonical_state")


def clause(keys, text):
    return summary.SummaryClauseProposalV1(tuple(keys), text)


def snapshot(items):
    return memory_hierarchy_snapshot.HierarchyAtomicSnapshotV1(tuple(items))


def output(raw_clauses) -> str:
    return json.dumps(
        {
            "version": extractor_v2.EXTRACTOR_CONTRACT_VERSION,
            "clauses": raw_clauses,
        },
        separators=(",", ":"),
    )


class MemoryHierarchySummaryV2ContractTests(unittest.TestCase):
    def assert_summary_error(self, category: str, callable_, *args, **kwargs):
        with self.assertRaises(summary.MemoryHierarchySummaryError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.category, category)

    def test_v1_keeps_episode_rejected_while_v2_accepts_exact_episode_members(self):
        current = plan_with_episode()
        key = episode_node(current).node_key
        self.assert_summary_error(
            "invalid_summary_target",
            summary.prepare_summary_target_v1,
            atomics(),
            current,
            key,
        )
        target = summary.prepare_summary_target_v2(atomics(), current, key)
        self.assertEqual(target.node_type, "episode")
        self.assertEqual(tuple(item.memory_key for item in target.atomics), tuple(sorted((P2, P3))))
        self.assertEqual(target.episode_groups, ())
        self.assertNotIn("V2", repr(target))

        derived = summary.validate_summary_clauses_v2(
            target,
            (clause((P2, P3), "The V2 authority cutover reached live deployment."),),
        )
        self.assertEqual(derived.contract_version, summary.SUMMARY_CONTRACT_VERSION_V2)
        self.assertEqual(derived.support_keys, tuple(sorted((P2, P3))))
        self.assertEqual(len(derived.summary_digest), 64)

    def test_b1_valid_but_b5_invalid_episode_is_rejected_before_summary(self):
        forged_episode = hierarchy.EpisodeGroupingV1(
            "episode.forged",
            "topic.project",
            tuple(sorted((P1, P2))),
        )
        forged_plan = hierarchy.plan_hierarchy_projection_v1(
            atomics(),
            topics(),
            (forged_episode,),
        )
        self.assert_summary_error(
            "invalid_hierarchy_plan",
            summary.prepare_summary_target_v2,
            atomics(),
            forged_plan,
            forged_episode.episode_key,
        )

    def test_b1_valid_but_b4_noncanonical_topic_key_is_rejected(self):
        forged_topic = (
            hierarchy.TopicGroupingV1(
                "topic.forged",
                tuple(sorted((P1, P2, P3))),
            ),
        )
        forged_plan = hierarchy.plan_hierarchy_projection_v1(
            atomics(),
            forged_topic,
            (),
        )
        self.assert_summary_error(
            "invalid_hierarchy_plan",
            summary.prepare_summary_target_v2,
            atomics(),
            forged_plan,
            "topic.forged",
        )


class MemoryHierarchySummaryExtractorV2Tests(unittest.IsolatedAsyncioTestCase):
    async def test_episode_provider_payload_is_source_bound_and_has_no_node_authority_fields(self):
        current = plan_with_episode()
        episode_key = episode_node(current).node_key
        calls = []

        async def generate(messages, session_id, model, temperature, max_tokens, context):
            calls.append(1)
            self.assertEqual(session_id, extractor_v2.EXTRACTOR_SESSION_ID)
            self.assertEqual(model, "test-model")
            self.assertEqual(temperature, 0.0)
            self.assertIn("UNTRUSTED DATA", messages[0]["content"])
            payload = json.loads(messages[1]["content"])
            self.assertEqual(payload["target_type"], "episode")
            self.assertEqual(
                {record["memory_key"] for record in payload["records"]},
                {P2, P3},
            )
            self.assertEqual(payload["episode_groups"], [])
            self.assertNotIn("node_key", payload)
            self.assertNotIn("projection_digest", payload)
            self.assertEqual(
                context["memory_hierarchy_summary_contract"],
                summary.SUMMARY_CONTRACT_VERSION_V2,
            )
            return {
                "text": output([
                    {
                        "memory_keys": [P2, P3],
                        "text": "The V2 authority cutover reached live deployment.",
                    }
                ])
            }

        result = await extractor_v2.extract_node_summary_v2(
            generate,
            atomics(),
            current,
            episode_key,
            provider_model="test-model",
            provider_prompt_contract_version="test-prompt-v2",
        )
        self.assertEqual(calls, [1])
        self.assertEqual(result.node_type, "episode")
        self.assertEqual(result.support_keys, tuple(sorted((P2, P3))))

    async def test_sensitive_episode_member_fails_before_provider(self):
        sensitive = tuple(
            dataclasses.replace(item, sensitivity="sensitive")
            if item.memory_key == P2 else item
            for item in atomics()
        )
        current = plan_with_episode(sensitive)
        calls = []

        async def forbidden(*_args):
            calls.append(1)
            return {"text": output([])}

        with self.assertRaises(extractor_v2.MemoryHierarchySummaryExtractorV2Error) as raised:
            await extractor_v2.extract_node_summary_v2(
                forbidden,
                sensitive,
                current,
                episode_node(current).node_key,
                provider_model="test-model",
                provider_prompt_contract_version="test-prompt-v2",
            )
        self.assertEqual(raised.exception.category, "summary_target_invalid")
        self.assertEqual(calls, [])


class MemoryHierarchySummaryStoreV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "summary-v2.db"

    def tearDown(self):
        self.temp.cleanup()

    def test_v2_schema_stores_episode_and_v1_cache_is_not_migrated(self):
        store_v2.initialize_summary_store(self.path)
        with sqlite3.connect(self.path) as conn:
            meta = conn.execute(
                "SELECT schema_version,store_contract_version,summary_contract_version FROM summary_meta"
            ).fetchone()
            self.assertEqual(meta[0], 2)
            self.assertEqual(meta[1], store_v2.SUMMARY_STORE_CONTRACT_VERSION)
            self.assertEqual(meta[2], summary.SUMMARY_CONTRACT_VERSION_V2)

        legacy_path = self.root / "summary-v1.db"
        store_v1.initialize_summary_store(legacy_path)
        with self.assertRaises(store_v2.MemoryHierarchySummaryStoreError) as raised:
            store_v2.initialize_summary_store(legacy_path)
        self.assertEqual(raised.exception.category, "summary_cache_schema_invalid")

    def test_episode_summary_round_trip_and_stale_digest_hidden(self):
        store_v2.initialize_summary_store(self.path)
        current = plan_with_episode()
        ep = episode_node(current)
        target = summary.prepare_summary_target_v2(
            atomics(),
            current,
            ep.node_key,
        )
        derived = summary.validate_summary_clauses_v2(
            target,
            (clause((P2, P3), "The V2 authority cutover reached live deployment."),),
        )
        result = store_v2.store_summary(self.path, derived, ep)
        self.assertTrue(result.created)
        loaded = store_v2.load_current_summary(self.path, ep)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.node_type, "episode")
        self.assertEqual(loaded.support_keys, tuple(sorted((P2, P3))))

        changed = tuple(
            dataclasses.replace(
                item,
                normalized_content="The V2 authority cutover completed another production step.",
                updated_at="2026-09-01T09:00:00+00:00",
            ) if item.memory_key == P3 else item
            for item in atomics()
        )
        changed_plan = plan_with_episode(changed)
        self.assertNotEqual(ep.projection_digest, episode_node(changed_plan).projection_digest)
        self.assertIsNone(
            store_v2.load_current_summary(self.path, episode_node(changed_plan))
        )


class MemoryHierarchySummaryRebuildV2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.relay = (self.root / "relay.db").resolve()
        self.hierarchy = (self.root / "memory-hierarchy.db").resolve()
        self.cache = (self.root / "memory-hierarchy-summary-v2.db").resolve()
        self.relay.write_bytes(b"authoritative-relay-sentinel")
        self.reader = memory_hierarchy_snapshot.MemoryHierarchySnapshotReader(
            self.relay,
            fingerprint_key_id="summary-v2-key",
            fingerprint_hmac_secret="SummaryV2-HMAC-0123456789-abcdef-XYZ!",
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
                        "version": extractor_v2.EXTRACTOR_CONTRACT_VERSION,
                        "clauses": clauses,
                    },
                    separators=(",", ":"),
                )
            }
        return generate

    async def run_rebuild(self, items, provider):
        with self.patch_snapshot(items):
            return await rebuild_v2.rebuild_current_hierarchy_summaries_v2(
                self.reader,
                self.hierarchy,
                self.cache,
                provider,
                provider_model="test-model",
                provider_prompt_contract_version="test-prompt-v2",
            )

    async def test_first_run_generates_three_targets_and_clean_replay_calls_provider_zero(self):
        first_calls = []
        first = await self.run_rebuild(atomics(), self.good_provider(first_calls))
        self.assertEqual(first.status, "completed")
        self.assertEqual(first.target_count, 3)
        self.assertEqual(first.generated_count, 3)
        self.assertEqual(first.failed_count, 0)
        self.assertEqual(first.provider_call_count, 3)
        self.assertEqual(sorted(first_calls), ["canonical_state", "episode", "topic"])

        current = plan_with_episode()
        for target in (node(current, "topic"), episode_node(current), state_node(current)):
            self.assertIsNotNone(store_v2.load_current_summary(self.cache, target))

        second_calls = []
        second = await self.run_rebuild(atomics(), self.good_provider(second_calls))
        self.assertEqual(second.cache_hit_count, 3)
        self.assertEqual(second.generated_count, 0)
        self.assertEqual(second.provider_call_count, 0)
        self.assertEqual(second_calls, [])

    async def test_episode_removal_prunes_episode_and_topic_but_keeps_state(self):
        await self.run_rebuild(atomics(), self.good_provider([]))
        without = plan_without_episode()
        hierarchy_store.apply_projection_plan(self.hierarchy, without)
        calls = []
        receipt = await self.run_rebuild(atomics(), self.good_provider(calls))
        self.assertEqual(receipt.target_count, 2)
        self.assertEqual(receipt.pruned_count, 2)
        self.assertEqual(receipt.cache_hit_count, 1)
        self.assertEqual(receipt.generated_count, 1)
        self.assertEqual(receipt.provider_call_count, 1)
        self.assertEqual(calls, ["topic"])
        self.assertIsNotNone(store_v2.load_current_summary(self.cache, state_node(without)))

    async def test_non_episode_atomic_change_keeps_episode_cache_and_regenerates_topic_state(self):
        await self.run_rebuild(atomics(), self.good_provider([]))
        changed = tuple(
            dataclasses.replace(
                item,
                normalized_content="The project backend runs on Render with a persistent disk.",
                updated_at="2026-09-01T09:00:00+00:00",
            ) if item.memory_key == P1 else item
            for item in atomics()
        )
        changed_plan = plan_with_episode(changed)
        hierarchy_store.apply_projection_plan(self.hierarchy, changed_plan)
        calls = []
        receipt = await self.run_rebuild(changed, self.good_provider(calls))
        self.assertEqual(receipt.pruned_count, 2)
        self.assertEqual(receipt.cache_hit_count, 1)
        self.assertEqual(receipt.generated_count, 2)
        self.assertEqual(receipt.provider_call_count, 2)
        self.assertEqual(sorted(calls), ["canonical_state", "topic"])
        self.assertIsNotNone(
            store_v2.load_current_summary(self.cache, episode_node(changed_plan))
        )

    async def test_sensitive_non_episode_atomic_allows_episode_only_and_blocks_topic_state_pre_provider(self):
        sensitive = tuple(
            dataclasses.replace(item, sensitivity="sensitive")
            if item.memory_key == P1 else item
            for item in atomics()
        )
        hierarchy_store.apply_projection_plan(self.hierarchy, plan_with_episode(sensitive))
        calls = []
        receipt = await self.run_rebuild(sensitive, self.good_provider(calls))
        self.assertEqual(receipt.target_count, 3)
        self.assertEqual(receipt.generated_count, 1)
        self.assertEqual(receipt.failed_count, 2)
        self.assertEqual(receipt.provider_call_count, 1)
        self.assertEqual(calls, ["episode"])


if __name__ == "__main__":
    unittest.main()
