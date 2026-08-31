from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import (
    memory_hierarchy_episode_refinement as episode,
    memory_hierarchy_projection as hierarchy,
    memory_hierarchy_summary as summary,
    memory_hierarchy_summary_store as store,
)


P1 = "summary_store_atomic_00000000000001"
P2 = "summary_store_atomic_00000000000002"
P3 = "summary_store_atomic_00000000000003"


def atomic(key: str, kind: str, content: str, *, updated: str = "2026-08-31T12:00:00+00:00"):
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
        sensitivity="normal",
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
        hierarchy.TopicGroupingV1("topic.project", tuple(sorted((P1, P2, P3)))),
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
    return next(
        node for node in plan.nodes
        if node.node_type == "topic" and node.node_key == "topic.project"
    )


def state_node(plan):
    return next(
        node for node in plan.nodes
        if node.node_type == "canonical_state" and node.parent_key == "topic.project"
    )


def clauses(*, variant: str = "base"):
    second = (
        "Web Memory formation uses V2 authority and the cutover is live."
        if variant == "base"
        else "V2 Web Memory authority remains live after the cutover."
    )
    return (
        summary.SummaryClauseProposalV1(
            (P1,),
            "The project backend runs on Render.",
        ),
        summary.SummaryClauseProposalV1(
            tuple(sorted((P2, P3))),
            second,
        ),
    )


def derived_for(items, plan, node, *, variant: str = "base"):
    target = summary.prepare_summary_target_v1(items, plan, node.node_key)
    return summary.validate_summary_clauses_v1(target, clauses(variant=variant))


class MemoryHierarchySummaryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / "memory-hierarchy-summary.db"
        self.relay = self.root / "relay.db"
        self.hierarchy = self.root / "memory-hierarchy.db"
        self.relay.write_bytes(b"authoritative-relay-sentinel")
        self.hierarchy.write_bytes(b"content-free-hierarchy-sentinel")
        store.initialize_summary_store(
            self.cache,
            forbidden_paths=(self.relay, self.hierarchy),
        )

    def tearDown(self):
        self.temp.cleanup()

    def assert_store_error(self, category: str, callable_, *args, **kwargs):
        with self.assertRaises(store.MemoryHierarchySummaryStoreError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.category, category)

    def test_initializes_separate_strict_summary_schema(self):
        with sqlite3.connect(self.cache) as conn:
            tables = tuple(
                row[0]
                for row in conn.execute(
                    """SELECT name FROM sqlite_master
                         WHERE type='table' AND name NOT LIKE 'sqlite_%'
                         ORDER BY name"""
                )
            )
            self.assertEqual(
                tables,
                (
                    "node_summaries",
                    "summary_clauses",
                    "summary_meta",
                    "summary_support",
                ),
            )
            meta = conn.execute("SELECT * FROM summary_meta").fetchone()
            self.assertEqual(meta[1], store.SUMMARY_STORE_SCHEMA_VERSION)
            self.assertEqual(meta[2], store.SUMMARY_STORE_CONTRACT_VERSION)
            self.assertEqual(meta[3], summary.SUMMARY_CONTRACT_VERSION)
            self.assertEqual(meta[4], 0)
        self.assertEqual(self.relay.read_bytes(), b"authoritative-relay-sentinel")
        self.assertEqual(
            self.hierarchy.read_bytes(),
            b"content-free-hierarchy-sentinel",
        )

    def test_store_contains_expected_derived_text_but_other_databases_do_not_change(self):
        relay_before = self.relay.read_bytes()
        hierarchy_before = self.hierarchy.read_bytes()
        current_plan = plan_with_episode()
        node = topic_node(current_plan)
        derived = derived_for(atomics(), current_plan, node)

        result = store.store_summary(self.cache, derived, node)
        self.assertTrue(result.created)
        self.assertFalse(result.replayed)
        cached = store.load_current_summary(self.cache, node)
        self.assertIsNotNone(cached)
        self.assertIn("Render", cached.text)
        self.assertIn("V2", cached.text)
        self.assertEqual(cached.support_keys, (P1, P2, P3))
        self.assertNotIn("Render", repr(cached))
        self.assertEqual(self.relay.read_bytes(), relay_before)
        self.assertEqual(self.hierarchy.read_bytes(), hierarchy_before)
        self.assertIn(b"Render", self.cache.read_bytes())

    def test_exact_replay_is_zero_write_and_replacement_increments_generation(self):
        current_plan = plan_with_episode()
        node = topic_node(current_plan)
        first_summary = derived_for(atomics(), current_plan, node)
        first = store.store_summary(self.cache, first_summary, node)
        replay = store.store_summary(self.cache, first_summary, node)
        self.assertEqual(replay.generation, first.generation)
        self.assertTrue(replay.replayed)
        self.assertFalse(replay.created)
        self.assertFalse(replay.replaced)

        replacement_summary = derived_for(
            atomics(),
            current_plan,
            node,
            variant="replacement",
        )
        replacement = store.store_summary(
            self.cache,
            replacement_summary,
            node,
        )
        self.assertEqual(replacement.generation, first.generation + 1)
        self.assertTrue(replacement.replaced)
        self.assertFalse(replacement.created)
        self.assertFalse(replacement.replayed)
        cached = store.load_current_summary(self.cache, node)
        self.assertEqual(cached.summary_digest, replacement_summary.summary_digest)
        self.assertIn("remains live", cached.text)

    def test_stale_projection_digest_returns_no_text(self):
        with_episode = plan_with_episode()
        without_episode = plan_without_episode()
        old_node = topic_node(with_episode)
        new_node = topic_node(without_episode)
        self.assertNotEqual(old_node.projection_digest, new_node.projection_digest)
        derived = derived_for(atomics(), with_episode, old_node)
        store.store_summary(self.cache, derived, old_node)

        self.assertIsNotNone(store.load_current_summary(self.cache, old_node))
        self.assertIsNone(store.load_current_summary(self.cache, new_node))

    def test_episode_regrouping_invalidates_topic_cache_but_not_state_cache(self):
        with_episode = plan_with_episode()
        without_episode = plan_without_episode()
        old_topic = topic_node(with_episode)
        old_state = state_node(with_episode)
        new_topic = topic_node(without_episode)
        new_state = state_node(without_episode)
        self.assertNotEqual(old_topic.projection_digest, new_topic.projection_digest)
        self.assertEqual(old_state.projection_digest, new_state.projection_digest)

        store.store_summary(
            self.cache,
            derived_for(atomics(), with_episode, old_topic),
            old_topic,
        )
        store.store_summary(
            self.cache,
            derived_for(atomics(), with_episode, old_state),
            old_state,
        )

        self.assertIsNone(store.load_current_summary(self.cache, new_topic))
        self.assertIsNotNone(store.load_current_summary(self.cache, new_state))
        pruned = store.prune_stale_summaries(
            self.cache,
            (new_topic, new_state),
        )
        self.assertEqual(pruned.removed_count, 1)
        self.assertIsNotNone(store.load_current_summary(self.cache, new_state))

    def test_atomic_change_invalidates_both_topic_and_state_cache(self):
        old_plan = plan_with_episode()
        old_topic = topic_node(old_plan)
        old_state = state_node(old_plan)
        store.store_summary(
            self.cache,
            derived_for(atomics(), old_plan, old_topic),
            old_topic,
        )
        store.store_summary(
            self.cache,
            derived_for(atomics(), old_plan, old_state),
            old_state,
        )

        changed = tuple(
            dataclasses.replace(
                item,
                normalized_content="The project backend runs on a changed platform.",
                updated_at="2026-09-01T00:00:00+00:00",
            ) if item.memory_key == P1 else item
            for item in atomics()
        )
        new_plan = plan_with_episode(changed)
        self.assertIsNone(
            store.load_current_summary(self.cache, topic_node(new_plan))
        )
        self.assertIsNone(
            store.load_current_summary(self.cache, state_node(new_plan))
        )

    def test_store_rejects_stale_forged_digest_or_support_binding(self):
        current_plan = plan_with_episode()
        current_node = topic_node(current_plan)
        derived = derived_for(atomics(), current_plan, current_node)
        without_episode = plan_without_episode()
        self.assert_store_error(
            "invalid_summary_cache_entry",
            store.store_summary,
            self.cache,
            derived,
            topic_node(without_episode),
        )
        self.assert_store_error(
            "invalid_summary_cache_entry",
            store.store_summary,
            self.cache,
            dataclasses.replace(derived, summary_digest="0" * 64),
            current_node,
        )
        forged_clauses = (
            summary.SummaryClauseProposalV1((P1,), "The project backend runs on Render."),
            summary.SummaryClauseProposalV1((P2,), "V2 Web Memory authority is live."),
        )
        self.assert_store_error(
            "invalid_summary_cache_entry",
            store.store_summary,
            self.cache,
            dataclasses.replace(derived, clauses=forged_clauses),
            current_node,
        )

    def test_mid_transaction_failure_preserves_previous_summary(self):
        current_plan = plan_with_episode()
        node = topic_node(current_plan)
        original = derived_for(atomics(), current_plan, node)
        replacement = derived_for(
            atomics(),
            current_plan,
            node,
            variant="replacement",
        )
        first = store.store_summary(self.cache, original, node)

        with mock.patch.object(
            store,
            "_before_summary_commit",
            side_effect=RuntimeError("simulated crash"),
        ):
            self.assert_store_error(
                "summary_cache_write_failed",
                store.store_summary,
                self.cache,
                replacement,
                node,
            )
        cached = store.load_current_summary(self.cache, node)
        self.assertEqual(cached.summary_digest, original.summary_digest)
        self.assertEqual(cached.generation, first.generation)
        self.assertNotIn("remains live", cached.text)

    def test_clause_and_support_ordinal_corruption_fails_closed(self):
        current_plan = plan_with_episode()
        node = topic_node(current_plan)
        derived = derived_for(atomics(), current_plan, node)
        store.store_summary(self.cache, derived, node)

        with sqlite3.connect(self.cache) as conn:
            conn.execute(
                """UPDATE summary_support SET support_ordinal=7
                     WHERE node_key=? AND clause_ordinal=0 AND support_ordinal=0""",
                (node.node_key,),
            )
            conn.commit()
        self.assert_store_error(
            "summary_cache_schema_invalid",
            store.load_current_summary,
            self.cache,
            node,
        )

    def test_delete_and_rebuild_reproduces_same_derived_summary(self):
        current_plan = plan_with_episode()
        node = topic_node(current_plan)
        derived = derived_for(atomics(), current_plan, node)
        store.store_summary(self.cache, derived, node)
        before = store.load_current_summary(self.cache, node)
        self.cache.unlink()
        store.initialize_summary_store(
            self.cache,
            forbidden_paths=(self.relay, self.hierarchy),
        )
        rebuilt_write = store.store_summary(self.cache, derived, node)
        rebuilt = store.load_current_summary(self.cache, node)
        self.assertEqual(rebuilt_write.generation, 1)
        self.assertEqual(before.summary_digest, rebuilt.summary_digest)
        self.assertEqual(before.text, rebuilt.text)
        self.assertEqual(before.support_keys, rebuilt.support_keys)

    def test_path_aliases_are_rejected_before_opening_authority_or_hierarchy_files(self):
        relay_before = self.relay.read_bytes()
        hierarchy_before = self.hierarchy.read_bytes()
        self.assert_store_error(
            "invalid_summary_store_path",
            store.initialize_summary_store,
            self.relay,
            forbidden_paths=(self.relay, self.hierarchy),
        )
        self.assert_store_error(
            "invalid_summary_store_path",
            store.initialize_summary_store,
            self.hierarchy,
            forbidden_paths=(self.relay, self.hierarchy),
        )
        self.assertEqual(self.relay.read_bytes(), relay_before)
        self.assertEqual(self.hierarchy.read_bytes(), hierarchy_before)

    def test_foreign_database_is_rejected_not_repaired(self):
        foreign = self.root / "foreign-summary.db"
        with sqlite3.connect(foreign) as conn:
            conn.execute("CREATE TABLE unrelated(value TEXT)")
        self.assert_store_error(
            "summary_cache_schema_invalid",
            store.initialize_summary_store,
            foreign,
            forbidden_paths=(self.relay, self.hierarchy),
        )
        with sqlite3.connect(foreign) as conn:
            tables = tuple(
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            )
        self.assertEqual(tables, ("unrelated",))

    def test_prune_missing_node_removes_cache_without_touching_other_files(self):
        relay_before = self.relay.read_bytes()
        hierarchy_before = self.hierarchy.read_bytes()
        current_plan = plan_with_episode()
        node = topic_node(current_plan)
        store.store_summary(
            self.cache,
            derived_for(atomics(), current_plan, node),
            node,
        )
        result = store.prune_stale_summaries(self.cache, ())
        self.assertEqual(result.removed_count, 1)
        self.assertIsNone(store.load_current_summary(self.cache, node))
        self.assertEqual(self.relay.read_bytes(), relay_before)
        self.assertEqual(self.hierarchy.read_bytes(), hierarchy_before)


if __name__ == "__main__":
    unittest.main()
