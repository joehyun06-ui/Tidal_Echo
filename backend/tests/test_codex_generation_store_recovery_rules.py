from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from backend import codex_generation_store as store


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class CodexGenerationRecoveryRulesTest(unittest.TestCase):
    def test_dispatch_uncertain_cannot_be_marked_complete_without_callback_pending(self):
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
            store.begin_thread_dispatch(
                path,
                job_id=job["id"],
                thread_attempt_id="attempt-1",
                cwd=cwd,
            )
            store.bind_session_thread(
                path,
                job_id=job["id"],
                thread_attempt_id="attempt-1",
                thread_id="thr-1",
                cwd=cwd,
            )
            store.begin_turn_dispatch(path, job_id=job["id"])
            store.mark_dispatch_uncertain(path, job_id=job["id"])
            with self.assertRaisesRegex(store.CodexGenerationStoreError, "state_conflict"):
                store.mark_completed(path, job_id=job["id"], assistant_message_id=99)

    def test_failed_or_interrupted_reconcile_is_terminal_not_requeued(self):
        for terminal in ("failed", "interrupted"):
            with self.subTest(terminal=terminal), tempfile.TemporaryDirectory() as temp:
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
                store.mark_dispatch_uncertain(path, job_id=job["id"])
                reconciled = store.record_reconciled_turn(
                    path,
                    job_id=job["id"],
                    turn_id="turn-1",
                    status=terminal,
                )
                self.assertEqual(reconciled["status"], "failed")
                self.assertIsNone(store.claim_next_job(path))


if __name__ == "__main__":
    unittest.main()
