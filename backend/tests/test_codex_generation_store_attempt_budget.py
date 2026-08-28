from __future__ import annotations

import hashlib
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend import codex_generation_store as store


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class CodexGenerationAttemptBudgetTest(unittest.TestCase):
    def test_only_safe_pre_dispatch_retries_consume_attempt_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "codex-generation.db"
            store.initialize(path)
            store.pin_session(
                path,
                api_session="api-canary",
                model="gpt-5.6-sol",
                model_provider="openai",
                reasoning_effort="high",
                persona_hash=sha("persona"),
            )
            job = store.enqueue_job(
                path,
                api_session="api-canary",
                canonical_message_id=1,
                input_digest=sha("hello"),
                generation_id="codex-gen-1",
                client_message_id="codex-client-1",
                callback_identity="codex-callback-1",
            )
            for expected in (1, 2):
                claimed = store.claim_next_job(path, lease_seconds=1, max_attempts=2)
                self.assertEqual(claimed["attempt_count"], expected)
                if expected == 1:
                    with closing(store.connect(path)) as conn:
                        conn.execute(
                            "UPDATE codex_generation_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",
                            (job["id"],),
                        )
            with closing(store.connect(path)) as conn:
                conn.execute(
                    "UPDATE codex_generation_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",
                    (job["id"],),
                )
            self.assertIsNone(store.claim_next_job(path, max_attempts=2))
            terminal = store.get_job(path, job["id"])
            self.assertEqual((terminal["status"], terminal["error_category"]), ("failed", "max_attempts"))


if __name__ == "__main__":
    unittest.main()
