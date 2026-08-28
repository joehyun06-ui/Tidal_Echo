from __future__ import annotations

import unittest

from backend import codex_generation_store as store


class CodexGenerationStatusSetTest(unittest.TestCase):
    def test_status_vocabulary_is_explicit_and_no_fallback_state_exists(self):
        self.assertEqual(store.JOB_STATUSES, {
            "queued", "processing", "thread_dispatching", "turn_dispatching",
            "in_progress", "callback_pending", "completed", "failed",
            "dispatch_uncertain",
        })
        self.assertNotIn("fallback", " ".join(store.JOB_STATUSES))


if __name__ == "__main__":
    unittest.main()
