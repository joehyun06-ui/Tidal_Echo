from __future__ import annotations

import unittest

from backend import (
    memory_hierarchy_derived_text as derived,
    memory_hierarchy_derived_text_extractor as extractor,
    memory_hierarchy_projection as hierarchy,
)


K1 = "derived_private_000001"
K2 = "derived_private_000002"


def atomic(key: str, content: str, sensitivity: str):
    return hierarchy.AtomicMemoryProjectionInputV1(
        memory_key=key,
        kind="project",
        scope_type="global_user",
        scope_ref="",
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


class MemoryHierarchyDerivedTextPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_sensitive_member_blocks_provider_before_payload_creation(self):
        atomics = (
            atomic(K1, "Normal project fact.", "normal"),
            atomic(K2, "Sensitive project fact that must not leave the server.", "sensitive"),
        )
        topic = hierarchy.TopicGroupingV1("topic.project", (K1, K2))
        plan = hierarchy.plan_hierarchy_projection_v1(atomics, (topic,), ())
        topic_node = next(node for node in plan.nodes if node.node_type == "topic")
        binding = derived.binding_from_projection_node_v1(topic_node)
        calls = []

        async def forbidden(*_args):
            calls.append(1)
            return {"text": "{}"}

        with self.assertRaises(
            extractor.MemoryHierarchyDerivedTextExtractorError
        ) as raised:
            await extractor.extract_derived_text_v1(
                forbidden,
                binding,
                atomics,
                provider_model="test-model",
                provider_prompt_contract_version="test-prompt-v1",
            )
        self.assertEqual(raised.exception.category, "sensitive_input_disabled")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
