from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import (
    memory_hierarchy_baseline,
    memory_hierarchy_episode_refinement as episode_refinement,
    memory_hierarchy_projection as hierarchy,
    memory_hierarchy_projection_store as hierarchy_store,
    memory_hierarchy_snapshot,
    memory_retrieval_bm25 as bm25,
    memory_retrieval_bm25_store as bm25_store,
    memory_retrieval_hierarchy_routing as routing,
    memory_retrieval_hierarchy_source as source,
)


SECRET = "Hierarchy-Routing-HMAC-0123456789-AbCd!"
KEY_ID = "hierarchy-routing-key-v1"

P1 = "hierarchy_route_atomic_000001"
P2 = "hierarchy_route_atomic_000002"
P3 = "hierarchy_route_atomic_000003"
P4 = "hierarchy_route_atomic_000004"
U1 = "hierarchy_route_atomic_000005"
S1 = "hierarchy_route_atomic_000006"
PS1 = "hierarchy_route_atomic_000007"
UNKNOWN = "hierarchy_route_atomic_999999"


def atomic(
    key: str,
    kind: str,
    content: str,
    *,
    sensitivity: str = "normal",
    scope_type: str = "global_user",
    scope_ref: str = "",
):
    return hierarchy.AtomicMemoryProjectionInputV1(
        memory_key=key,
        kind=kind,
        scope_type=scope_type,
        scope_ref=scope_ref,
        normalized_content=content,
        fingerprint_version=1,
        status="active",
        explicitness="inferred",
        confidence=1.0,
        sensitivity=sensitivity,
        first_observed_at="2026-09-01T08:00:00+00:00",
        last_confirmed_at="2026-09-01T08:00:00+00:00",
        updated_at="2026-09-01T08:00:00+00:00",
    )


def atomics():
    return (
        atomic(P1, "decision", "V2 cutover authority decision."),
        atomic(P2, "task_or_progress", "V2 deployment completed successfully."),
        atomic(P3, "project", "Backend runs on Render."),
        atomic(P4, "project", "Frontend runs on Vercel."),
        atomic(U1, "user_preference", "Prefer concise progress updates."),
        atomic(
            S1,
            "project",
            "Sensitive project sibling must never route.",
            sensitivity="sensitive",
        ),
        atomic(
            PS1,
            "project",
            "Project-scoped sibling must never route.",
            scope_type="project",
            scope_ref="tidal-echo",
        ),
    )


def current_plan(items=None):
    values = atomics() if items is None else items
    topics = memory_hierarchy_baseline.group_baseline_topics_v1(values)
    episodes = episode_refinement.refine_episodes_v1(
        values,
        topics,
        (episode_refinement.EpisodeMembershipProposalV1((P1, P2)),),
    ).episodes
    return hierarchy.plan_hierarchy_projection_v1(values, topics, episodes)


def bm25_plan(items=None):
    values = atomics() if items is None else items
    digest = memory_hierarchy_baseline.build_baseline_hierarchy_plan_v1(
        values
    ).atomic_snapshot_digest
    return bm25.build_bm25_index_v1(
        values,
        source_snapshot_digest=digest,
        term_key_id=KEY_ID,
        term_hmac_secret=SECRET,
    )


def bm25_result(query: str, items=None):
    return bm25.search_bm25_index_v1(
        bm25_plan(items),
        query,
        term_key_id=KEY_ID,
        term_hmac_secret=SECRET,
    )


class HierarchyRoutingContractTests(unittest.TestCase):
    def assert_error(self, category: str, callable_, *args, **kwargs):
        with self.assertRaises(routing.MemoryRetrievalHierarchyRoutingError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.category, category)

    def test_bm25_seed_expands_episode_then_topic_and_excludes_unsafe_siblings(self):
        result = routing.route_hierarchy_candidates_v1(
            atomics(),
            current_plan(),
            bm25_result("cutover"),
        )
        routed = tuple((item.memory_key, item.route_kind) for item in result.items)
        self.assertEqual(
            routed,
            (
                (P1, "bm25_seed"),
                (P2, "episode_neighbor"),
                (P3, "topic_neighbor"),
                (P4, "topic_neighbor"),
            ),
        )
        keys = {item.memory_key for item in result.items}
        self.assertNotIn(S1, keys)
        self.assertNotIn(PS1, keys)
        self.assertNotIn(U1, keys)
        self.assertEqual(result.seed_count, 1)
        self.assertEqual(result.episode_neighbor_count, 1)
        self.assertEqual(result.topic_neighbor_count, 2)

    def test_multiple_bm25_seeds_raise_topic_neighbor_support_without_duplicate_routes(self):
        result = routing.route_hierarchy_candidates_v1(
            atomics(),
            current_plan(),
            bm25_result("cutover deployment"),
        )
        self.assertEqual(
            tuple(item.memory_key for item in result.items[:2]),
            (P1, P2),
        )
        self.assertTrue(
            all(item.route_kind == "bm25_seed" for item in result.items[:2])
        )
        project_neighbors = {
            item.memory_key: item
            for item in result.items
            if item.route_kind == "topic_neighbor"
        }
        self.assertEqual(project_neighbors[P3].support_seed_count, 2)
        self.assertEqual(project_neighbors[P4].support_seed_count, 2)
        self.assertEqual(project_neighbors[P3].best_seed_rank, 1)

    def test_unknown_or_reordered_bm25_seed_result_fails_closed(self):
        valid = bm25_result("cutover deployment")
        unknown_hit = bm25.BM25SearchHitV1(
            memory_key=UNKNOWN,
            score=1.0,
            matched_term_count=1,
        )
        forged_unknown = bm25.BM25SearchResultV1(
            hits=(unknown_hit,),
            query_term_count=1,
            indexed_document_count=valid.indexed_document_count,
        )
        self.assert_error(
            "invalid_bm25_result",
            routing.route_hierarchy_candidates_v1,
            atomics(),
            current_plan(),
            forged_unknown,
        )
        forged_order = dataclasses.replace(valid, hits=tuple(reversed(valid.hits)))
        self.assert_error(
            "invalid_bm25_result",
            routing.route_hierarchy_candidates_v1,
            atomics(),
            current_plan(),
            forged_order,
        )

    def test_b4_invalid_but_structurally_valid_topic_is_rejected(self):
        values = atomics()
        project_members = tuple(sorted((P1, P2, P3, P4, S1, PS1)))
        forged_topics = (
            hierarchy.TopicGroupingV1("topic.forged", project_members),
            hierarchy.TopicGroupingV1("topic.user", (U1,)),
        )
        forged = hierarchy.plan_hierarchy_projection_v1(
            values,
            forged_topics,
            (),
        )
        self.assert_error(
            "invalid_hierarchy",
            routing.route_hierarchy_candidates_v1,
            values,
            forged,
            bm25_result("cutover"),
        )

    def test_result_and_item_repr_are_content_and_identity_free(self):
        result = routing.route_hierarchy_candidates_v1(
            atomics(),
            current_plan(),
            bm25_result("cutover"),
        )
        rendered = repr(result) + " " + " ".join(repr(item) for item in result.items)
        for forbidden in (
            P1,
            P2,
            P3,
            "cutover",
            "Render",
            "Vercel",
        ):
            self.assertNotIn(forbidden, rendered)


class HierarchyRoutingSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.authority = self.root / "relay.db"
        self.hierarchy_path = self.root / "hierarchy.db"
        self.bm25_path = self.root / "bm25.db"
        reader = object.__new__(
            memory_hierarchy_snapshot.MemoryHierarchySnapshotReader
        )
        object.__setattr__(reader, "_database_path", str(self.authority))
        self.reader = reader
        self.snapshot = memory_hierarchy_snapshot.HierarchyAtomicSnapshotV1(
            atomics=atomics()
        )
        hierarchy_store.initialize_projection_store(self.hierarchy_path)
        hierarchy_store.apply_projection_plan(self.hierarchy_path, current_plan())
        bm25_store.initialize_bm25_store(self.bm25_path)
        bm25_store.apply_bm25_index_plan(self.bm25_path, bm25_plan())

    def call(self, query="cutover"):
        with mock.patch.object(
            memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
            "load_active_snapshot",
            return_value=self.snapshot,
        ):
            return source.route_current_hierarchy_candidates_v1(
                self.reader,
                self.hierarchy_path,
                self.bm25_path,
                query,
                term_key_id=KEY_ID,
                term_hmac_secret=SECRET,
            )

    def test_current_three_way_revision_binding_routes_successfully(self):
        result = self.call()
        self.assertGreater(result.hierarchy_generation, 0)
        self.assertGreater(result.bm25_generation, 0)
        self.assertEqual(result.source_atomic_count, len(atomics()))
        self.assertEqual(
            tuple(item.memory_key for item in result.routing_result.items),
            (P1, P2, P3, P4),
        )

    def test_stale_hierarchy_digest_fails_before_routing(self):
        with sqlite3.connect(self.hierarchy_path) as conn:
            conn.execute(
                "UPDATE projection_meta SET atomic_snapshot_digest=? WHERE singleton=1",
                ("b" * 64,),
            )
            conn.commit()
        with self.assertRaises(source.MemoryRetrievalHierarchySourceError) as raised:
            self.call()
        self.assertEqual(raised.exception.category, "hierarchy_source_stale")

    def test_stale_bm25_digest_fails_before_routing(self):
        stale = dataclasses.replace(
            bm25_plan(),
            source_snapshot_digest="b" * 64,
        )
        self.bm25_path.unlink()
        bm25_store.initialize_bm25_store(self.bm25_path)
        bm25_store.apply_bm25_index_plan(self.bm25_path, stale)
        with self.assertRaises(source.MemoryRetrievalHierarchySourceError) as raised:
            self.call()
        self.assertEqual(raised.exception.category, "hierarchy_source_stale")

    def test_structurally_valid_forged_topic_sidecar_fails_b4_reproof(self):
        values = atomics()
        forged_topics = (
            hierarchy.TopicGroupingV1(
                "topic.forged",
                tuple(sorted((P1, P2, P3, P4, S1, PS1))),
            ),
            hierarchy.TopicGroupingV1("topic.user", (U1,)),
        )
        forged = hierarchy.plan_hierarchy_projection_v1(values, forged_topics, ())
        self.hierarchy_path.unlink()
        hierarchy_store.initialize_projection_store(self.hierarchy_path)
        hierarchy_store.apply_projection_plan(self.hierarchy_path, forged)
        with self.assertRaises(source.MemoryRetrievalHierarchySourceError) as raised:
            self.call()
        self.assertEqual(
            raised.exception.category,
            "hierarchy_source_projection_invalid",
        )

    def test_authority_hierarchy_and_bm25_paths_must_be_distinct(self):
        with mock.patch.object(
            memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
            "load_active_snapshot",
            return_value=self.snapshot,
        ):
            with self.assertRaises(source.MemoryRetrievalHierarchySourceError) as raised:
                source.route_current_hierarchy_candidates_v1(
                    self.reader,
                    self.authority,
                    self.bm25_path,
                    "cutover",
                    term_key_id=KEY_ID,
                    term_hmac_secret=SECRET,
                )
        self.assertEqual(
            raised.exception.category,
            "hierarchy_source_configuration_invalid",
        )

    def test_c2_remains_unwired_and_does_not_use_summary_cache_text(self):
        root = Path(__file__).resolve().parents[2]
        context_source = (
            root / "backend" / "memory_context_integration.py"
        ).read_text(encoding="utf-8")
        relay_source = (
            root / "backend" / "p3_relay_app.py"
        ).read_text(encoding="utf-8")
        source_module = (
            root / "backend" / "memory_retrieval_hierarchy_source.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("memory_retrieval_hierarchy", context_source)
        self.assertNotIn("memory_retrieval_hierarchy", relay_source)
        self.assertNotIn("summary_store", source_module)
        self.assertNotIn("load_current_summary", source_module)


if __name__ == "__main__":
    unittest.main()
