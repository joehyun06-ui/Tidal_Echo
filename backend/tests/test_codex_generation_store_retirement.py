from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from backend import codex_generation_store as store


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class CodexGenerationRetirementTest(unittest.TestCase):
    def test_retired_session_rejects_new_jobs(self):
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
            store.retire_session(path, api_session="api-canary")
            with self.assertRaisesRegex(store.CodexGenerationStoreError, "session_not_pinned"):
                store.enqueue_job(
                    path,
                    api_session="api-canary",
                    canonical_message_id=1,
                    input_digest=sha("hello"),
                    generation_id="codex-gen-1",
                    client_message_id="codex-client-1",
                    callback_identity="codex-callback-1",
                )


if __name__ == "__main__":
    unittest.main()
