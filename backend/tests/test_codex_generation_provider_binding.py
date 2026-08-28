from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from backend import codex_generation_provider_binding as binding
from backend import codex_generation_store as store


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class CodexGenerationProviderBindingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "codex-generation.db"
        store.initialize(self.path)
        store.pin_session(
            self.path,
            api_session="api-canary",
            model="gpt-5.6-sol",
            model_provider=binding.UNRESOLVED_MODEL_PROVIDER,
            reasoning_effort="high",
            persona_hash=sha("persona"),
        )
        self.job = store.enqueue_job(
            self.path,
            api_session="api-canary",
            canonical_message_id=1,
            input_digest=sha("hello"),
            generation_id="codex-gen-1",
            client_message_id="codex-client-1",
            callback_identity="codex-callback-1",
        )
        self.job = store.claim_next_job(self.path)
        self.cwd = str(Path(self.temp.name) / "workspace" / "attempt-1")
        store.begin_thread_dispatch(
            self.path,
            job_id=self.job["id"],
            thread_attempt_id="attempt-1",
            cwd=self.cwd,
        )

    def test_first_thread_freezes_provider_and_thread_in_one_transaction(self):
        job = binding.bind_first_thread_and_provider(
            self.path,
            job_id=self.job["id"],
            thread_attempt_id="attempt-1",
            thread_id="thr-1",
            cwd=self.cwd,
            model_provider="openai",
        )
        session = store.get_session(self.path, "api-canary")
        self.assertEqual(session["model_provider"], "openai")
        self.assertEqual(session["thread_id"], "thr-1")
        self.assertEqual(session["cwd"], self.cwd)
        self.assertEqual(job["status"], "processing")
        self.assertEqual(job["thread_id"], "thr-1")

    def test_unresolved_sentinel_can_never_be_frozen_as_real_provider(self):
        with self.assertRaisesRegex(store.CodexGenerationStoreError, "provider_invalid"):
            binding.bind_first_thread_and_provider(
                self.path,
                job_id=self.job["id"],
                thread_attempt_id="attempt-1",
                thread_id="thr-1",
                cwd=self.cwd,
                model_provider=binding.UNRESOLVED_MODEL_PROVIDER,
            )
        session = store.get_session(self.path, "api-canary")
        self.assertEqual(session["model_provider"], binding.UNRESOLVED_MODEL_PROVIDER)
        self.assertIsNone(session["thread_id"])

    def test_existing_authoritative_provider_cannot_change(self):
        binding.bind_first_thread_and_provider(
            self.path,
            job_id=self.job["id"],
            thread_attempt_id="attempt-1",
            thread_id="thr-1",
            cwd=self.cwd,
            model_provider="openai",
        )
        store.mark_failed(self.path, job_id=self.job["id"], category="test_terminal")
        second = store.enqueue_job(
            self.path,
            api_session="api-canary",
            canonical_message_id=2,
            input_digest=sha("second"),
            generation_id="codex-gen-2",
            client_message_id="codex-client-2",
            callback_identity="codex-callback-2",
        )
        self.assertEqual(second["thread_id"], "thr-1")
        self.assertEqual(store.get_session(self.path, "api-canary")["model_provider"], "openai")


if __name__ == "__main__":
    unittest.main()
