from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import deployment_config
from backend import provider_model_migration as migration


OLD_MODEL = "[Legacy]provider-model"
NEW_MODEL = "provider-model"
URL = "https://provider.invalid/v1"
SECRET = "server-secret-never-returned"


def config_payload() -> dict:
    return {
        "history_n": 24,
        "main_chain": [
            {"url": URL, "key": SECRET, "model": OLD_MODEL},
        ],
        "sessions": [],
        "active_session": "",
    }


def provider_env(**overrides: str) -> dict[str, str]:
    env = {
        "LLM_API_BASE": URL,
        "LLM_API_KEY": SECRET,
        "LLM_MODEL": OLD_MODEL,
    }
    env.update(overrides)
    return env


class ProviderModelMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "loop.json"
        self.path.write_text(
            json.dumps(config_payload(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_model_only_migration_preserves_url_key_and_unrelated_config(self):
        before = json.loads(self.path.read_text(encoding="utf-8"))
        result = migration.migrate_primary_provider_model(
            self.path,
            {"model": NEW_MODEL},
        )
        after = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertEqual(result, {
            "ok": True,
            "contract_version": migration.CONTRACT_VERSION,
            "changed": True,
            "model": NEW_MODEL,
        })
        self.assertEqual(after["main_chain"][0]["model"], NEW_MODEL)
        self.assertEqual(after["main_chain"][0]["url"], URL)
        self.assertEqual(after["main_chain"][0]["key"], SECRET)
        self.assertEqual(
            {k: v for k, v in after.items() if k != "main_chain"},
            {k: v for k, v in before.items() if k != "main_chain"},
        )
        self.assertNotIn(SECRET, repr(result))
        self.assertNotIn(URL, repr(result))

    def test_same_model_is_noop_and_preserves_exact_file_bytes(self):
        before = self.path.read_bytes()
        result = migration.migrate_primary_provider_model(
            self.path,
            {"model": OLD_MODEL},
        )
        self.assertFalse(result["changed"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_file_materializes_validated_env_route_server_side(self):
        path = Path(self.temp.name) / "missing.json"
        result = migration.migrate_primary_provider_model(
            path,
            {"model": NEW_MODEL},
            environ=provider_env(),
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(result, {
            "ok": True,
            "contract_version": migration.CONTRACT_VERSION,
            "changed": True,
            "model": NEW_MODEL,
        })
        self.assertEqual(payload, {
            "main_chain": [
                {"url": URL, "key": SECRET, "model": NEW_MODEL},
            ],
        })
        self.assertNotIn(URL, repr(result))
        self.assertNotIn(SECRET, repr(result))

    def test_valid_config_without_materialized_route_uses_env_and_preserves_fields(self):
        states = (
            {
                "history_n": 17,
                "sessions": [{"id": "api-1", "title": "One"}],
                "active_session": "api-1",
            },
            {
                "history_n": 17,
                "main_chain": [],
                "sessions": [{"id": "api-1", "title": "One"}],
                "active_session": "api-1",
            },
        )
        for index, state in enumerate(states):
            path = Path(self.temp.name) / f"env-backed-{index}.json"
            path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.subTest(index=index):
                migration.migrate_primary_provider_model(
                    path,
                    {"model": NEW_MODEL},
                    environ=provider_env(),
                )
                after = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    {k: v for k, v in after.items() if k != "main_chain"},
                    {k: v for k, v in state.items() if k != "main_chain"},
                )
                self.assertEqual(after["main_chain"], [
                    {"url": URL, "key": SECRET, "model": NEW_MODEL},
                ])

    def test_env_backed_same_model_is_noop_and_preserves_file_absence(self):
        path = Path(self.temp.name) / "missing-noop.json"
        result = migration.migrate_primary_provider_model(
            path,
            {"model": OLD_MODEL},
            environ=provider_env(),
        )
        self.assertFalse(result["changed"])
        self.assertFalse(path.exists())

    def test_request_shape_and_model_validation_are_strict(self):
        invalid = (
            None,
            {},
            {"model": NEW_MODEL, "key": "forbidden"},
            {"model": ""},
            {"model": " leading"},
            {"model": "trailing "},
            {"model": "line\nbreak"},
            {"model": "x" * (migration.MAX_MODEL_CHARS + 1)},
            {"model": 123},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(
                migration.ProviderModelMigrationError
            ) as raised:
                migration.validate_model_request(payload)
            self.assertEqual(raised.exception.category, migration.INVALID_REQUEST)
            self.assertEqual(raised.exception.status_code, 400)

    def test_missing_malformed_or_multi_route_config_fails_data_free(self):
        cases = (
            None,
            "not-json",
            json.dumps({"main_chain": []}),
            json.dumps({
                "main_chain": [
                    {"url": URL, "key": SECRET, "model": OLD_MODEL},
                    {"url": URL, "key": "other-secret", "model": "other"},
                ]
            }),
        )
        for index, raw in enumerate(cases):
            path = Path(self.temp.name) / f"bad-{index}.json"
            if raw is not None:
                path.write_text(raw, encoding="utf-8")
            with self.subTest(index=index), self.assertRaises(
                migration.ProviderModelMigrationError
            ) as raised:
                migration.migrate_primary_provider_model(
                    path,
                    {"model": NEW_MODEL},
                    environ={},
                )
            self.assertEqual(raised.exception.category, migration.CONFIG_UNAVAILABLE)
            self.assertNotIn(SECRET, repr(raised.exception))

    def test_malformed_existing_config_never_falls_back_to_valid_env(self):
        malformed = (
            "not-json",
            json.dumps({"main_chain": "not-a-list"}),
            json.dumps({
                "main_chain": [
                    {"url": URL, "key": SECRET, "model": OLD_MODEL},
                    {"url": URL, "key": "other-secret", "model": "other"},
                ]
            }),
        )
        for index, raw in enumerate(malformed):
            path = Path(self.temp.name) / f"malformed-{index}.json"
            path.write_text(raw, encoding="utf-8")
            before = path.read_bytes()
            with self.subTest(index=index), self.assertRaises(
                migration.ProviderModelMigrationError
            ) as raised:
                migration.migrate_primary_provider_model(
                    path,
                    {"model": NEW_MODEL},
                    environ=provider_env(),
                )
            self.assertEqual(raised.exception.category, migration.CONFIG_UNAVAILABLE)
            self.assertEqual(path.read_bytes(), before)

    def test_env_fallback_routes_are_rejected_fail_closed(self):
        path = Path(self.temp.name) / "missing-fallback.json"
        for suffix in ("_2", "_3", "_4"):
            env = provider_env(**{
                f"LLM_API_BASE{suffix}": f"https://fallback{suffix}.invalid/v1",
                f"LLM_API_KEY{suffix}": f"fallback-secret{suffix}",
                f"LLM_MODEL{suffix}": f"fallback-model{suffix}",
            })
            with self.subTest(suffix=suffix), self.assertRaises(
                migration.ProviderModelMigrationError
            ) as raised:
                migration.migrate_primary_provider_model(
                    path,
                    {"model": NEW_MODEL},
                    environ=env,
                )
            self.assertEqual(raised.exception.category, migration.CONFIG_UNAVAILABLE)
            self.assertFalse(path.exists())

    def test_post_write_verification_failure_restores_original_config(self):
        before = self.path.read_text(encoding="utf-8")
        real_atomic = deployment_config.atomic_write_text
        calls = 0

        def corrupt_first_write(path, text):
            nonlocal calls
            calls += 1
            if calls == 1:
                real_atomic(path, json.dumps({
                    "main_chain": [{"url": URL, "key": "changed", "model": NEW_MODEL}]
                }) + "\n")
            else:
                real_atomic(path, text)

        with mock.patch.object(
            migration.deployment_config,
            "atomic_write_text",
            side_effect=corrupt_first_write,
        ):
            with self.assertRaises(migration.ProviderModelMigrationError) as raised:
                migration.migrate_primary_provider_model(
                    self.path,
                    {"model": NEW_MODEL},
                )
        self.assertEqual(raised.exception.category, migration.WRITE_FAILED)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_post_write_failure_restores_prior_file_absence(self):
        path = Path(self.temp.name) / "created-then-corrupt.json"
        real_atomic = deployment_config.atomic_write_text

        def corrupt_write(target, _text):
            real_atomic(target, json.dumps({
                "main_chain": [
                    {"url": URL, "key": "changed", "model": NEW_MODEL},
                ]
            }) + "\n")

        with mock.patch.object(
            migration.deployment_config,
            "atomic_write_text",
            side_effect=corrupt_write,
        ):
            with self.assertRaises(migration.ProviderModelMigrationError) as raised:
                migration.migrate_primary_provider_model(
                    path,
                    {"model": NEW_MODEL},
                    environ=provider_env(),
                )
        self.assertEqual(raised.exception.category, migration.WRITE_FAILED)
        self.assertFalse(path.exists())

    def test_written_candidate_still_satisfies_render_mvp_loop_contract(self):
        migration.migrate_primary_provider_model(self.path, {"model": NEW_MODEL})
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIs(
            deployment_config.validate_loop_config_payload(payload, render_mvp=True),
            payload,
        )


if __name__ == "__main__":
    unittest.main()
