from __future__ import annotations

import dataclasses
import inspect
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend import (
    channel_store,
    deployment_config,
    memory_candidate_review,
    memory_candidate_review_adapters,
    memory_candidate_review_composition,
    memory_policy,
)
from backend.tests._support import NoNetworkMixin


TEST_SECRET = "Synthetic-Candidate-HMAC-Key-2026-Alpha!Z9q7"
OTHER_SECRET = "Other-Synthetic-HMAC-Key-2026-Beta!Q8w6"
KEY_ID = "candidate-review-composition-key"


class MemoryCandidateReviewCompositionTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "review-composition.sqlite3"

    def environment(
        self,
        *,
        review: bool = True,
        core: bool = True,
        secret: str = TEST_SECRET,
        key_id: str = KEY_ID,
    ) -> dict[str, str]:
        return {
            "TELEGRAM_ENABLED": "false",
            "RELAY_DB": str(self.path),
            "MEMORY_CORE_ENABLED": "true" if core else "false",
            "MEMORY_CANDIDATE_REVIEW_ENABLED": "true" if review else "false",
            "MEMORY_EXPLICIT_WRITES_ENABLED": "false",
            "MEMORY_EXPLICIT_ENTRY_ENABLED": "false",
            "MEMORY_AUTO_FORMATION_ENABLED": "false",
            "MEMORY_AUTO_CANDIDATE_PERSISTENCE_ENABLED": "false",
            "KELIVO_ENABLED": "false",
            "MEMORY_FINGERPRINT_KEY_ID": key_id,
            "MEMORY_FINGERPRINT_HMAC_SECRET": secret,
        }

    def deployment(self, **kwargs) -> deployment_config.DeploymentConfig:
        return deployment_config.load_deployment_config(
            SimpleNamespace(requested=False, enabled=False),
            self.environment(**kwargs),
        )

    def database(self, *, profile: bool = True) -> None:
        with channel_store.connect(str(self.path)) as conn:
            conn.execute(channel_store.RELAY_TABLE_DDL["messages"])
        channel_store.run_migrations(str(self.path))
        if profile:
            with channel_store.connect(str(self.path)) as conn:
                stamp = channel_store.now_iso()
                conn.execute(
                    """INSERT INTO memory_fingerprint_profile
                       (singleton,key_id,key_check,normalization_version,
                        fingerprint_version,created_at,updated_at)
                       VALUES(1,?,?,?,?,?,?)""",
                    (
                        KEY_ID,
                        memory_policy.fingerprint_profile_check(TEST_SECRET),
                        memory_policy.NORMALIZATION_VERSION,
                        memory_policy.FINGERPRINT_VERSION,
                        stamp,
                        stamp,
                    ),
                )

    def assert_error(self, category: str, call, *args, **kwargs):
        with self.assertRaises(
            memory_candidate_review.MemoryCandidateReviewError
        ) as ctx:
            call(*args, **kwargs)
        self.assertEqual(ctx.exception.category, category)

    def test_standalone_import_does_not_load_app_or_write_capabilities(self):
        code = """
import sys
import backend.memory_candidate_review_composition
forbidden = (
    'backend.app', 'fastapi', 'backend.memory_runtime',
    'backend.memory_store', 'backend.memory_explicit_actions',
)
present = [name for name in forbidden if name in sys.modules]
raise SystemExit(1 if present else 0)
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        source = inspect.getsource(memory_candidate_review_composition)
        for forbidden in (
            "memory_runtime", "memory_store", "PrivilegedMemoryActions",
            "memory_explicit_actions", "memory_operator_composition",
            "FastAPI", "kelivo", "run_migrations", "BEGIN IMMEDIATE",
        ):
            self.assertNotIn(forbidden, source)

    def test_disabled_core_and_invalid_config_fail_before_reader_or_db(self):
        disabled = self.deployment(review=False)
        core_off = dataclasses.replace(
            disabled,
            memory=dataclasses.replace(
                disabled.memory,
                enabled=False,
                candidate_review_enabled=True,
            ),
        )
        invalid = self.deployment(secret="")
        self.assertFalse(invalid.memory.configuration_valid)
        with mock.patch.object(
            memory_candidate_review,
            "MemoryCandidateReviewReader",
            side_effect=AssertionError("reader constructed"),
        ):
            self.assert_error(
                "candidate_review_disabled",
                memory_candidate_review_composition.compose_candidate_review_capabilities,
                disabled,
            )
            self.assert_error(
                "candidate_review_configuration_invalid",
                memory_candidate_review_composition.compose_candidate_review_capabilities,
                core_off,
            )
            self.assert_error(
                "candidate_review_configuration_invalid",
                memory_candidate_review_composition.compose_candidate_review_capabilities,
                invalid,
            )
        self.assertFalse(self.path.exists())

    def test_environment_composition_fails_closed_before_open(self):
        telegram = SimpleNamespace(requested=False, enabled=False)
        cases = (
            (
                memory_candidate_review_composition
                .compose_operator_candidate_review_from_environment,
                self.environment(review=False),
                "candidate_review_disabled",
            ),
            (
                memory_candidate_review_composition
                .compose_mcp_candidate_review_from_environment,
                self.environment(core=False),
                "candidate_review_configuration_invalid",
            ),
            (
                memory_candidate_review_composition
                .compose_operator_candidate_review_from_environment,
                self.environment(secret=""),
                "candidate_review_configuration_invalid",
            ),
        )
        with mock.patch.object(
            memory_candidate_review,
            "MemoryCandidateReviewReader",
            side_effect=AssertionError("reader constructed"),
        ):
            for compose, environ, category in cases:
                with self.subTest(compose=compose.__name__, category=category):
                    self.assert_error(
                        category,
                        compose,
                        telegram,
                        environ,
                    )
        self.assertFalse(self.path.exists())

    def test_review_only_success_is_read_only_and_binds_both_origins(self):
        self.database()
        before = self.path.read_bytes()
        capabilities = (
            memory_candidate_review_composition
            .compose_candidate_review_capabilities(self.deployment())
        )
        self.assertEqual(
            repr(capabilities),
            "<MemoryCandidateReviewCapabilitiesV1>",
        )
        self.assertIsInstance(
            capabilities.service,
            memory_candidate_review.MemoryCandidateReviewService,
        )
        self.assertIsInstance(
            capabilities.operator_cli,
            memory_candidate_review_adapters.MemoryCandidateReviewAdapter,
        )
        self.assertIsInstance(
            capabilities.mcp,
            memory_candidate_review_adapters.MemoryCandidateReviewAdapter,
        )
        self.assertIs(capabilities.operator_cli._service, capabilities.service)
        self.assertIs(capabilities.mcp._service, capabilities.service)
        self.assertEqual(capabilities.operator_cli.list_candidates(), ())
        self.assertEqual(capabilities.mcp.list_candidates(), ())
        self.assertEqual(capabilities.service.readiness(), (True, ""))
        self.assertEqual(self.path.read_bytes(), before)
        leaked = repr(capabilities)
        self.assertNotIn(TEST_SECRET, leaked)
        self.assertNotIn(str(self.path), leaked)

    def test_environment_composition_loads_one_snapshot_and_returns_one_adapter(self):
        self.database()
        telegram = SimpleNamespace(requested=False, enabled=False)
        with mock.patch.object(
            deployment_config,
            "load_deployment_config",
            wraps=deployment_config.load_deployment_config,
        ) as loader:
            operator = (
                memory_candidate_review_composition
                .compose_operator_candidate_review_from_environment(
                    telegram,
                    self.environment(),
                )
            )
            loader.assert_called_once()
        with mock.patch.object(
            deployment_config,
            "load_deployment_config",
            wraps=deployment_config.load_deployment_config,
        ) as loader:
            mcp = (
                memory_candidate_review_composition
                .compose_mcp_candidate_review_from_environment(
                    telegram,
                    self.environment(),
                )
            )
            loader.assert_called_once()
        self.assertEqual(operator._origin, "operator_cli")
        self.assertEqual(mcp._origin, "mcp")

    def test_missing_db_profile_and_schema_fail_closed_without_repair(self):
        deployment = self.deployment()
        self.assert_error(
            "storage_unavailable",
            memory_candidate_review_composition.compose_candidate_review_capabilities,
            deployment,
        )
        self.assertFalse(self.path.exists())

        self.database(profile=False)
        before = self.path.read_bytes()
        self.assert_error(
            "candidate_review_profile_mismatch",
            memory_candidate_review_composition.compose_candidate_review_capabilities,
            deployment,
        )
        self.assertEqual(self.path.read_bytes(), before)
        with channel_store.connect(str(self.path)) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM memory_fingerprint_profile"
            ).fetchone()[0], 0)

        with channel_store.connect(str(self.path)) as conn:
            conn.execute("DROP INDEX idx_memory_candidate_sources_canonical")
        self.assert_error(
            "candidate_review_schema_invalid",
            memory_candidate_review_composition.compose_candidate_review_capabilities,
            deployment,
        )

    def test_profile_mismatch_and_candidate_corruption_readiness_semantics(self):
        self.database()
        capabilities = (
            memory_candidate_review_composition
            .compose_candidate_review_capabilities(self.deployment())
        )
        stamp = channel_store.now_iso()
        with channel_store.connect(str(self.path)) as conn:
            conn.execute(
                """INSERT INTO memory_items
                   (memory_key,kind,scope_type,scope_ref,normalized_content,
                    normalized_fingerprint,fingerprint_version,status,
                    explicitness,confidence,sensitivity,first_observed_at,
                    last_confirmed_at,created_at,updated_at)
                   VALUES(?,'project','global_user','','Project Atlas uses Python.',
                          zeroblob(32),1,'candidate','inferred',0.0,'normal',
                          ?,?,?,?)""",
                ("A" * 32, stamp, stamp, stamp, stamp),
            )
        self.assertEqual(capabilities.service.readiness(), (True, ""))
        self.assert_error(
            "candidate_review_state_invalid",
            capabilities.operator_cli.list_candidates,
        )
        with channel_store.connect(str(self.path)) as conn:
            conn.execute(
                "UPDATE memory_fingerprint_profile SET key_check=zeroblob(32)"
            )
        self.assertEqual(
            capabilities.service.readiness(),
            (False, "candidate_review_profile_mismatch"),
        )

    def test_dangerous_write_capabilities_are_never_called(self):
        self.database()
        from backend import memory_explicit_actions, memory_runtime, memory_store

        with (
            mock.patch.object(
                channel_store,
                "run_migrations",
                side_effect=AssertionError("migration called"),
            ),
            mock.patch.object(
                memory_runtime,
                "bootstrap_memory_runtime_from_environment",
                side_effect=AssertionError("runtime called"),
            ),
            mock.patch.object(
                memory_store,
                "MemoryStore",
                side_effect=AssertionError("store called"),
            ),
            mock.patch.object(
                memory_explicit_actions,
                "create_entry_backend",
                side_effect=AssertionError("entry backend called"),
            ),
        ):
            capabilities = (
                memory_candidate_review_composition
                .compose_candidate_review_capabilities(self.deployment())
            )
        self.assertEqual(capabilities.service.readiness(), (True, ""))


if __name__ == "__main__":
    unittest.main()
