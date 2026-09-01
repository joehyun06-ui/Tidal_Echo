from __future__ import annotations

import unittest
from unittest import mock

from backend import memory_context_integration


K1 = "hybrid_shadow_redteam_000001"


def item():
    return {
        "memory_key": K1,
        "kind": "project",
        "scope_type": "global_user",
        "scope_ref": "",
        "normalized_content": "render deployment",
        "fingerprint_version": 1,
        "status": "active",
        "explicitness": "explicit",
        "confidence": 1.0,
        "sensitivity": "normal",
        "first_observed_at": "2026-01-01T00:00:00Z",
        "last_confirmed_at": "2026-01-02T00:00:00Z",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "provenance": [],
    }


class ReadService:
    def get_active_memories(self, **_kwargs):
        return [item()]


class EffectiveAuthorityKeyRedTeamTests(unittest.TestCase):
    def test_renderer_none_forces_empty_effective_authority_keys(self):
        base = ({"role": "user", "content": "render deployment"},)
        real_selector = memory_context_integration.memory_retrieval.select_relevant_memory_items
        with (
            mock.patch.object(
                memory_context_integration.memory_retrieval,
                "select_relevant_memory_items",
                side_effect=real_selector,
            ),
            mock.patch.object(
                memory_context_integration.memory_context,
                "render_memory_developer_message",
                return_value=None,
            ),
        ):
            result = memory_context_integration.prepare_transient_memory_dispatch(
                ReadService(),
                base,
                enabled=True,
                smart_retrieval_enabled=True,
            )
        self.assertFalse(result.memory_applied)
        self.assertIs(result.provider_messages, base)
        self.assertEqual(result.authoritative_memory_keys, ())
        self.assertNotIn(K1, repr(result))


if __name__ == "__main__":
    unittest.main()
