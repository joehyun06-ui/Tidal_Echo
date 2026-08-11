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
        self.assertFalse(config.context_injection_enabled)
        self.assertFalse(config.smart_retrieval_enabled)
        self.assertFalse(config.explicit_writes_enabled)
        self.assertFalse(config.sensitive_storage_enabled)
        self.assertFalse(config.explicit_entry_enabled)
        self.assertFalse(config.auto_formation_enabled)
        self.assertFalse(config.auto_candidate_persistence_enabled)
        self.assertFalse(config.candidate_review_enabled)
        self.assertFalse(config.candidate_decisions_enabled)
        self.assertTrue(config.entry_configuration_valid)
        self.assertEqual(config.max_item_chars, 1000)
        self.assertEqual(config.forget_retention_policy, "tombstone_without_content")
        self.assertEqual(config.fingerprint_key_id, "")
        self.assertEqual(config.fingerprint_hmac_secret, "")
        self.assertTrue(config.configuration_valid)

    def test_candidate_review_is_strict_default_off_and_requires_only_core(self):
        for value in ("", "maybe", " true ", "enabled"):
            with self.subTest(value=value), self.assertRaisesRegex(
                deployment_config.DeploymentConfigError,
                r"^invalid_memory_candidate_review_enabled$",
            ):
                self.load({"MEMORY_CANDIDATE_REVIEW_ENABLED": value})

        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            r"^memory_candidate_review_requires_core$",
        ):
            self.load({"MEMORY_CANDIDATE_REVIEW_ENABLED": "true"})

        config = self.load({
            "MEMORY_CORE_ENABLED": "true",
            "MEMORY_CANDIDATE_REVIEW_ENABLED": "true",
            "MEMORY_FINGERPRINT_KEY_ID": TEST_KEY_ID,
            "MEMORY_FINGERPRINT_HMAC_SECRET": TEST_HMAC_SECRET,
        })
        self.assertTrue(config.candidate_review_enabled)
        self.assertTrue(config.configuration_valid)
        self.assertFalse(config.context_injection_enabled)
        self.assertFalse(config.auto_formation_enabled)
        self.assertFalse(config.auto_candidate_persistence_enabled)
        self.assertFalse(config.explicit_writes_enabled)
        self.assertFalse(config.explicit_entry_enabled)
        self.assertFalse(config.sensitive_storage_enabled)

    def test_candidate_review_requires_valid_fingerprint_configuration(self):
        common = {
            "MEMORY_CORE_ENABLED": "true",
            "MEMORY_CANDIDATE_REVIEW_ENABLED": "true",
        }
        for values, category in (
            (
                {"MEMORY_FINGERPRINT_HMAC_SECRET": TEST_HMAC_SECRET},
                "memory_fingerprint_key_id_missing",
            ),
            (
                {"MEMORY_FINGERPRINT_KEY_ID": TEST_KEY_ID},
                "memory_fingerprint_hmac_secret_missing",
            ),
            (
                {
                    "MEMORY_FINGERPRINT_KEY_ID": "unsafe/key",
                    "MEMORY_FINGERPRINT_HMAC_SECRET": TEST_HMAC_SECRET,
                },
                "memory_fingerprint_key_id_invalid",
            ),
        ):
            with self.subTest(category=category):
                config = self.load({**common, **values})
                self.assertTrue(config.candidate_review_enabled)
                self.assertFalse(config.configuration_valid)
                self.assertEqual(config.error_category, category)

    def test_candidate_decisions_are_strict_default_off(self):
        for value in ("", "maybe", " true ", "enabled"):
            with self.subTest(value=value), self.assertRaisesRegex(
                deployment_config.DeploymentConfigError,
                r"^invalid_memory_candidate_decisions_enabled$",
            ):
                self.load({"MEMORY_CANDIDATE_DECISIONS_ENABLED": value})

    def test_candidate_decisions_require_only_core_review_and_profile(self):
        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            r"^memory_candidate_decisions_requires_core$",
        ):
            self.load({"MEMORY_CANDIDATE_DECISIONS_ENABLED": "true"})
        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            r"^memory_candidate_decisions_requires_candidate_review$",
        ):
            self.load({
                "MEMORY_CORE_ENABLED": "true",
                "MEMORY_CANDIDATE_DECISIONS_ENABLED": "true",
            })

        config = self.load({
            "MEMORY_CORE_ENABLED": "true",
            "MEMORY_CANDIDATE_REVIEW_ENABLED": "true",
            "MEMORY_CANDIDATE_DECISIONS_ENABLED": "true",
            "MEMORY_FINGERPRINT_KEY_ID": TEST_KEY_ID,
            "MEMORY_FINGERPRINT_HMAC_SECRET": TEST_HMAC_SECRET,
        })
        self.assertTrue(config.candidate_decisions_enabled)
        self.assertTrue(config.candidate_review_enabled)
        self.assertTrue(config.configuration_valid)
        self.assertFalse(config.context_injection_enabled)
        self.assertFalse(config.auto_formation_enabled)
        self.assertFalse(config.auto_candidate_persistence_enabled)
        self.assertFalse(config.explicit_writes_enabled)
        self.assertFalse(config.explicit_entry_enabled)
        self.assertFalse(config.sensitive_storage_enabled)

    def test_candidate_decisions_require_valid_fingerprint_configuration(self):
        config = self.load({
            "MEMORY_CORE_ENABLED": "true",
            "MEMORY_CANDIDATE_REVIEW_ENABLED": "true",
            "MEMORY_CANDIDATE_DECISIONS_ENABLED": "true",
        })
        self.assertTrue(config.candidate_decisions_enabled)
        self.assertFalse(config.configuration_valid)
        self.assertEqual(
            config.error_category,
            "memory_fingerprint_key_id_missing",
        )

    def test_auto_formation_is_strict_default_off_and_has_only_core_kelivo_dependencies(self):
        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            r"^invalid_memory_auto_formation_enabled$",
        ):
            self.load({"MEMORY_AUTO_FORMATION_ENABLED": "maybe"})
        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            r"^memory_auto_formation_requires_core$",
        ):
            self.load({
                "MEMORY_AUTO_FORMATION_ENABLED": "true",
                "KELIVO_ENABLED": "true",
            })
        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            r"^memory_auto_formation_requires_kelivo$",
        ):
            self.load({
                "MEMORY_AUTO_FORMATION_ENABLED": "true",
                "MEMORY_CORE_ENABLED": "true",
            })

        config = self.load({
            "MEMORY_AUTO_FORMATION_ENABLED": "true",
            "MEMORY_CORE_ENABLED": "true",
            "KELIVO_ENABLED": "true",
            "KELIVO_API_KEY": "test-kelivo-key-distinct-1234567890",
            "KELIVO_CLIENT_ID": "primary-kelivo",
            "KELIVO_API_SESSION": "shared-test-session",
            "KELIVO_MODEL_ALIAS": "ouou-home",
            "LLM_MODEL": "test-provider-model",
        })
        self.assertTrue(config.auto_formation_enabled)
        self.assertFalse(config.context_injection_enabled)
        self.assertFalse(config.smart_retrieval_enabled)
        self.assertFalse(config.explicit_writes_enabled)
        self.assertFalse(config.explicit_entry_enabled)
        self.assertFalse(config.sensitive_storage_enabled)

    def test_auto_candidate_persistence_is_strict_and_default_off(self):
        for value in ("", "maybe", " true ", "enabled", "真"):
            with self.subTest(value=value), self.assertRaisesRegex(
                deployment_config.DeploymentConfigError,
                r"^invalid_memory_auto_candidate_persistence_enabled$",
            ):
                self.load({
                    "MEMORY_AUTO_CANDIDATE_PERSISTENCE_ENABLED": value,
                })

        disabled = self.load({
            "MEMORY_AUTO_CANDIDATE_PERSISTENCE_ENABLED": "false",
        })
        self.assertFalse(disabled.auto_candidate_persistence_enabled)

    def test_auto_candidate_persistence_dependencies_are_fixed(self):
        common = {
            "MEMORY_AUTO_CANDIDATE_PERSISTENCE_ENABLED": "true",
            "MEMORY_AUTO_FORMATION_ENABLED": "true",
            "MEMORY_CORE_ENABLED": "true",
            "KELIVO_ENABLED": "true",
        }
        cases = (
            (
                {**common, "MEMORY_CORE_ENABLED": "false"},
                "memory_auto_candidate_persistence_requires_core",
            ),
            (
                {**common, "KELIVO_ENABLED": "false"},
                "memory_auto_candidate_persistence_requires_kelivo",
            ),
            (
                {**common, "MEMORY_AUTO_FORMATION_ENABLED": "false"},
                "memory_auto_candidate_persistence_requires_auto_formation",
            ),
        )
        for environ, category in cases:
            with self.subTest(category=category), self.assertRaisesRegex(
                deployment_config.DeploymentConfigError,
                f"^{category}$",
            ):
                self.load(environ)

    def test_auto_candidate_persistence_is_independent_of_explicit_and_sensitive(self):
        config = self.load({
            "MEMORY_AUTO_CANDIDATE_PERSISTENCE_ENABLED": "true",
            "MEMORY_AUTO_FORMATION_ENABLED": "true",
            "MEMORY_CORE_ENABLED": "true",
            "KELIVO_ENABLED": "true",
            "KELIVO_API_KEY": "test-kelivo-key-distinct-1234567890",
            "KELIVO_CLIENT_ID": "primary-kelivo",
            "KELIVO_API_SESSION": "shared-test-session",
            "KELIVO_MODEL_ALIAS": "ouou-home",
            "LLM_MODEL": "test-provider-model",
            "MEMORY_FINGERPRINT_KEY_ID": TEST_KEY_ID,
            "MEMORY_FINGERPRINT_HMAC_SECRET": TEST_HMAC_SECRET,
        })
        self.assertTrue(config.auto_candidate_persistence_enabled)
        self.assertTrue(config.auto_formation_enabled)
        self.assertTrue(config.configuration_valid)
        self.assertFalse(config.explicit_writes_enabled)
        self.assertFalse(config.sensitive_storage_enabled)

    def test_auto_candidate_persistence_invalid_fingerprint_profile_fails_closed(self):
        common = {
            "MEMORY_AUTO_CANDIDATE_PERSISTENCE_ENABLED": "true",
            "MEMORY_AUTO_FORMATION_ENABLED": "true",
            "MEMORY_CORE_ENABLED": "true",
            "KELIVO_ENABLED": "true",
            "KELIVO_API_KEY": "test-kelivo-key-distinct-1234567890",
            "KELIVO_CLIENT_ID": "primary-kelivo",
            "KELIVO_API_SESSION": "shared-test-session",
            "KELIVO_MODEL_ALIAS": "ouou-home",
            "LLM_MODEL": "test-provider-model",
        }
        cases = (
            (
                {"MEMORY_FINGERPRINT_HMAC_SECRET": TEST_HMAC_SECRET},
                "memory_fingerprint_key_id_missing",
            ),
            (
                {"MEMORY_FINGERPRINT_KEY_ID": TEST_KEY_ID},
                "memory_fingerprint_hmac_secret_missing",
            ),
            (
                {
                    "MEMORY_FINGERPRINT_KEY_ID": "unsafe/key",
                    "MEMORY_FINGERPRINT_HMAC_SECRET": TEST_HMAC_SECRET,
                },
                "memory_fingerprint_key_id_invalid",
            ),
        )
        for values, category in cases:
            with self.subTest(category=category):
                config = self.load({**common, **values})
                self.assertTrue(config.auto_candidate_persistence_enabled)
                self.assertFalse(config.configuration_valid)
                self.assertEqual(config.error_category, category)
                self.assertFalse(config.explicit_writes_enabled)

    def test_context_injection_is_strict_and_requires_core_and_kelivo(self):
        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            "invalid_memory_context_injection_enabled",
        ):
            self.load({"MEMORY_CONTEXT_INJECTION_ENABLED": "maybe"})
        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            "memory_context_injection_requires_core",
        ):
            self.load({
                "MEMORY_CONTEXT_INJECTION_ENABLED": "true",
                "KELIVO_ENABLED": "true",
            })
        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            "memory_context_injection_requires_kelivo",
        ):
            self.load({
                "MEMORY_CONTEXT_INJECTION_ENABLED": "true",
                "MEMORY_CORE_ENABLED": "true",
            })

    def test_context_injection_needs_no_writes_entry_or_sensitive_storage(self):
        config = self.load({
            "MEMORY_CONTEXT_INJECTION_ENABLED": "true",
            "MEMORY_CORE_ENABLED": "true",
            "KELIVO_ENABLED": "true",
            "KELIVO_API_KEY": "test-kelivo-key-distinct-1234567890",
            "KELIVO_CLIENT_ID": "primary-kelivo",
            "KELIVO_API_SESSION": "shared-test-session",
            "KELIVO_MODEL_ALIAS": "ouou-home",
            "LLM_MODEL": "test-provider-model",
        })
        self.assertTrue(config.context_injection_enabled)
        self.assertFalse(config.explicit_writes_enabled)
        self.assertFalse(config.explicit_entry_enabled)
        self.assertFalse(config.sensitive_storage_enabled)

    def test_smart_retrieval_is_strict_and_defaults_closed(self):
        for value in ("", "maybe", " true ", "真"):
            with self.subTest(value=value), self.assertRaisesRegex(
                deployment_config.DeploymentConfigError,
                r"^invalid_memory_smart_retrieval_enabled$",
            ):
                self.load({"MEMORY_SMART_RETRIEVAL_ENABLED": value})

        disabled = self.load({"MEMORY_SMART_RETRIEVAL_ENABLED": "false"})
        self.assertFalse(disabled.smart_retrieval_enabled)

        enabled = self.load({
            "MEMORY_SMART_RETRIEVAL_ENABLED": "true",
            "MEMORY_CONTEXT_INJECTION_ENABLED": "true",
            "MEMORY_CORE_ENABLED": "true",
            "KELIVO_ENABLED": "true",
            "KELIVO_API_KEY": "test-kelivo-key-distinct-1234567890",
            "KELIVO_CLIENT_ID": "primary-kelivo",
            "KELIVO_API_SESSION": "shared-test-session",
            "KELIVO_MODEL_ALIAS": "ouou-home",
            "LLM_MODEL": "test-provider-model",
        })
        self.assertTrue(enabled.smart_retrieval_enabled)
        self.assertFalse(enabled.explicit_writes_enabled)
        self.assertFalse(enabled.explicit_entry_enabled)
        self.assertFalse(enabled.sensitive_storage_enabled)

    def test_smart_retrieval_dependency_rules_are_fixed(self):
        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            r"^memory_smart_retrieval_requires_core$",
        ):
            self.load({"MEMORY_SMART_RETRIEVAL_ENABLED": "true"})

        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            r"^memory_smart_retrieval_requires_context_injection$",
        ):
            self.load({
                "MEMORY_SMART_RETRIEVAL_ENABLED": "true",
                "MEMORY_CORE_ENABLED": "true",
            })

        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            r"^memory_context_injection_requires_kelivo$",
        ):
            self.load({
                "MEMORY_SMART_RETRIEVAL_ENABLED": "true",
                "MEMORY_CONTEXT_INJECTION_ENABLED": "true",
                "MEMORY_CORE_ENABLED": "true",
            })

        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            r"^memory_context_injection_requires_core$",
        ):
            self.load({
                "MEMORY_SMART_RETRIEVAL_ENABLED": "false",
                "MEMORY_CONTEXT_INJECTION_ENABLED": "true",
                "KELIVO_ENABLED": "true",
            })

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
