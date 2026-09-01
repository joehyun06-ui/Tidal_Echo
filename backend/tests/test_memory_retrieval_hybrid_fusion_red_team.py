from __future__ import annotations

import unittest
from pathlib import Path

from backend import (
    memory_hierarchy_projection as hierarchy,
    memory_retrieval_hybrid_fusion as hybrid,
)


K1 = "hybrid_red_team_atomic_000001"


def atomic(content: str) -> hierarchy.AtomicMemoryProjectionInputV1:
    return hierarchy.AtomicMemoryProjectionInputV1(
        memory_key=K1,
        kind="project",
        scope_type="global_user",
        scope_ref="",
        normalized_content=content,
        fingerprint_version=1,
        status="active",
        explicitness="explicit",
        confidence=1.0,
        sensitivity="normal",
        first_observed_at="2026-09-01T08:00:00+00:00",
        last_confirmed_at="2026-09-01T08:00:00+00:00",
        updated_at="2026-09-01T08:00:00+00:00",
    )


class HybridFusionExactBoundaryRedTeamTests(unittest.TestCase):
    def test_identifier_is_not_exact_when_only_a_subtoken_of_larger_identifier(self):
        deploy_id = "dep-daak91hf2nfc73ak97p0"
        candidate = atomic(f"prefix-{deploy_id}-suffix")
        self.assertEqual(
            hybrid._exact_channel((candidate,), deploy_id),
            (),
        )

    def test_identifier_matches_as_complete_token_with_sentence_punctuation(self):
        deploy_id = "dep-daak91hf2nfc73ak97p0"
        candidate = atomic(f"Current deploy is {deploy_id}.")
        self.assertEqual(
            hybrid._exact_channel((candidate,), deploy_id),
            ((K1, 1),),
        )

    def test_environment_identifier_matches_case_insensitively_but_not_partially(self):
        candidate = atomic(
            "CODEX_GENERATION_ENABLED is false; "
            "OLD_CODEX_GENERATION_ENABLED_BACKUP is unrelated."
        )
        self.assertEqual(
            hybrid._exact_channel((candidate,), "codex_generation_enabled"),
            ((K1, 1),),
        )
        partial_only = atomic("OLD_CODEX_GENERATION_ENABLED_BACKUP is unrelated.")
        self.assertEqual(
            hybrid._exact_channel((partial_only,), "CODEX_GENERATION_ENABLED"),
            (),
        )


class HybridFusionAuthorityRedTeamTests(unittest.TestCase):
    def test_d1_remains_unwired_to_current_context_runtime_and_app(self):
        root = Path(__file__).resolve().parents[2]
        for relative in (
            "backend/memory_context_integration.py",
            "backend/p3_relay_app.py",
            "backend/app.py",
        ):
            with self.subTest(relative=relative):
                source = (root / relative).read_text(encoding="utf-8")
                self.assertNotIn("memory_retrieval_hybrid_fusion", source)
                self.assertNotIn("HYBRID_FUSION", source)

    def test_d1_adds_no_render_or_deployment_gate(self):
        root = Path(__file__).resolve().parents[2]
        for relative in (
            "backend/deployment_config.py",
            "backend/.env.example",
            "render.yaml",
        ):
            with self.subTest(relative=relative):
                source = (root / relative).read_text(encoding="utf-8")
                self.assertNotIn("HYBRID_FUSION", source)
                self.assertNotIn("memory_retrieval_hybrid_fusion", source)


if __name__ == "__main__":
    unittest.main()
