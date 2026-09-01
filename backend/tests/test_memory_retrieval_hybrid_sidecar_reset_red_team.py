from __future__ import annotations

import os
import tempfile
import types
import unittest
from pathlib import Path

from backend import memory_retrieval_hybrid_runtime_composition as composition


FINGERPRINT_SECRET = "Fingerprint-Secret-0123456789-AbCd!"
TERM_SECRET = "Hybrid-Term-Secret-0123456789-XyZ!"
EMBEDDING_KEY = "Embedding-Key-0123456789-AbCdEfGh!"


def _relay(root: Path):
    memory = types.SimpleNamespace(
        fingerprint_key_id="memory-fingerprint-v1",
        fingerprint_hmac_secret=FINGERPRINT_SECRET,
        max_item_chars=1000,
        sensitive_storage_enabled=False,
    )
    return types.SimpleNamespace(
        DEPLOYMENT=types.SimpleNamespace(
            db_path=root / "relay.db",
            persistent_root=root,
            memory=memory,
        )
    )


def _env():
    return {
        "MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED": "true",
        composition.TERM_KEY_ID_ENV: "hybrid-term-v1",
        composition.TERM_SECRET_ENV: TERM_SECRET,
        composition.EMBEDDING_API_BASE_ENV: "https://embedding.example/v1",
        composition.EMBEDDING_API_KEY_ENV: EMBEDDING_KEY,
        composition.EMBEDDING_MODEL_ENV: "text-embedding-test-v1",
        composition.EMBEDDING_DIMENSIONS_ENV: "8",
    }


class DisposableSidecarResetRedTeamTests(unittest.TestCase):
    def _assert_unrelated_hardlink_is_rejected_without_mutation(
        self,
        *,
        vector: bool,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = composition.load_hybrid_runtime_config_v1(
                _relay(root),
                _env(),
            )
            path = config.vector_path if vector else config.bm25_path
            victim = root / ("victim-vector.bin" if vector else "victim-bm25.bin")
            victim.write_bytes(b"")
            try:
                os.link(victim, path)
            except OSError:
                self.skipTest("hard links unavailable")
            self.assertTrue(os.path.samefile(victim, path))
            self.assertGreaterEqual(path.stat().st_nlink, 2)

            with self.assertRaises(
                composition.MemoryRetrievalHybridRuntimeCompositionError
            ) as raised:
                if vector:
                    composition._initialize_vector(config)
                else:
                    composition._initialize_bm25(config)

            self.assertEqual(
                raised.exception.category,
                "hybrid_runtime_configuration_invalid",
            )
            self.assertEqual(victim.read_bytes(), b"")
            self.assertTrue(path.is_file())
            self.assertTrue(os.path.samefile(victim, path))
            self.assertEqual(path.stat().st_size, 0)

    def test_bm25_multilink_inode_fails_closed_without_write_or_unlink(self):
        self._assert_unrelated_hardlink_is_rejected_without_mutation(vector=False)

    def test_vector_multilink_inode_fails_closed_without_write_or_unlink(self):
        self._assert_unrelated_hardlink_is_rejected_without_mutation(vector=True)


if __name__ == "__main__":
    unittest.main()
