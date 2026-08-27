from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from backend import codex_generation_store as store


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class CodexGenerationSessionContractTest(unittest.TestCase):
    def test_pinned_session_freezes_model_provider_effort_and_persona_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "codex-generation.db"
            store.initialize(path)
            row = store.pin_session(
                path,
                api_session="api-canary",
                model="gpt-5.6-sol",
                model_provider="openai",
                reasoning_effort="high",
                persona_hash=sha("persona"),
            )
            self.assertEqual(
                (row["provider"], row["model"], row["model_provider"], row["reasoning_effort"]),
                ("codex", "gpt-5.6-sol", "openai", "high"),
            )
            self.assertEqual(row["persona_hash"], sha("persona"))


if __name__ == "__main__":
    unittest.main()
