from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backend import sinus_wake


UTC = timezone.utc
NOW = datetime(2026, 8, 15, 14, 0, tzinfo=UTC)


class SinusWakeStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.store = sinus_wake.WakeStateStore(self.path)

    def test_missing_state_is_immediately_due(self):
        state = self.store.load()
        self.assertTrue(sinus_wake.is_due(state, now=NOW))
        self.assertEqual(state.did, sinus_wake.DEFAULT_DID)
        self.assertEqual(state.schedule_generation, 0)

    def test_schedule_clamps_and_passes_did_forward(self):
        state, minutes = self.store.schedule_wakeup(
            999,
            "Checked deployment; waiting for the next status change.",
            min_minutes=2,
            max_minutes=360,
            now=NOW,
        )
        self.assertEqual(minutes, 360)
        self.assertEqual(state.did, "Checked deployment; waiting for the next status change.")
        self.assertEqual(state.wakeup_reason, "scheduled")
        self.assertEqual(state.consecutive_fallbacks, 0)
        self.assertEqual(state.schedule_generation, 1)
        self.assertFalse(sinus_wake.is_due(state, now=NOW))
        self.assertEqual(self.store.load(), state)

    def test_fallback_keeps_did_and_does_not_fake_agent_schedule(self):
        scheduled, _ = self.store.schedule_wakeup(
            20,
            "Waiting for a concrete external result.",
            min_minutes=2,
            max_minutes=360,
            now=NOW,
        )
        fallback = self.store.schedule_fallback(30, now=NOW)
        self.assertEqual(fallback.did, scheduled.did)
        self.assertEqual(fallback.wakeup_reason, "fallback")
        self.assertEqual(fallback.consecutive_fallbacks, 1)
        self.assertEqual(fallback.schedule_generation, scheduled.schedule_generation)

    def test_run_identity_is_stable_until_schedule_changes(self):
        initial = self.store.load()
        first = sinus_wake.wake_run_id(initial)
        self.assertEqual(first, sinus_wake.wake_run_id(self.store.load()))
        state, _ = self.store.schedule_wakeup(
            5,
            "Scheduled another look.",
            min_minutes=2,
            max_minutes=360,
            now=NOW,
        )
        self.assertNotEqual(first, sinus_wake.wake_run_id(state))

    def test_invalid_or_corrupt_state_fails_closed(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not-json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid_wake_state"):
            self.store.load()

        self.path.write_text(json.dumps({"did": "", "schedule_generation": -1}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid_wake_state"):
            self.store.load()

    def test_did_is_bounded(self):
        with self.assertRaisesRegex(ValueError, "invalid_wake_did"):
            self.store.schedule_wakeup(
                5,
                "x" * (sinus_wake.MAX_DID_CHARS + 1),
                min_minutes=2,
                max_minutes=360,
                now=NOW,
            )


if __name__ == "__main__":
    unittest.main()
