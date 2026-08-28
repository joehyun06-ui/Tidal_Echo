from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from backend import codex_generation_store as store


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class CodexGenerationNoFallbackTest(unittest.TestCase):
    def test_uncertain_job_remains_nonterminal_until_reconciled(self):
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
            store.bind_session_thread(path, job_id=job["id"], thread_attempt_id="attempt-1", thread_id="thr-1", cwd=cwd)
            store.begin_turn_dispatch(path, job_id=job["id"])
            uncertain = store.mark_dispatch_uncertain(path, job_id=job["id"])
            self.assertEqual(uncertain["status"], "dispatch_uncertain")
            self.assertIn(uncertain["status"], store.ACTIVE_JOB_STATUSES)
            self.assertNotIn(uncertain["status"], {"completed", "failed"})


if __name__ == "__main__":
    unittest.main()
