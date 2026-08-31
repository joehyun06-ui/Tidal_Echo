from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import memory_hierarchy_projection as hierarchy
from backend import memory_hierarchy_projection_store as store


K1 = "sidecar_atomic_000001"
K2 = "sidecar_atomic_000002"
K3 = "sidecar_atomic_000003"
K4 = "sidecar_atomic_000004"


def atomic(key: str, content: str, *, kind: str = "project", updated: str = "t1"):
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
        first_observed_at="first",
        last_confirmed_at="confirmed",
        updated_at=updated,
    )


def active_items():
    return (
        atomic(K1, "Sidecar Project uses Python."),
        atomic(K2, "Sidecar Project runs on Render."),
        atomic(K3, "Sidecar Project frontend uses Vercel.", kind="decision"),
        atomic(K4, "I prefer concise output.", kind="user_preference"),
    )


def grouping_topics():
    return (
        hierarchy.TopicGroupingV1("topic.project", (K1, K2, K3)),
        hierarchy.TopicGroupingV1("topic.user", (K4,)),
    )


def grouping_episodes():
    return (
        hierarchy.EpisodeGroupingV1(
            "episode.deploy",
            "topic.project",
            (K1, K2),
        ),
    )


def plan(*, previous=(), items=None, topics=None, episodes=None):
    return hierarchy.plan_hierarchy_projection_v1(
        active_items() if items is None else items,
        grouping_topics() if topics is None else topics,
        grouping_episodes() if episodes is None else episodes,
        previous_nodes=previous,
    )


class MemoryHierarchyProjectionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "memory-hierarchy.db"

    def assert_error(self, category: str, callable_, *args, **kwargs):
        with self.assertRaises(store.MemoryHierarchyProjectionStoreError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.category, category)

    def initialize(self):
        returned = store.initialize_projection_store(self.path)
        self.assertEqual(returned, self.path)

    def test_initializes_content_free_sidecar_schema(self):
        self.initialize()
        snapshot = store.load_projection_snapshot(self.path)
        self.assertEqual(snapshot.schema_version, 1)
        self.assertEqual(snapshot.contract_version, "memory-hierarchy-sidecar-v1")
        self.assertEqual(
            snapshot.projection_contract_version,
            "memory-hierarchy-projection-v1",
        )
        self.assertEqual(snapshot.generation, 0)
        self.assertEqual(snapshot.nodes, ())

        with sqlite3.connect(self.path) as conn:
            schema = " ".join(
                row[0] for row in conn.execute(
                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
                ).fetchall()
            )
        lowered = schema.lower()
        self.assertNotIn("normalized_content", lowered)
        self.assertNotIn("summary_text", lowered)
        self.assertNotIn("memory_items", lowered)
        self.assertNotIn("memory_sources", lowered)

    def test_apply_materializes_manifest_and_members_only(self):
        self.initialize()
        projection = plan()
        snapshot = store.apply_projection_plan(self.path, projection)
        self.assertEqual(snapshot.generation, 1)
        self.assertEqual(snapshot.atomic_snapshot_digest, projection.atomic_snapshot_digest)
        self.assertEqual(len(snapshot.nodes), 5)
        self.assertEqual(snapshot.member_count, 10)
        self.assertEqual(set(snapshot.dirty_node_keys), set(projection.dirty_node_keys))
        self.assertEqual(
            set(snapshot.receipts()),
            set(projection.receipts()),
        )

        raw = self.path.read_bytes()
        self.assertNotIn(b"Sidecar Project uses Python", raw)
        self.assertNotIn(b"runs on Render", raw)
        self.assertNotIn(b"Vercel", raw)
        self.assertNotIn(b"I prefer concise output", raw)

    def test_receipts_feed_clean_replan_and_second_generation(self):
        self.initialize()
        first = store.apply_projection_plan(self.path, plan())
        clean_plan = plan(previous=store.load_projection_receipts(self.path))
        self.assertEqual(clean_plan.dirty_node_keys, ())
        second = store.apply_projection_plan(self.path, clean_plan)
        self.assertEqual(first.generation, 1)
        self.assertEqual(second.generation, 2)
        self.assertEqual(second.dirty_node_keys, ())
        self.assertEqual(first.receipts(), second.receipts())

    def test_changed_atomic_updates_only_planned_dirty_nodes(self):
        self.initialize()
        first = store.apply_projection_plan(self.path, plan())
        changed = list(active_items())
        changed[0] = dataclasses.replace(
            changed[0],
            normalized_content="Sidecar Project uses Python 3.12.",
            updated_at="t2",
        )
        changed_plan = plan(previous=first.receipts(), items=changed)
        second = store.apply_projection_plan(self.path, changed_plan)
        dirty = set(second.dirty_node_keys)
        self.assertIn("episode.deploy", dirty)
        self.assertIn("topic.project", dirty)
        project_state = next(
            node.node_key for node in second.nodes
            if node.node_type == "canonical_state" and node.parent_key == "topic.project"
        )
        self.assertIn(project_state, dirty)
        self.assertNotIn("topic.user", dirty)

    def test_removed_nodes_and_members_do_not_survive_complete_snapshot(self):
        self.initialize()
        first = store.apply_projection_plan(self.path, plan())
        reduced_items = active_items()[:3]
        reduced_topics = (
            hierarchy.TopicGroupingV1("topic.project", (K1, K2, K3)),
        )
        reduced_plan = plan(
            previous=first.receipts(),
            items=reduced_items,
            topics=reduced_topics,
            episodes=grouping_episodes(),
        )
        second = store.apply_projection_plan(self.path, reduced_plan)
        keys = {node.node_key for node in second.nodes}
        self.assertNotIn("topic.user", keys)
        self.assertEqual(len(second.nodes), 3)
        with sqlite3.connect(self.path) as conn:
            member_keys = {
                row[0] for row in conn.execute(
                    "SELECT DISTINCT memory_key FROM projection_members"
                ).fetchall()
            }
        self.assertNotIn(K4, member_keys)

    def test_mid_transaction_failure_rolls_back_previous_snapshot(self):
        self.initialize()
        first = store.apply_projection_plan(self.path, plan())
        changed = list(active_items())
        changed[0] = dataclasses.replace(
            changed[0],
            normalized_content="Sidecar Project uses Python 3.12.",
            updated_at="t2",
        )
        changed_plan = plan(previous=first.receipts(), items=changed)
        original = store._write_node_members
        calls = []

        def explode(conn, node, generation):
            original(conn, node, generation)
            calls.append(node.node_key)
            if len(calls) == 1:
                raise RuntimeError("synthetic sidecar interruption")

        with mock.patch.object(store, "_write_node_members", new=explode):
            self.assert_error(
                "projection_write_failed",
                store.apply_projection_plan,
                self.path,
                changed_plan,
            )
        after = store.load_projection_snapshot(self.path)
        self.assertEqual(after.generation, first.generation)
        self.assertEqual(after.atomic_snapshot_digest, first.atomic_snapshot_digest)
        self.assertEqual(after.receipts(), first.receipts())

    def test_deleting_sidecar_and_rebuilding_reproduces_receipts(self):
        self.initialize()
        projection = plan()
        first = store.apply_projection_plan(self.path, projection)
        first_receipts = first.receipts()
        self.path.unlink()
        self.initialize()
        rebuilt = store.apply_projection_plan(self.path, projection)
        self.assertEqual(rebuilt.generation, 1)
        self.assertEqual(rebuilt.receipts(), first_receipts)
        self.assertEqual(rebuilt.atomic_snapshot_digest, first.atomic_snapshot_digest)

    def test_sidecar_never_opens_or_mutates_authoritative_relay_file(self):
        relay = self.root / "relay.db"
        relay.write_bytes(b"authoritative-memory-ledger-sentinel")
        before = hashlib.sha256(relay.read_bytes()).digest()
        self.initialize()
        store.apply_projection_plan(self.path, plan())
        after = hashlib.sha256(relay.read_bytes()).digest()
        self.assertEqual(before, after)
        self.assertEqual(relay.read_bytes(), b"authoritative-memory-ledger-sentinel")

    def test_existing_corrupt_sidecar_is_not_silently_repaired(self):
        self.initialize()
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE projection_meta SET contract_version='wrong-contract'"
            )
            conn.commit()
        self.assert_error(
            "projection_schema_invalid",
            store.initialize_projection_store,
            self.path,
        )
        self.assert_error(
            "projection_schema_invalid",
            store.load_projection_snapshot,
            self.path,
        )

    def test_partial_or_foreign_sqlite_file_is_not_adopted(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY)")
            conn.commit()
        self.assert_error(
            "projection_schema_invalid",
            store.initialize_projection_store,
            self.path,
        )
        with sqlite3.connect(self.path) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertEqual(tables, {"unrelated"})

    def test_apply_rejects_non_planner_objects_before_writing(self):
        self.initialize()
        before = self.path.read_bytes()
        self.assert_error(
            "invalid_projection_plan",
            store.apply_projection_plan,
            self.path,
            {"nodes": []},
        )
        after = self.path.read_bytes()
        self.assertEqual(before, after)

    def test_plan_structure_is_revalidated_at_storage_boundary(self):
        self.initialize()
        valid = plan()
        topic = next(node for node in valid.nodes if node.node_type == "topic")
        forged_topic = dataclasses.replace(topic, parent_key="forged.parent")
        forged = dataclasses.replace(
            valid,
            nodes=tuple(
                forged_topic if node.node_key == topic.node_key else node
                for node in valid.nodes
            ),
        )
        self.assert_error(
            "invalid_projection_plan",
            store.apply_projection_plan,
            self.path,
            forged,
        )

    def test_read_path_detects_member_ordinal_corruption(self):
        self.initialize()
        snapshot = store.apply_projection_plan(self.path, plan())
        target = next(node for node in snapshot.nodes if len(node.atomic_keys) >= 2)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE projection_members SET ordinal=99 WHERE node_key=? AND ordinal=1",
                (target.node_key,),
            )
            conn.commit()
        self.assert_error(
            "projection_schema_invalid",
            store.load_projection_snapshot,
            self.path,
        )


if __name__ == "__main__":
    unittest.main()
