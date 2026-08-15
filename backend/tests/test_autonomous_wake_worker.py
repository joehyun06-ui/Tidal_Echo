from __future__ import annotations

import datetime as dt
import unittest

from backend import deployment_config
from scripts import autonomous_wake_worker as worker


class AutonomousWakeWorkerTests(unittest.TestCase):
    def test_default_is_disabled_and_requires_no_secret(self):
        config = worker.load_config({})
        self.assertFalse(config.enabled)
        self.assertEqual(config.day_interval_seconds, 900)
        self.assertEqual(config.night_interval_seconds, 5400)

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

    def test_run_id_uses_day_and_night_cadence(self):
        config = worker.load_config({
            "AUTONOMOUS_WAKE_TIMEZONE": "America/New_York",
            "AUTONOMOUS_WAKE_DAY_INTERVAL_SECONDS": "900",
            "AUTONOMOUS_WAKE_NIGHT_INTERVAL_SECONDS": "5400",
            "AUTONOMOUS_WAKE_NIGHT_START": "22:00",
            "AUTONOMOUS_WAKE_NIGHT_END": "08:00",
        })
        day = worker.make_run_id(
            config,
            dt.datetime(2026, 8, 15, 13, 41, tzinfo=dt.timezone.utc),
        )
        night = worker.make_run_id(
            config,
            dt.datetime(2026, 8, 16, 3, 41, tzinfo=dt.timezone.utc),
        )
        self.assertTrue(day.startswith("wake-v1-day-900-"))
        self.assertTrue(night.startswith("wake-v1-night-5400-"))

    def test_model_decision_contract_is_closed(self):
        self.assertEqual(worker._parse_model_decision('{"action":"silent"}'), ("silent", ""))
        self.assertEqual(
            worker._parse_model_decision('{"action":"message","message":"hi"}'),
            ("message", "hi"),
        )
        with self.assertRaisesRegex(ValueError, "invalid_model_decision"):
            worker._parse_model_decision('{"action":"other"}')
        with self.assertRaisesRegex(ValueError, "invalid_model_decision"):
            worker._parse_model_decision("not json")


if __name__ == "__main__":
    unittest.main()
