from __future__ import annotations

import json
import unittest
from pathlib import Path

from backend import provider_model_migration as migration


class ProviderModelMigrationRequestContractTests(unittest.TestCase):
    def test_body_decoder_is_bounded_identity_only_and_exact_length(self):
        raw = json.dumps({"model": "provider-model"}).encode("utf-8")
        self.assertEqual(
            migration.decode_model_request_body(
                raw,
                content_length=str(len(raw)),
                content_encoding="identity",
            ),
            {"model": "provider-model"},
        )

        invalid_cases = (
            (b"", None, ""),
            (b"not-json", None, ""),
            (raw, str(len(raw) + 1), ""),
            (raw, " 12", ""),
            (raw, str(migration.MAX_REQUEST_BYTES + 1), ""),
            (raw, None, "gzip"),
            (b"x" * (migration.MAX_REQUEST_BYTES + 1), None, ""),
            (raw.decode("utf-8"), None, ""),
        )
        for body, length, encoding in invalid_cases:
            with self.subTest(length=length, encoding=encoding), self.assertRaises(
                migration.ProviderModelMigrationError
            ) as raised:
                migration.decode_model_request_body(
                    body,
                    content_length=length,
                    content_encoding=encoding,
                )
            self.assertEqual(raised.exception.category, migration.INVALID_REQUEST)

    def test_p3_route_authenticates_before_read_or_migration(self):
        root = Path(__file__).resolve().parents[2]
        source = (root / "backend" / "p3_relay_app.py").read_text(encoding="utf-8")
        start = source.index('@app.post("/app/provider/model")')
        end = source.index(
            'relay_app._P3_PROVIDER_MODEL_MIGRATION_INSTALLED = True',
            start,
        )
        block = source[start:end]
        auth_index = block.index("relay_app.check_auth(request)")
        read_index = block.index("_read_provider_model_request(request)")
        migration_index = block.index("migrate_primary_provider_model")
        self.assertLess(auth_index, read_index)
        self.assertLess(read_index, migration_index)
        self.assertNotIn('"key"', block)
        self.assertNotIn('"url"', block)


if __name__ == "__main__":
    unittest.main()
