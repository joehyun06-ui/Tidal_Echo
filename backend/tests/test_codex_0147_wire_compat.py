from __future__ import annotations

import unittest

from backend.codex_0147_wire_compat import (
    correlated_turn_from_page,
    final_answer_from_turn,
)
from backend.codex_generation_live_reliability import enrich_generation_notification


class Codex0147WireCompatTest(unittest.TestCase):
    def test_actual_0147_final_answer_phase_is_deliverable(self):
        turn = {
            "items": [
                {
                    "type": "agentMessage",
                    "id": "commentary-1",
                    "phase": "commentary",
                    "text": "not the final answer",
                },
                {
                    "type": "agentMessage",
                    "id": "answer-1",
                    "phase": "final_answer",
                    "text": "Codex canary 首轮通过。",
                },
            ]
        }
        self.assertEqual(final_answer_from_turn(turn), "Codex canary 首轮通过。")

    def test_previous_camel_case_and_phase_less_fallback_remain_compatible(self):
        self.assertEqual(
            final_answer_from_turn({
                "items": [{
                    "type": "agentMessage",
                    "id": "a1",
                    "phase": "finalAnswer",
                    "text": "legacy-compatible",
                }]
            }),
            "legacy-compatible",
        )
        self.assertEqual(
            final_answer_from_turn({
                "items": [{"type": "agentMessage", "id": "a2", "text": "fallback"}]
            }),
            "fallback",
        )

    def test_recovery_repairs_base_projection_for_0147_summary_page(self):
        page = {
            "data": [{
                "id": "turn-1",
                "status": "completed",
                "items": [
                    {
                        "type": "userMessage",
                        "id": "u1",
                        "clientId": "codex-client-233",
                        "content": [],
                    },
                    {
                        "type": "agentMessage",
                        "id": "a1",
                        "phase": "final_answer",
                        "text": "recovered-answer",
                    },
                ],
            }]
        }
        found = correlated_turn_from_page(page, "codex-client-233")
        self.assertIsNotNone(found)
        self.assertEqual(found.turn_id, "turn-1")
        self.assertEqual(found.status, "completed")
        self.assertEqual(found.final_answer, "recovered-answer")

    def test_completed_notification_preserves_0147_final_answer(self):
        event = enrich_generation_notification("turn/completed", {
            "threadId": "thr-1",
            "turn": {
                "id": "turn-1",
                "status": "completed",
                "items": [{
                    "type": "agentMessage",
                    "id": "a1",
                    "phase": "final_answer",
                    "text": "terminal-answer",
                }],
            },
        })
        self.assertIsNotNone(event)
        self.assertTrue(event.terminal)
        self.assertEqual(event.final_answer, "terminal-answer")


if __name__ == "__main__":
    unittest.main()
