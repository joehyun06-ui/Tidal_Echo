from __future__ import annotations

import dataclasses
import unittest

from backend import memory_hierarchy_projection as hierarchy


K1 = "atomic_key_00000001"
K2 = "atomic_key_00000002"
K3 = "atomic_key_00000003"
K4 = "atomic_key_00000004"


def atomic(
    key: str,
    content: str,
    *,
    kind: str = "project",
    status: str = "active",
    updated_at: str = "2026-08-31T12:00:00+00:00",
    sensitivity: str = "normal",
):
    return hierarchy.AtomicMemoryProjectionInputV1(
        memory_key=key,
        kind=kind,
        scope_type="global_user",
        scope_ref="",
        normalized_content=content,
        fingerprint_version=1,
        status=status,
        explicitness="inferred",
        confidence=1.0,
        sensitivity=sensitivity,
        first_observed_at="2026-08-31T10:00:00+00:00",
        last_confirmed_at="2026-08-31T11:00:00+00:00",
        updated_at=updated_at,
    )


def topics():
    return (
        hierarchy.TopicGroupingV1("topic.project", (K1, K2, K3)),
        hierarchy.TopicGroupingV1("topic.user", (K4,)),
    )


def episodes():
    return (
        hierarchy.EpisodeGroupingV1(
            "episode.deploy",
            "topic.project",
            (K1, K2),
        ),
    )


def atomics():
    return (
        atomic(K1, "Project Atlas uses Python."),
        atomic(K2, "Project Atlas runs on Render."),
        atomic(K3, "Project Atlas frontend uses Vercel.", kind="decision"),
        atomic(K4, "I prefer concise status reports.", kind="user_preference"),
    )


class MemoryHierarchyProjectionTests(unittest.TestCase):
    def assert_error(self, category: str, callable_, *args, **kwargs):
        with self.assertRaises(hierarchy.MemoryHierarchyProjectionError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.category, category)

    def test_builds_topic_episode_and_canonical_state_without_copying_plaintext(self):
        plan = hierarchy.plan_hierarchy_projection_v1(
            atomics(),
            topics(),
            episodes(),
        )
        self.assertEqual(plan.contract_version, "memory-hierarchy-projection-v1")
        self.assertEqual(len(plan.nodes), 5)
        self.assertEqual(len(plan.dirty_node_keys), 5)
        self.assertEqual(plan.obsolete_node_keys, ())

        project_topic = next(
            node for node in plan.nodes
            if node.node_type == "topic" and node.node_key == "topic.project"
        )
        deploy_episode = next(
            node for node in plan.nodes
            if node.node_type == "episode" and node.node_key == "episode.deploy"
        )
        project_state = next(
            node for node in plan.nodes
            if node.node_type == "canonical_state" and node.parent_key == "topic.project"
        )
        self.assertEqual(project_topic.atomic_keys, (K1, K2, K3))
        self.assertEqual(deploy_episode.atomic_keys, (K1, K2))
        self.assertEqual(project_state.atomic_keys, (K1, K2, K3))
        self.assertEqual(deploy_episode.parent_key, "topic.project")

        rendered = repr(plan) + " " + " ".join(repr(node) for node in plan.nodes)
        self.assertNotIn("Project Atlas", rendered)
        self.assertNotIn("Python", rendered)
        self.assertNotIn("Render", rendered)
        self.assertNotIn("Vercel", rendered)

    def test_input_order_does_not_change_projection_or_receipts(self):
        first = hierarchy.plan_hierarchy_projection_v1(
            atomics(), topics(), episodes()
        )
        second = hierarchy.plan_hierarchy_projection_v1(
            tuple(reversed(atomics())),
            tuple(reversed(topics())),
            tuple(reversed(episodes())),
        )
        self.assertEqual(first.atomic_snapshot_digest, second.atomic_snapshot_digest)
        self.assertEqual(first.receipts(), second.receipts())

    def test_exact_previous_receipts_make_every_node_clean(self):
        first = hierarchy.plan_hierarchy_projection_v1(
            atomics(), topics(), episodes()
        )
        second = hierarchy.plan_hierarchy_projection_v1(
            atomics(), topics(), episodes(), previous_nodes=first.receipts()
        )
        self.assertEqual(second.dirty_node_keys, ())
        self.assertEqual(second.obsolete_node_keys, ())

    def test_atomic_revision_dirties_only_affected_episode_topic_and_state(self):
        first = hierarchy.plan_hierarchy_projection_v1(
            atomics(), topics(), episodes()
        )
        changed = list(atomics())
        changed[0] = dataclasses.replace(
            changed[0],
            normalized_content="Project Atlas uses Python 3.12.",
            updated_at="2026-08-31T13:00:00+00:00",
        )
        second = hierarchy.plan_hierarchy_projection_v1(
            changed,
            topics(),
            episodes(),
            previous_nodes=first.receipts(),
        )
        dirty = set(second.dirty_node_keys)
        self.assertIn("episode.deploy", dirty)
        self.assertIn("topic.project", dirty)
        project_state = next(
            node.node_key for node in second.nodes
            if node.node_type == "canonical_state" and node.parent_key == "topic.project"
        )
        user_state = next(
            node.node_key for node in second.nodes
            if node.node_type == "canonical_state" and node.parent_key == "topic.user"
        )
        self.assertIn(project_state, dirty)
        self.assertNotIn("topic.user", dirty)
        self.assertNotIn(user_state, dirty)

    def test_episode_membership_change_dirties_episode_and_topic_not_canonical_state(self):
        first = hierarchy.plan_hierarchy_projection_v1(
            atomics(), topics(), episodes()
        )
        changed_episode = (
            hierarchy.EpisodeGroupingV1(
                "episode.deploy",
                "topic.project",
                (K2, K3),
            ),
        )
        second = hierarchy.plan_hierarchy_projection_v1(
            atomics(),
            topics(),
            changed_episode,
            previous_nodes=first.receipts(),
        )
        dirty = set(second.dirty_node_keys)
        self.assertIn("episode.deploy", dirty)
        self.assertIn("topic.project", dirty)
        project_state = next(
            node for node in second.nodes
            if node.node_type == "canonical_state" and node.parent_key == "topic.project"
        )
        self.assertFalse(project_state.dirty)

    def test_removed_projection_nodes_are_reported_obsolete(self):
        first = hierarchy.plan_hierarchy_projection_v1(
            atomics(), topics(), episodes()
        )
        reduced_atomics = atomics()[:3]
        reduced_topics = (
            hierarchy.TopicGroupingV1("topic.project", (K1, K2, K3)),
        )
        second = hierarchy.plan_hierarchy_projection_v1(
            reduced_atomics,
            reduced_topics,
            episodes(),
            previous_nodes=first.receipts(),
        )
        obsolete = set(second.obsolete_node_keys)
        self.assertIn("topic.user", obsolete)
        prior_user_state = next(
            node.node_key for node in first.nodes
            if node.node_type == "canonical_state" and node.parent_key == "topic.user"
        )
        self.assertIn(prior_user_state, obsolete)

    def test_only_active_atomic_truth_can_enter_projection(self):
        for status in ("candidate", "rejected", "superseded", "forgotten"):
            items = list(atomics())
            items[0] = dataclasses.replace(items[0], status=status)
            self.assert_error(
                "invalid_atomics",
                hierarchy.plan_hierarchy_projection_v1,
                items,
                topics(),
                episodes(),
            )

    def test_duplicate_and_unassigned_atomics_fail_closed(self):
        self.assert_error(
            "duplicate_atomic",
            hierarchy.plan_hierarchy_projection_v1,
            atomics() + (atomics()[0],),
            topics(),
            episodes(),
        )
        self.assert_error(
            "unassigned_atomic",
            hierarchy.plan_hierarchy_projection_v1,
            atomics(),
            (hierarchy.TopicGroupingV1("topic.project", (K1, K2, K3)),),
            (),
        )
        conflicting = (
            hierarchy.TopicGroupingV1("topic.project", (K1, K2, K3)),
            hierarchy.TopicGroupingV1("topic.other", (K3, K4)),
        )
        self.assert_error(
            "topic_membership_conflict",
            hierarchy.plan_hierarchy_projection_v1,
            atomics(),
            conflicting,
            (),
        )

    def test_episode_must_be_multi_atomic_single_topic_and_non_overlapping(self):
        self.assert_error(
            "invalid_episode",
            hierarchy.plan_hierarchy_projection_v1,
            atomics(), topics(),
            (hierarchy.EpisodeGroupingV1("episode.one", "topic.project", (K1,)),),
        )
        self.assert_error(
            "episode_topic_mismatch",
            hierarchy.plan_hierarchy_projection_v1,
            atomics(), topics(),
            (hierarchy.EpisodeGroupingV1("episode.cross", "topic.project", (K1, K4)),),
        )
        overlap = (
            hierarchy.EpisodeGroupingV1("episode.one", "topic.project", (K1, K2)),
            hierarchy.EpisodeGroupingV1("episode.two", "topic.project", (K2, K3)),
        )
        self.assert_error(
            "episode_membership_conflict",
            hierarchy.plan_hierarchy_projection_v1,
            atomics(), topics(), overlap,
        )

    def test_previous_receipts_are_strict_and_duplicate_free(self):
        plan = hierarchy.plan_hierarchy_projection_v1(atomics(), topics(), episodes())
        receipt = plan.receipts()[0]
        self.assert_error(
            "duplicate_previous_node",
            hierarchy.plan_hierarchy_projection_v1,
            atomics(), topics(), episodes(),
            previous_nodes=(receipt, receipt),
        )
        bad = dataclasses.replace(receipt, projection_digest="not-a-digest")
        self.assert_error(
            "invalid_previous_nodes",
            hierarchy.plan_hierarchy_projection_v1,
            atomics(), topics(), episodes(),
            previous_nodes=(bad,),
        )

    def test_sensitive_content_affects_digest_but_never_projection_plaintext(self):
        items = list(atomics())
        items[0] = dataclasses.replace(
            items[0],
            sensitivity="restricted",
            normalized_content="Project Atlas secret architecture marker.",
        )
        plan = hierarchy.plan_hierarchy_projection_v1(items, topics(), episodes())
        self.assertNotIn("secret architecture", repr(plan))
        for node in plan.nodes:
            self.assertNotIn("secret architecture", repr(node))
            self.assertEqual(len(node.projection_digest), 64)

    def test_projection_namespace_collision_fails_closed(self):
        collision_episode = (
            hierarchy.EpisodeGroupingV1(
                "state:topic.project",
                "topic.project",
                (K1, K2),
            ),
        )
        self.assert_error(
            "invalid_groupings",
            hierarchy.plan_hierarchy_projection_v1,
            atomics(), topics(), collision_episode,
        )

    def test_empty_active_snapshot_has_empty_projection(self):
        plan = hierarchy.plan_hierarchy_projection_v1((), (), ())
        self.assertEqual(plan.nodes, ())
        self.assertEqual(plan.dirty_node_keys, ())
        self.assertEqual(plan.obsolete_node_keys, ())


if __name__ == "__main__":
    unittest.main()
