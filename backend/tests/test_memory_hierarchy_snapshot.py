from __future__ import annotations

import dataclasses
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import (
    memory_hierarchy_baseline as baseline,
    memory_hierarchy_projection_store as projection_store,
    memory_hierarchy_rebuild as rebuild,
    memory_hierarchy_snapshot as snapshot,
    memory_policy,
)
from backend.tests._support import load_app


SECRET = "Synthetic-Hierarchy-Snapshot-HMAC-Key-2026!Z9q7"
KEY_ID = "hierarchy-snapshot-test-key"
STAMP = "2026-08-31T12:00:00+00:00"


def memory_key(index: int) -> str:
    return "h" + f"{index:031d}"


class MemoryHierarchySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(
            self.temp.name,
            telegram=False,
            memory=True,
            memory_writes=True,
            memory_secret=SECRET,
        )
        self.db_path = Path(self.module.DB_PATH).resolve()
        self.sidecar = Path(self.temp.name) / "memory-hierarchy.db"
        self._seed_profile()

    def _seed_profile(self):
        with self.module.db() as conn:
            row = conn.execute(
                "SELECT count(*) FROM memory_fingerprint_profile"
            ).fetchone()
            if int(row[0]) == 0:
                conn.execute(
                    """INSERT INTO memory_fingerprint_profile
                       (singleton,key_id,key_check,normalization_version,
                        fingerprint_version,created_at,updated_at)
                       VALUES(1,?,?,?,?,?,?)""",
                    (
                        KEY_ID,
                        memory_policy.fingerprint_profile_check(SECRET),
                        memory_policy.NORMALIZATION_VERSION,
                        memory_policy.FINGERPRINT_VERSION,
                        STAMP,
                        STAMP,
                    ),
                )

    def _seed_item(
        self,
        index: int,
        *,
        kind: str,
        content: str,
        status: str = "active",
        sensitivity: str = "normal",
        explicitness: str = "explicit",
        confidence: float = 1.0,
        updated_at: str = STAMP,
    ) -> str:
        policy = memory_policy.MemoryPolicy(
            max_item_chars=1000,
            sensitive_storage_enabled=True,
        )
        normalized = policy.validate_content(content, sensitivity)
        key = memory_key(index)
        fingerprint = memory_policy.fingerprint_content(
            SECRET,
            scope_type="global_user",
            scope_ref="",
            kind=kind,
            normalized_content=normalized,
        )
        with self.module.db() as conn:
            conn.execute(
                """INSERT INTO memory_items
                   (memory_key,kind,scope_type,scope_ref,normalized_content,
                    normalized_fingerprint,fingerprint_version,status,
                    explicitness,confidence,sensitivity,first_observed_at,
                    last_confirmed_at,superseded_by_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)""",
                (
                    key,
                    kind,
                    "global_user",
                    "",
                    normalized,
                    fingerprint,
                    memory_policy.FINGERPRINT_VERSION,
                    status,
                    explicitness,
                    confidence,
                    sensitivity,
                    STAMP,
                    STAMP,
                    STAMP,
                    updated_at,
                ),
            )
        return key

    def reader(self, *, secret: str = SECRET, sensitive: bool = False):
        return snapshot.MemoryHierarchySnapshotReader(
            self.db_path,
            fingerprint_key_id=KEY_ID,
            fingerprint_hmac_secret=secret,
            max_item_chars=1000,
            sensitive_storage_enabled=sensitive,
        )

    def assert_snapshot_error(self, category: str, callable_, *args, **kwargs):
        with self.assertRaises(snapshot.MemoryHierarchySnapshotError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.category, category)

    def assert_rebuild_error(self, category: str, callable_, *args, **kwargs):
        with self.assertRaises(rebuild.MemoryHierarchyRebuildError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.category, category)

    def test_complete_mode_ro_snapshot_maps_only_active_authoritative_rows(self):
        first = self._seed_item(
            1,
            kind="project",
            content="Hierarchy project uses Python.",
        )
        second = self._seed_item(
            2,
            kind="user_preference",
            content="I prefer concise status reports.",
        )
        self._seed_item(
            3,
            kind="project",
            content="Pending candidate is excluded.",
            status="candidate",
            explicitness="inferred",
            confidence=0.0,
        )
        before = hashlib.sha256(self.db_path.read_bytes()).digest()
        with mock.patch.object(
            snapshot.channel_store,
            "connect",
            side_effect=AssertionError("write-capable connect is forbidden"),
        ):
            result = self.reader().load_active_snapshot()
        after = hashlib.sha256(self.db_path.read_bytes()).digest()
        self.assertEqual(before, after)
        self.assertEqual(result.count, 2)
        self.assertEqual(
            tuple(item.memory_key for item in result.atomics),
            (first, second),
        )
        self.assertTrue(all(item.status == "active" for item in result.atomics))
        self.assertNotIn("Hierarchy project", repr(result))
        self.assertNotIn("concise", repr(result))

    def test_profile_mismatch_fails_closed(self):
        with self.module.db() as conn:
            conn.execute(
                "UPDATE memory_fingerprint_profile SET key_id='wrong-key' WHERE singleton=1"
            )
        self.assert_snapshot_error(
            "hierarchy_snapshot_profile_mismatch",
            self.reader().load_active_snapshot,
        )

    def test_schema_corruption_fails_before_snapshot(self):
        with self.module.db() as conn:
            conn.execute("DROP TABLE memory_fingerprint_profile")
        self.assert_snapshot_error(
            "hierarchy_snapshot_schema_invalid",
            self.reader().load_active_snapshot,
        )

    def test_active_fingerprint_corruption_fails_closed(self):
        key = self._seed_item(
            1,
            kind="project",
            content="Hierarchy project runs on Render.",
        )
        with self.module.db() as conn:
            conn.execute(
                "UPDATE memory_items SET normalized_fingerprint=? WHERE memory_key=?",
                (b"x" * 32, key),
            )
        self.assert_snapshot_error(
            "hierarchy_snapshot_state_invalid",
            self.reader().load_active_snapshot,
        )

    def test_sensitive_active_memory_requires_matching_internal_policy(self):
        self._seed_item(
            1,
            kind="project",
            content="Restricted project datum.",
            sensitivity="restricted",
        )
        self.assert_snapshot_error(
            "hierarchy_snapshot_state_invalid",
            self.reader(sensitive=False).load_active_snapshot,
        )
        allowed = self.reader(sensitive=True).load_active_snapshot()
        self.assertEqual(allowed.count, 1)
        self.assertEqual(allowed.atomics[0].sensitivity, "restricted")

    def test_more_than_planner_bound_fails_instead_of_truncating(self):
        with self.module.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                policy = memory_policy.MemoryPolicy(
                    max_item_chars=1000,
                    sensitive_storage_enabled=False,
                )
                for index in range(1, 258):
                    content = policy.validate_content(
                        f"Project item {index} uses Python.",
                        "normal",
                    )
                    fingerprint = memory_policy.fingerprint_content(
                        SECRET,
                        scope_type="global_user",
                        scope_ref="",
                        kind="project",
                        normalized_content=content,
                    )
                    conn.execute(
                        """INSERT INTO memory_items
                           (memory_key,kind,scope_type,scope_ref,normalized_content,
                            normalized_fingerprint,fingerprint_version,status,
                            explicitness,confidence,sensitivity,first_observed_at,
                            last_confirmed_at,superseded_by_id,created_at,updated_at)
                           VALUES(?, 'project','global_user','',?,?,?,'active',
                                  'explicit',1.0,'normal',?,?,NULL,?,?)""",
                        (
                            memory_key(index),
                            content,
                            fingerprint,
                            memory_policy.FINGERPRINT_VERSION,
                            STAMP,
                            STAMP,
                            STAMP,
                            STAMP,
                        ),
                    )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        self.assert_snapshot_error(
            "too_many_active_memories",
            self.reader().load_active_snapshot,
        )

    def test_baseline_groups_all_supported_kinds_without_episodes(self):
        kinds = (
            "project",
            "decision",
            "task_or_progress",
            "user_profile",
            "user_preference",
            "relationship",
            "shared_episode",
            "assistant_experience",
        )
        for index, kind in enumerate(kinds, 1):
            self._seed_item(
                index,
                kind=kind,
                content=f"Durable memory content for {kind}.",
            )
        active = self.reader().load_active_snapshot()
        topics = baseline.group_baseline_topics_v1(active.atomics)
        membership = {
            topic.topic_key: topic.atomic_keys
            for topic in topics
        }
        self.assertEqual(set(membership), {
            "topic.project",
            "topic.user",
            "topic.relationship",
            "topic.assistant",
        })
        self.assertEqual(len(membership["topic.project"]), 3)
        self.assertEqual(len(membership["topic.user"]), 2)
        self.assertEqual(len(membership["topic.relationship"]), 2)
        self.assertEqual(len(membership["topic.assistant"]), 1)
        plan = baseline.build_baseline_hierarchy_plan_v1(active.atomics)
        self.assertEqual(len(plan.nodes), 8)
        self.assertFalse(any(node.node_type == "episode" for node in plan.nodes))

    def test_baseline_rebuild_materializes_sidecar_without_touching_authority(self):
        self._seed_item(
            1,
            kind="project",
            content="Hierarchy project uses Python.",
        )
        self._seed_item(
            2,
            kind="decision",
            content="Hierarchy project uses SQLite.",
        )
        self._seed_item(
            3,
            kind="user_preference",
            content="I prefer concise project updates.",
        )
        before = hashlib.sha256(self.db_path.read_bytes()).digest()
        receipt = rebuild.rebuild_baseline_hierarchy_v1(
            self.reader(),
            self.sidecar,
        )
        after = hashlib.sha256(self.db_path.read_bytes()).digest()
        self.assertEqual(before, after)
        self.assertEqual(receipt.generation, 1)
        self.assertEqual(receipt.atomic_count, 3)
        self.assertEqual(receipt.topic_count, 2)
        self.assertEqual(receipt.node_count, 4)
        self.assertEqual(receipt.dirty_node_count, 4)
        sidecar_bytes = self.sidecar.read_bytes()
        self.assertNotIn(b"Hierarchy project", sidecar_bytes)
        self.assertNotIn(b"concise", sidecar_bytes)

    def test_exact_second_rebuild_is_clean_and_atomic_change_dirties_one_topic_pair(self):
        key = self._seed_item(
            1,
            kind="project",
            content="Hierarchy project uses Python.",
        )
        first = rebuild.rebuild_baseline_hierarchy_v1(self.reader(), self.sidecar)
        second = rebuild.rebuild_baseline_hierarchy_v1(self.reader(), self.sidecar)
        self.assertEqual(first.generation, 1)
        self.assertEqual(second.generation, 2)
        self.assertEqual(second.dirty_node_count, 0)

        content = "Hierarchy project uses Python 3.12."
        fingerprint = memory_policy.fingerprint_content(
            SECRET,
            scope_type="global_user",
            scope_ref="",
            kind="project",
            normalized_content=content,
        )
        with self.module.db() as conn:
            conn.execute(
                """UPDATE memory_items
                      SET normalized_content=?,normalized_fingerprint=?,updated_at=?
                    WHERE memory_key=?""",
                (content, fingerprint, "2026-08-31T13:00:00+00:00", key),
            )
        third = rebuild.rebuild_baseline_hierarchy_v1(self.reader(), self.sidecar)
        self.assertEqual(third.generation, 3)
        self.assertEqual(third.dirty_node_count, 2)
        stored = projection_store.load_projection_snapshot(self.sidecar)
        dirty = set(stored.dirty_node_keys)
        self.assertEqual(len(dirty), 2)
        self.assertIn("topic.project", dirty)
        self.assertTrue(any(key.startswith("state:topic.project") for key in dirty))

    def test_source_failure_does_not_create_sidecar(self):
        broken_reader = snapshot.MemoryHierarchySnapshotReader(
            self.db_path,
            fingerprint_key_id=KEY_ID,
            fingerprint_hmac_secret="Different-Strong-Hierarchy-Snapshot-Key-2026!X8w6",
            max_item_chars=1000,
            sensitive_storage_enabled=False,
        )
        self.assertFalse(self.sidecar.exists())
        self.assert_rebuild_error(
            "hierarchy_rebuild_failed",
            rebuild.rebuild_baseline_hierarchy_v1,
            broken_reader,
            self.sidecar,
        )
        self.assertFalse(self.sidecar.exists())

    def test_sidecar_path_may_not_alias_authoritative_database(self):
        self._seed_item(
            1,
            kind="project",
            content="Hierarchy project uses Python.",
        )
        before = hashlib.sha256(self.db_path.read_bytes()).digest()
        self.assert_rebuild_error(
            "hierarchy_rebuild_configuration_invalid",
            rebuild.rebuild_baseline_hierarchy_v1,
            self.reader(),
            self.db_path,
        )
        after = hashlib.sha256(self.db_path.read_bytes()).digest()
        self.assertEqual(before, after)

    def test_empty_authority_snapshot_builds_empty_sidecar(self):
        result = self.reader().load_active_snapshot()
        self.assertEqual(result.count, 0)
        receipt = rebuild.rebuild_baseline_hierarchy_v1(self.reader(), self.sidecar)
        self.assertEqual(receipt.atomic_count, 0)
        self.assertEqual(receipt.topic_count, 0)
        self.assertEqual(receipt.node_count, 0)
        stored = projection_store.load_projection_snapshot(self.sidecar)
        self.assertEqual(stored.nodes, ())


if __name__ == "__main__":
    unittest.main()
