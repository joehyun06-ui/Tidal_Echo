from __future__ import annotations

import unittest

from backend import (
    memory_context,
    memory_hierarchy_projection as hierarchy,
    memory_retrieval_hybrid_active as hybrid_active,
)


class HybridActiveProviderWireParityTests(unittest.TestCase):
    def test_d3c1_atomic_mapping_renders_byte_identical_legacy_memory_envelope(self):
        atomic = hierarchy.AtomicMemoryProjectionInputV1(
            memory_key="hybrid_provider_wire_parity_0001",
            kind="project",
            scope_type="global_user",
            scope_ref="",
            normalized_content=(
                "Production Render Auto-Deploy remains disabled and releases are manual."
            ),
            fingerprint_version=1,
            status="active",
            explicitness="explicit",
            confidence=0.95,
            sensitivity="normal",
            first_observed_at="2026-08-01T00:00:00+00:00",
            last_confirmed_at="2026-09-03T00:00:00+00:00",
            updated_at="2026-09-03T00:00:00+00:00",
        )
        legacy_safe_item = {
            "memory_key": atomic.memory_key,
            "kind": atomic.kind,
            "scope_type": atomic.scope_type,
            "scope_ref": atomic.scope_ref,
            "normalized_content": atomic.normalized_content,
            "fingerprint_version": atomic.fingerprint_version,
            "status": atomic.status,
            "explicitness": atomic.explicitness,
            "confidence": atomic.confidence,
            "sensitivity": atomic.sensitivity,
            "first_observed_at": atomic.first_observed_at,
            "last_confirmed_at": atomic.last_confirmed_at,
            "created_at": atomic.first_observed_at,
            "updated_at": atomic.updated_at,
            "provenance": [],
        }

        legacy_message = memory_context.render_memory_developer_message(
            (legacy_safe_item,),
            scope_type="global_user",
            max_items=memory_context.DEFAULT_MAX_ITEMS,
            character_budget=memory_context.DEFAULT_CHARACTER_BUDGET,
        )
        active_message = memory_context.render_memory_developer_message(
            (hybrid_active._atomic_context_item(atomic),),
            scope_type="global_user",
            max_items=memory_context.DEFAULT_MAX_ITEMS,
            character_budget=memory_context.DEFAULT_CHARACTER_BUDGET,
        )

        self.assertEqual(active_message, legacy_message)
        self.assertIsNotNone(active_message)
        self.assertEqual(active_message["role"], "developer")
        self.assertIn("memory_context_developer_message/v1", active_message["content"])

        base = (
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "check deployment policy"},
        )
        legacy_wire = (*base[:-1], dict(legacy_message), base[-1])
        active_wire = (*base[:-1], dict(active_message), base[-1])
        self.assertEqual(active_wire, legacy_wire)
        self.assertEqual(active_wire[-2]["role"], "developer")
        self.assertEqual(active_wire[-1]["role"], "user")


if __name__ == "__main__":
    unittest.main()
