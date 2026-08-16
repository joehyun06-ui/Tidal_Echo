from __future__ import annotations

import unittest

from scripts import autonomous_wake_worker as worker


class AutonomousContactPolicyTests(unittest.TestCase):
    @staticmethod
    def _context(silents: int):
        return {
            "memories": [
                {
                    "category": "wake_contact_state",
                    "content": (
                        "内部接触状态（中性调度元数据）："
                        f"连续自主 Wake 选择 silent = {silents}；"
                        "最近一次自主主动消息时间 = 尚无记录。"
                    ),
                }
            ]
        }

    def test_contact_is_optional_before_two_consecutive_silents(self):
        self.assertFalse(worker._contact_required(self._context(0)))
        self.assertFalse(worker._contact_required(self._context(1)))

    def test_third_wake_requires_contact(self):
        self.assertTrue(worker._contact_required(self._context(2)))
        self.assertTrue(worker._contact_required(self._context(9)))

    def test_forced_contact_rejects_silent_but_accepts_free_message(self):
        silent = '{"action":"silent","next_wakeup_minutes":90,"did":"wait"}'
        with self.assertRaisesRegex(ValueError, "forced_contact_silent"):
            worker._parse_model_decision(silent, require_message=True)

        message = (
            '{"action":"message","message":"想来找你说句话。",'
            '"next_wakeup_minutes":45,"did":"sent a natural check-in"}'
        )
        decision = worker._parse_model_decision(message, require_message=True)
        self.assertEqual(decision.action, "message")
        self.assertEqual(decision.message, "想来找你说句话。")

    def test_missing_or_malformed_contact_state_fails_open_to_optional(self):
        self.assertEqual(worker._consecutive_silents({}), 0)
        self.assertFalse(
            worker._contact_required(
                {"memories": [{"category": "wake_contact_state", "content": "bad"}]}
            )
        )


if __name__ == "__main__":
    unittest.main()
