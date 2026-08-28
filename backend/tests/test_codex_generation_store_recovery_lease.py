from __future__ import annotations

import hashlib
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend import codex_generation_store as store


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class CodexGenerationRecoveryLeaseTest(unittest.TestCase):
    def test_recovery_lease_prevents_double_reconcile(self):
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
            job = store.claim_next_job(path)
            cwd = str(Path(temp) / "workspace" / "attempt-1")
            store.begin_thread_dispatch(path, job_id=job["id"], thread_attempt_id="attempt-1", cwd=cwd)
            with closing(store.connect(path)) as conn:
                conn.execute(
                    "UPDATE codex_generation_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",
                    (job["id"],),
                )
            first = store.claim_recovery_job(path, lease_seconds=30)
            self.assertEqual(first["id"], job["id"])
            self.assertIsNone(store.claim_recovery_job(path, lease_seconds=30))


if __name__ == "__main__":
    unittest.main()
