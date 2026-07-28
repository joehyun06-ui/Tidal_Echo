from __future__ import annotations

import unittest

from backend import deployment_config


TEST_HMAC_SECRET = "Synthetic-Memory-HMAC-Key-2026-Alpha!Z9q7"
TEST_KEY_ID = "phase1-test-key"


class _TelegramDisabled:
    requested = False


class MemoryConfigTests(unittest.TestCase):
    def load(self, values: dict[str, str] | None = None):
        return deployment_config.load_deployment_config(
            _TelegramDisabled(), environ=values or {}
        ).memory

    def test_defaults_are_disabled_and_bounded(self):
        config = self.load()
        self.assertFalse(config.enabled)
        self.assertFalse(config.explicit_writes_enabled)
        self.assertFalse(config.sensitive_storage_enabled)
        self.assertFalse(config.explicit_entry_enabled)
        self.assertTrue(config.entry_configuration_valid)
        self.assertEqual(config.max_item_chars, 1000)
        self.assertEqual(config.forget_retention_policy, "tombstone_without_content")
        self.assertEqual(config.fingerprint_key_id, "")
        self.assertEqual(config.fingerprint_hmac_secret, "")
        self.assertTrue(config.configuration_valid)

    def test_enabled_read_only_does_not_require_hmac(self):
        config = self.load({"MEMORY_CORE_ENABLED": "true"})
        self.assertTrue(config.enabled)
        self.assertFalse(config.explicit_writes_enabled)
        self.assertTrue(config.configuration_valid)

    def test_enabled_writes_missing_or_invalid_hmac_is_nonfatal_but_invalid(self):
        base = {
            "MEMORY_CORE_ENABLED": "true",
            "MEMORY_EXPLICIT_WRITES_ENABLED": "true",
            "MEMORY_FINGERPRINT_KEY_ID": TEST_KEY_ID,
        }
        for value, category in (
            ("", "memory_fingerprint_hmac_secret_missing"),
            ("short", "memory_fingerprint_hmac_secret_invalid"),
            (" " * 40, "memory_fingerprint_hmac_secret_invalid"),
            ("A" * 40, "memory_fingerprint_hmac_secret_invalid"),
            (
                "Replace-With-Random-Memory-Secret-2026!a",
                "memory_fingerprint_hmac_secret_invalid",
            ),
            (" " + TEST_HMAC_SECRET, "memory_fingerprint_hmac_secret_invalid"),
        ):
            with self.subTest(category=category):
                config = self.load({**base, "MEMORY_FINGERPRINT_HMAC_SECRET": value})
                self.assertFalse(config.configuration_valid)
                self.assertEqual(config.error_category, category)

    def test_hmac_must_be_dedicated_and_is_hidden_from_repr(self):
        config = self.load({
            "MEMORY_CORE_ENABLED": "true",
            "MEMORY_EXPLICIT_WRITES_ENABLED": "true",
            "MEMORY_FINGERPRINT_KEY_ID": TEST_KEY_ID,
            "MEMORY_FINGERPRINT_HMAC_SECRET": TEST_HMAC_SECRET,
            "RELAY_SECRET": TEST_HMAC_SECRET,
        })
        self.assertFalse(config.configuration_valid)
        self.assertEqual(
            config.error_category, "memory_fingerprint_hmac_secret_must_be_distinct"
        )
        self.assertNotIn(TEST_HMAC_SECRET, repr(config))
        self.assertNotIn(TEST_KEY_ID, repr(config))

    def test_enabled_writes_require_bounded_fingerprint_key_id(self):
        base = {
            "MEMORY_CORE_ENABLED": "true",
            "MEMORY_EXPLICIT_WRITES_ENABLED": "true",
            "MEMORY_FINGERPRINT_HMAC_SECRET": TEST_HMAC_SECRET,
        }
        for value, category in (
            ("", "memory_fingerprint_key_id_missing"),
            (" invalid", "memory_fingerprint_key_id_invalid"),
            ("unsafe/key", "memory_fingerprint_key_id_invalid"),
            ("x" * 65, "memory_fingerprint_key_id_invalid"),
        ):
            with self.subTest(value=value):
                config = self.load({**base, "MEMORY_FINGERPRINT_KEY_ID": value})
                self.assertFalse(config.configuration_valid)
                self.assertEqual(config.error_category, category)

    def test_feature_relationship_limits_and_retention_are_strict(self):
        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError, "invalid_memory_feature_relationship"
        ):
            self.load({"MEMORY_EXPLICIT_WRITES_ENABLED": "true"})
        for name, value, category in (
            ("MEMORY_MAX_ITEM_CHARS", "63", "invalid_memory_max_item_chars"),
            ("MEMORY_MAX_ITEM_CHARS", "4097", "invalid_memory_max_item_chars"),
            (
                "MEMORY_FORGET_RETENTION_POLICY",
                "keep_plaintext",
                "invalid_memory_forget_retention_policy",
            ),
        ):
            with self.subTest(name=name, value=value), self.assertRaisesRegex(
                deployment_config.DeploymentConfigError, category
            ):
                self.load({name: value})

    def test_explicit_entry_is_default_closed_and_has_independent_validity(self):
        disabled = self.load({"MEMORY_CORE_ENABLED": "true"})
        self.assertFalse(disabled.explicit_entry_enabled)
        self.assertTrue(disabled.entry_configuration_valid)

        cases = (
            (
                {"MEMORY_EXPLICIT_ENTRY_ENABLED": "true"},
                "memory_explicit_entry_requires_core",
            ),
            (
                {
                    "MEMORY_CORE_ENABLED": "true",
                    "MEMORY_EXPLICIT_ENTRY_ENABLED": "true",
                },
                "memory_explicit_entry_requires_writes",
            ),
            (
                {
                    "MEMORY_CORE_ENABLED": "true",
                    "MEMORY_EXPLICIT_WRITES_ENABLED": "true",
                    "MEMORY_EXPLICIT_ENTRY_ENABLED": "true",
                    "MEMORY_FINGERPRINT_KEY_ID": TEST_KEY_ID,
                },
                "memory_fingerprint_hmac_secret_missing",
            ),
        )
        for environ, category in cases:
            with self.subTest(category=category):
                config = self.load(environ)
                self.assertTrue(config.explicit_entry_enabled)
                self.assertFalse(config.entry_configuration_valid)
                self.assertEqual(config.entry_error_category, category)

        enabled = self.load({
            "MEMORY_CORE_ENABLED": "true",
            "MEMORY_EXPLICIT_WRITES_ENABLED": "true",
            "MEMORY_EXPLICIT_ENTRY_ENABLED": "true",
            "MEMORY_FINGERPRINT_KEY_ID": TEST_KEY_ID,
            "MEMORY_FINGERPRINT_HMAC_SECRET": TEST_HMAC_SECRET,
        })
        self.assertTrue(enabled.entry_configuration_valid)
        self.assertEqual(enabled.entry_error_category, "")


if __name__ == "__main__":
    unittest.main()
