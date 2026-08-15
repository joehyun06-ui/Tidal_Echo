from __future__ import annotations

import unittest

from backend import deployment_config
from scripts import autonomous_wake_worker as worker


class AutonomousWakeWorkerTests(unittest.TestCase):
    def test_default_is_disabled_and_uses_sinus_defaults(self):
        config = worker.load_config({})
        self.assertFalse(config.enabled)
        self.assertEqual(config.min_minutes, 2)
        self.assertEqual(config.max_minutes, 360)
        self.assertEqual(config.fallback_minutes, 30)
        self.assertEqual(config.poll_seconds, 5.0)

    def test_enabled_requires_https_bridge_and_strong_token(self):
        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            "invalid_autonomous_wake_bridge_url",
        ):
            worker.load_config({"AUTONOMOUS_WAKE_ENABLED": "true"})

        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            "invalid_autonomous_wake_token",
        ):
            worker.load_config({
                "AUTONOMOUS_WAKE_ENABLED": "true",
                "AUTONOMOUS_WAKE_BRIDGE_URL": "https://example.invalid/wake",
                "AUTONOMOUS_WAKE_TOKEN": "short",
            })

    def test_invalid_schedule_bounds_fail_closed(self):
        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            "invalid_autonomous_wake_interval_bounds",
        ):
            worker.load_config({
                "AUTONOMOUS_WAKE_MIN_MINUTES": "120",
                "AUTONOMOUS_WAKE_MAX_MINUTES": "60",
            })

    def test_model_decision_requires_action_schedule_and_did(self):
        silent = worker._parse_model_decision(
            '{"action":"silent","next_wakeup_minutes":90,"did":"No concrete follow-up is pending."}'
        )
        self.assertEqual(silent.action, "silent")
        self.assertEqual(silent.message, "")
        self.assertEqual(silent.next_wakeup_minutes, 90)
        self.assertEqual(silent.did, "No concrete follow-up is pending.")

        message = worker._parse_model_decision(
            '{"action":"message","message":"hi","next_wakeup_minutes":20,"did":"Sent a concrete follow-up."}'
        )
        self.assertEqual(message.action, "message")
        self.assertEqual(message.message, "hi")
        self.assertEqual(message.next_wakeup_minutes, 20)

        invalid = (
            '{"action":"silent"}',
            '{"action":"silent","next_wakeup_minutes":30,"did":""}',
            '{"action":"message","next_wakeup_minutes":30,"did":"missing message"}',
            '{"action":"other","next_wakeup_minutes":30,"did":"x"}',
            '{"action":"silent","message":"not allowed","next_wakeup_minutes":30,"did":"x"}',
            '{"action":"silent","next_wakeup_minutes":30,"did":"x","extra":1}',
            "not json",
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaisesRegex(
                ValueError, "invalid_model_decision"
            ):
                worker._parse_model_decision(raw)


if __name__ == "__main__":
    unittest.main()
