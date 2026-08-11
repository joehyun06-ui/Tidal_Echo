from __future__ import annotations

import inspect
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend
from backend import channel_store, memory_candidate_review, memory_policy
from backend.tests._support import NoNetworkMixin, load_app, request


TEST_SECRET = "Synthetic-Memory-HMAC-Key-2026-Alpha!Z9q7"
KEY_ID = "phase1-test-key"


class _TaskState:
    def done(self) -> bool:
        return False


class MemoryCandidateReviewIntegrationTests(
    NoNetworkMixin,
    unittest.IsolatedAsyncioTestCase,
):
    """Fresh-process app integration that restores imported module identity."""

    def setUp(self):
        super().setUp()
        names = (
            "backend.app", "backend.telegram_integration",
            "backend.channel_store", "backend.kelivo_service",
            "backend.heartbeat_service", "backend.memory_policy",
            "backend.memory_runtime", "backend.memory_store",
            "backend.memory_service", "backend.memory_explicit_actions",
            "backend.memory_candidate_review",
            "backend.memory_candidate_review_adapters",
            "backend.memory_candidate_review_composition",
            "backend.memory_candidate_decision_adapters",
            "backend.memory_candidate_decision_composition",
            "backend.memory_context", "backend.memory_context_integration",
            "backend.memory_retrieval", "backend.memory_formation_extractor",
            "backend.memory_formation_integration",
        )
        missing = object()
        modules = {name: sys.modules.get(name, missing) for name in names}
        attributes = {
            name.rsplit(".", 1)[-1]: getattr(
                backend,
                name.rsplit(".", 1)[-1],
                missing,
            )
            for name in names
        }

        def restore_modules() -> None:
            for name in names:
                sys.modules.pop(name, None)
            for name, module in modules.items():
                if module is not missing:
                    sys.modules[name] = module
            for attribute in attributes:
                if hasattr(backend, attribute):
                    delattr(backend, attribute)
            for attribute, value in attributes.items():
                if value is not missing:
                    setattr(backend, attribute, value)

        self.addCleanup(restore_modules)

    def seed_database(
        self,
        root: str,
        *,
        profile: bool = True,
        invalid_candidate: bool = False,
    ) -> Path:
        path = Path(root) / "test-relay.sqlite3"
        with channel_store.connect(str(path)) as conn:
            for statement in channel_store.RELAY_TABLE_DDL.values():
                conn.execute(statement)
        channel_store.run_migrations(str(path))
        stamp = channel_store.now_iso()
        with channel_store.connect(str(path)) as conn:
            if profile:
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
            if invalid_candidate:
                conn.execute(
                    """INSERT INTO memory_items
                       (memory_key,kind,scope_type,scope_ref,normalized_content,
                        normalized_fingerprint,fingerprint_version,status,
                        explicitness,confidence,sensitivity,first_observed_at,
                        last_confirmed_at,created_at,updated_at)
                       VALUES(?,'project','global_user','',
                              'Project Atlas uses Python.',zeroblob(32),1,
                              'candidate','inferred',0.0,'normal',?,?,?,?)""",
                    ("A" * 32, stamp, stamp, stamp, stamp),
                )
        return path

    async def ready(self, module):
        module.app.state.telegram_worker_task = _TaskState()
        with mock.patch.object(
            module,
            "_api_loop_ready",
            new=mock.AsyncMock(return_value=True),
        ):
            return await request(module, "GET", "/readyz")

    async def test_default_off_has_no_globals_error_or_readyz_check(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(root)
            response = await self.ready(module)
        self.assertIsNone(module.MEMORY_CANDIDATE_REVIEW_SERVICE)
        self.assertIsNone(module.MEMORY_CANDIDATE_REVIEW_OPERATOR)
        self.assertIsNone(module.MEMORY_CANDIDATE_REVIEW_MCP)
        self.assertEqual(module.MEMORY_CANDIDATE_REVIEW_ERROR, "")
        self.assertNotIn("memory_candidate_review", response.json()["checks"])
        self.assertNotIn("memory_candidate_review", response.json().get("errors", {}))

    async def test_review_only_composes_independently_and_is_ready(self):
        with tempfile.TemporaryDirectory() as root:
            self.seed_database(root)
            module = load_app(
                root,
                memory=True,
                memory_secret=TEST_SECRET,
                memory_candidate_review=True,
            )
            response = await self.ready(module)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["checks"]["memory_candidate_review"])
        self.assertIsInstance(
            module.MEMORY_CANDIDATE_REVIEW_SERVICE,
            module.memory_candidate_review.MemoryCandidateReviewService,
        )
        self.assertIsNotNone(module.MEMORY_CANDIDATE_REVIEW_OPERATOR)
        self.assertIsNotNone(module.MEMORY_CANDIDATE_REVIEW_MCP)
        self.assertEqual(module.MEMORY_CANDIDATE_REVIEW_ERROR, "")
        self.assertIsNone(module.MEMORY_PRIVILEGED_RUNTIME)
        self.assertIsNone(module.MEMORY_CANDIDATE_PERSISTENCE)
        self.assertIsNone(module.MEMORY_EXPLICIT_ENTRY_SERVICES)

    async def test_review_only_never_bootstraps_privileged_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            self.seed_database(root)
            module = load_app(
                root,
                memory=True,
                memory_secret=TEST_SECRET,
                memory_candidate_review=True,
            )
            with mock.patch.object(
                module.memory_runtime,
                "bootstrap_memory_runtime_from_environment",
                side_effect=AssertionError("privileged runtime bootstrapped"),
            ):
                module.init_db()
                response = await self.ready(module)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["checks"]["memory_candidate_review"])
        self.assertIsNone(module.MEMORY_PRIVILEGED_RUNTIME)

    async def test_missing_profile_is_not_initialized_and_blocks_readyz(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(
                root,
                memory=True,
                memory_secret=TEST_SECRET,
                memory_candidate_review=True,
            )
            response = await self.ready(module)
            with module.channel_store.connect(module.DB_PATH) as conn:
                profile_count = conn.execute(
                    "SELECT count(*) FROM memory_fingerprint_profile"
                ).fetchone()[0]
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["checks"]["memory_candidate_review"])
        self.assertEqual(
            response.json()["errors"]["memory_candidate_review"],
            "candidate_review_profile_mismatch",
        )
        self.assertEqual(
            module.MEMORY_CANDIDATE_REVIEW_ERROR,
            "candidate_review_profile_mismatch",
        )
        self.assertEqual(profile_count, 0)
        self.assertIsNone(module.MEMORY_CANDIDATE_REVIEW_SERVICE)

    async def test_profile_tamper_after_startup_is_detected_on_next_readyz(self):
        with tempfile.TemporaryDirectory() as root:
            self.seed_database(root)
            module = load_app(
                root,
                memory=True,
                memory_secret=TEST_SECRET,
                memory_candidate_review=True,
            )
            first = await self.ready(module)
            with module.channel_store.connect(module.DB_PATH) as conn:
                conn.execute(
                    "UPDATE memory_fingerprint_profile SET key_id='other-key'"
                )
            second = await self.ready(module)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["checks"]["memory_candidate_review"])
        self.assertEqual(second.status_code, 503)
        self.assertFalse(second.json()["checks"]["memory_candidate_review"])
        self.assertEqual(
            second.json()["errors"]["memory_candidate_review"],
            "candidate_review_profile_mismatch",
        )

    async def test_invalid_candidate_does_not_fail_readyz_but_list_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            self.seed_database(root, invalid_candidate=True)
            module = load_app(
                root,
                memory=True,
                memory_secret=TEST_SECRET,
                memory_candidate_review=True,
            )
            response = await self.ready(module)
            with self.assertRaises(
                module.memory_candidate_review.MemoryCandidateReviewError
            ) as ctx:
                module.MEMORY_CANDIDATE_REVIEW_OPERATOR.list_candidates()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["checks"]["memory_candidate_review"])
        self.assertEqual(ctx.exception.category, "candidate_review_state_invalid")

    async def test_review_and_explicit_capabilities_do_not_share_authority(self):
        with tempfile.TemporaryDirectory() as root:
            self.seed_database(root)
            module = load_app(
                root,
                memory=True,
                memory_writes=True,
                memory_entry=True,
                memory_secret=TEST_SECRET,
                memory_candidate_review=True,
            )
            response = await self.ready(module)
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(module.MEMORY_PRIVILEGED_RUNTIME)
        self.assertIsNotNone(module.MEMORY_EXPLICIT_ENTRY_SERVICES)
        self.assertIsNotNone(module.MEMORY_CANDIDATE_REVIEW_SERVICE)
        self.assertIsNot(
            module.MEMORY_CANDIDATE_REVIEW_SERVICE,
            module.MEMORY_PRIVILEGED_RUNTIME,
        )
        reader = module.MEMORY_CANDIDATE_REVIEW_SERVICE._reader
        for forbidden in ("_authority", "_store", "_runtime", "_actions"):
            self.assertFalse(hasattr(reader, forbidden))
        self.assertIs(
            module.MEMORY_CANDIDATE_REVIEW_OPERATOR._service,
            module.MEMORY_CANDIDATE_REVIEW_SERVICE,
        )

    async def test_review_and_persistence_capabilities_do_not_share_authority(self):
        with tempfile.TemporaryDirectory() as root:
            self.seed_database(root)
            module = load_app(
                root,
                kelivo=True,
                memory=True,
                memory_auto_formation=True,
                memory_candidate_persistence=True,
                memory_secret=TEST_SECRET,
                memory_candidate_review=True,
            )
            response = await self.ready(module)
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(module.MEMORY_PRIVILEGED_RUNTIME)
        self.assertIsNotNone(module.MEMORY_CANDIDATE_PERSISTENCE)
        self.assertIsNotNone(module.MEMORY_CANDIDATE_REVIEW_SERVICE)
        self.assertIsNot(
            module.MEMORY_CANDIDATE_REVIEW_SERVICE,
            module.MEMORY_CANDIDATE_PERSISTENCE,
        )
        self.assertIs(
            module.MEMORY_CANDIDATE_REVIEW_MCP._service,
            module.MEMORY_CANDIDATE_REVIEW_SERVICE,
        )

    async def test_review_adds_no_http_route_or_telegram_operit_binding(self):
        with tempfile.TemporaryDirectory() as default_root:
            default = load_app(default_root)
            default_routes = tuple(sorted(route.path for route in default.app.routes))
        with tempfile.TemporaryDirectory() as review_root:
            self.seed_database(review_root)
            review = load_app(
                review_root,
                memory=True,
                memory_secret=TEST_SECRET,
                memory_candidate_review=True,
            )
            review_routes = tuple(sorted(route.path for route in review.app.routes))
        self.assertEqual(review_routes, default_routes)
        for route in review_routes:
            lowered = route.lower()
            self.assertFalse(lowered.startswith("/memory/"))
            self.assertFalse(lowered.startswith("/candidate/"))
            self.assertFalse(lowered.startswith("/review/"))
            self.assertFalse(lowered.startswith("/mcp/"))
        source = inspect.getsource(review)
        for integration in (
            "TelegramWorker", "operit_share", "kelivo_service",
        ):
            occurrences = [
                line for line in source.splitlines()
                if "MEMORY_CANDIDATE_REVIEW" in line and integration in line
            ]
            self.assertEqual(occurrences, [])

    async def test_invalid_review_configuration_is_bounded_not_import_fatal(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(
                root,
                memory=True,
                memory_secret="",
                memory_candidate_review=True,
            )
            response = await self.ready(module)
        self.assertEqual(
            module.MEMORY_CANDIDATE_REVIEW_ERROR,
            "candidate_review_configuration_invalid",
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["errors"]["memory_candidate_review"],
            "candidate_review_configuration_invalid",
        )


if __name__ == "__main__":
    unittest.main()
