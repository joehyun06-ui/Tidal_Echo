from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from backend import codex_generation_store as store


def h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class CodexGenerationStoreTerminalOrderingTest(unittest.TestCase):
    def test_callback_pending_blocks_later_message_until_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "codex-generation.db"
            store.initialize(path)
            store.pin_session(
                path,
                api_session="api-canary",
                model="gpt-5.6-sol",
                model_provider="openai",
                reasoning_effort="high",
                persona_hash=h("persona"),
            )
            first = store.enqueue_job(
                path,
                api_session="api-canary",
                canonical_message_id=1,
                input_digest=h("one"),
                generation_id="codex-gen-1",
                client_message_id="codex-client-1",
                callback_identity="codex-callback-1",
            )
            second = store.enqueue_job(
                path,
                api_session="api-canary",
                canonical_message_id=2,
                input_digest=h("two"),
                generation_id="codex-gen-2",
                client_message_id="codex-client-2",
                callback_identity="codex-callback-2",
            )
            claimed = store.claim_next_job(path)
            self.assertEqual(claimed["id"], first["id"])
            with store.connect(path) as conn:
                conn.execute(
                    "UPDATE codex_generation_jobs SET status='callback_pending',thread_id='thr-1',turn_id='turn-1' WHERE id=?",
                    (first["id"],),
                )
            self.assertIsNone(store.claim_next_job(path))
            store.mark_completed(path, job_id=first["id"], assistant_message_id=50)
            self.assertEqual(store.claim_next_job(path)["id"], second["id"])


if __name__ == "__main__":
    unittest.main()
