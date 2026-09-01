from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import (
    memory_hierarchy_episode_refinement_extractor as episode_extractor,
    memory_hierarchy_projection as hierarchy,
    memory_hierarchy_projection_store as projection_store,
    memory_hierarchy_refinement_extractor as topic_extractor,
    memory_hierarchy_semantic_rebuild as semantic,
    memory_hierarchy_snapshot,
)


P1 = "semantic_project_alpha_000001"
D1 = "semantic_decision_alpha_000002"
T1 = "semantic_progress_alpha_000003"
P2 = "semantic_project_beta_000004"
MODEL = "semantic-test-model"
PROMPT = "kelivo-provider-prompt-v1"
SECRET = "Semantic-Rebuild-HMAC-0123456789-AbCd!"


def atomic(
    key: str,
    kind: str,
    content: str,
    *,
    sensitivity: str = "normal",
    minute: int = 0,
):
    stamp = f"2026-09-01T08:{minute:02d}:00+00:00"
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
        first_observed_at=stamp,
        last_confirmed_at=stamp,
        updated_at=stamp,
    )


def normal_atomics():
    return (
        atomic(P1, "project", "Project Alpha uses Python.", minute=0),
        atomic(D1, "decision", "Project Alpha backend is deployed on Render.", minute=1),
        atomic(T1, "task_or_progress", "Project Alpha Render deployment completed.", minute=2),
        atomic(P2, "project", "Project Beta uses Rust.", minute=3),
    )


def reader(path: Path):
    return memory_hierarchy_snapshot.MemoryHierarchySnapshotReader(
        path,
        fingerprint_key_id="semantic-test-key",
        fingerprint_hmac_secret=SECRET,
        max_item_chars=4096,
        sensitive_storage_enabled=True,
    )


class MemoryHierarchySemanticRebuildTests(unittest.IsolatedAsyncioTestCase):
    async def test_b4_split_then_b5_episode_materializes_content_free_projection(self):
        atomics = normal_atomics()
        snapshot = memory_hierarchy_snapshot.HierarchyAtomicSnapshotV1(atomics)
        calls = []

        async def generation(messages, session_id, *_args, **_kwargs):
            calls.append((session_id, messages[1]["content"]))
            if session_id == topic_extractor.EXTRACTOR_SESSION_ID:
                return {
                    "text": json.dumps(
                        {
                            "version": topic_extractor.EXTRACTOR_CONTRACT_VERSION,
                            "topic_groups": [[P1, D1, T1], [P2]],
                        },
                        separators=(",", ":"),
                    )
                }
            if session_id == episode_extractor.EXTRACTOR_SESSION_ID:
                return {
                    "text": json.dumps(
                        {
                            "version": episode_extractor.EXTRACTOR_CONTRACT_VERSION,
                            "episode_groups": [[D1, T1]],
                        },
                        separators=(",", ":"),
                    )
                }
            self.fail(f"unexpected session {session_id}")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            authority = root / "relay.db"
            sidecar = root / "hierarchy.db"
            bound_reader = reader(authority)
            with mock.patch.object(
                memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
                "load_active_snapshot",
                return_value=snapshot,
            ):
                receipt = await semantic.rebuild_semantic_hierarchy_v1(
                    bound_reader,
                    sidecar,
                    generation,
                    provider_model=MODEL,
                    provider_prompt_contract_version=PROMPT,
                )
            stored = projection_store.load_projection_snapshot(sidecar)

        self.assertEqual(receipt.atomic_count, 4)
        self.assertEqual(receipt.topic_count, 2)
        self.assertEqual(receipt.episode_count, 1)
        self.assertEqual(receipt.node_count, 5)
        self.assertEqual(receipt.topic_mode, semantic.TOPIC_MODE_APPLIED)
        self.assertEqual(receipt.episode_mode, semantic.EPISODE_MODE_APPLIED)
        self.assertEqual(receipt.topic_provider_call_count, 1)
        self.assertEqual(receipt.episode_provider_call_count, 1)
        self.assertFalse(receipt.provider_failed)
        self.assertEqual(
            [session for session, _payload in calls],
            [
                topic_extractor.EXTRACTOR_SESSION_ID,
                episode_extractor.EXTRACTOR_SESSION_ID,
            ],
        )
        self.assertEqual(sum(node.node_type == "episode" for node in stored.nodes), 1)
        rendered = repr(stored) + repr(receipt)
        for plaintext in (
            "Project Alpha",
            "Project Beta",
            "Python",
            "Render deployment completed",
        ):
            self.assertNotIn(plaintext, rendered)

    async def test_sensitive_event_atomic_blocks_both_semantic_providers_before_access(self):
        atomics = (
            atomic(P1, "project", "Normal project fact.", minute=0),
            atomic(
                D1,
                "decision",
                "PRIVATE-SENSITIVE-DECISION",
                sensitivity="sensitive",
                minute=1,
            ),
            atomic(T1, "task_or_progress", "Normal progress fact.", minute=2),
        )
        snapshot = memory_hierarchy_snapshot.HierarchyAtomicSnapshotV1(atomics)
        calls = []

        async def generation(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("provider must not be called")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            authority = root / "relay.db"
            sidecar = root / "hierarchy.db"
            bound_reader = reader(authority)
            with mock.patch.object(
                memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
                "load_active_snapshot",
                return_value=snapshot,
            ):
                receipt = await semantic.rebuild_semantic_hierarchy_v1(
                    bound_reader,
                    sidecar,
                    generation,
                    provider_model=MODEL,
                    provider_prompt_contract_version=PROMPT,
                )

        self.assertEqual(calls, [])
        self.assertEqual(receipt.topic_mode, semantic.TOPIC_MODE_SKIPPED_SENSITIVE)
        self.assertEqual(receipt.episode_mode, semantic.EPISODE_MODE_SKIPPED_SENSITIVE)
        self.assertEqual(receipt.topic_provider_call_count, 0)
        self.assertEqual(receipt.episode_provider_call_count, 0)
        self.assertEqual(receipt.episode_count, 0)
        self.assertFalse(receipt.provider_failed)

    async def test_provider_failure_falls_back_to_server_structure_but_remains_retryable(self):
        atomics = normal_atomics()
        snapshot = memory_hierarchy_snapshot.HierarchyAtomicSnapshotV1(atomics)
        calls = []

        async def generation(messages, session_id, *_args, **_kwargs):
            calls.append(session_id)
            if session_id == topic_extractor.EXTRACTOR_SESSION_ID:
                raise RuntimeError("private provider failure")
            return {
                "text": json.dumps(
                    {
                        "version": episode_extractor.EXTRACTOR_CONTRACT_VERSION,
                        "episode_groups": [[D1, T1]],
                    },
                    separators=(",", ":"),
                )
            }

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bound_reader = reader(root / "relay.db")
            with mock.patch.object(
                memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
                "load_active_snapshot",
                return_value=snapshot,
            ):
                receipt = await semantic.rebuild_semantic_hierarchy_v1(
                    bound_reader,
                    root / "hierarchy.db",
                    generation,
                    provider_model=MODEL,
                    provider_prompt_contract_version=PROMPT,
                )

        self.assertEqual(receipt.topic_mode, semantic.TOPIC_MODE_PROVIDER_FAILED)
        self.assertTrue(receipt.provider_failed)
        self.assertEqual(receipt.topic_count, 1)
        self.assertEqual(receipt.episode_count, 1)
        self.assertEqual(calls[0], topic_extractor.EXTRACTOR_SESSION_ID)
        self.assertEqual(calls[1], episode_extractor.EXTRACTOR_SESSION_ID)


if __name__ == "__main__":
    unittest.main()
