from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from backend import codex_generation_store as store


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class CodexGenerationIdValidationTest(unittest.TestCase):
    def test_unsafe_identifiers_are_rejected_before_sql(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "codex-generation.db"
            store.initialize(path)
            with self.assertRaises(store.CodexGenerationStoreError):
                store.pin_session(
                    path,
                    api_session="../escape",
                    model="gpt-5.6-sol",
                    model_provider="openai",
                    reasoning_effort="high",
                    persona_hash=sha("persona"),
                )


if __name__ == "__main__":
    unittest.main()
