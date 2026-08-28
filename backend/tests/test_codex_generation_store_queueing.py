from __future__ import annotations

import hashlib
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend import codex_generation_store as store


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CodexGenerationQueueingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "codex-generation.db"
        store.initialize(self.path)
        store.pin_session(
            self.path,
            api_session="api-canary",
            model="gpt-5.6-sol",
            model_provider="openai",
            reasoning_effort="high",
            persona_hash=digest("persona"),
        )

    def enqueue(self, mid: int):
        return store.enqueue_job(
            self.path,
            api_session="api-canary",
            canonical_message_id=mid,
            input_digest=digest(f"m-{mid}"),
            generation_id=f"codex-gen-{mid}",
            client_message_id=f"codex-client-{mid}",
            callback_identity=f"codex-callback-{mid}",
        )

    def test_second_message_must_not_be_claimed_while_first_session_job_is_active(self):
        first = self.enqueue(1)
        second = self.enqueue(2)
        claimed = store.claim_next_job(self.path)
        self.assertEqual(claimed["id"], first["id"])
        self.assertIsNone(store.claim_next_job(self.path))
        store.mark_failed(self.path, job_id=first["id"], category="test_terminal")
        next_job = store.claim_next_job(self.path)
        self.assertEqual(next_job["id"], second["id"])

    def test_new_job_inherits_durable_thread_mapping(self):
        first = self.enqueue(1)
        first = store.claim_next_job(self.path)
        cwd = str(Path(self.temp.name) / "workspace" / "attempt-1")
        store.begin_thread_dispatch(
            self.path,
            job_id=first["id"],
            thread_attempt_id="attempt-1",
            cwd=cwd,
        )
        store.bind_session_thread(
            self.path,
            job_id=first["id"],
            thread_attempt_id="attempt-1",
            thread_id="thr-1",
            cwd=cwd,
        )
        store.begin_turn_dispatch(self.path, job_id=first["id"])
        store.record_turn_started(self.path, job_id=first["id"], turn_id="turn-1")
        store.mark_callback_pending(self.path, job_id=first["id"], turn_id="turn-1")
        store.mark_completed(self.path, job_id=first["id"], assistant_message_id=99)

        second = self.enqueue(2)
        self.assertEqual(second["thread_attempt_id"], "attempt-1")
        self.assertEqual(second["thread_id"], "thr-1")
        self.assertEqual(second["cwd"], cwd)

    def test_recovery_claim_blocks_new_session_work(self):
        first = self.enqueue(1)
        second = self.enqueue(2)
        first = store.claim_next_job(self.path)
        cwd = str(Path(self.temp.name) / "workspace" / "attempt-1")
        store.begin_thread_dispatch(
            self.path,
            job_id=first["id"],
            thread_attempt_id="attempt-1",
            cwd=cwd,
        )
        with closing(store.connect(self.path)) as conn:
            conn.execute(
                "UPDATE codex_generation_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",
                (first["id"],),
            )
        recovered = store.claim_recovery_job(self.path)
        self.assertEqual(recovered["id"], first["id"])
        self.assertIsNone(store.claim_next_job(self.path))
        self.assertEqual(store.get_job(self.path, second["id"])["status"], "queued")


if __name__ == "__main__":
    unittest.main()
