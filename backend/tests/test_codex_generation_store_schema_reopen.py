from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend import codex_generation_store as store


class CodexGenerationStoreSchemaReopenTest(unittest.TestCase):
    def test_reopen_revalidates_schema_and_rejects_extra_table(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "codex-generation.db"
            store.initialize(path)
            store.initialize(path)
            with store.connect(path) as conn:
                conn.execute("CREATE TABLE unexpected_table (id INTEGER PRIMARY KEY)")
            with self.assertRaisesRegex(
                store.CodexGenerationStoreError, "codex_generation_store_schema_invalid"
            ):
                store.initialize(path)


if __name__ == "__main__":
    unittest.main()
