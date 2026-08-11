from __future__ import annotations

import inspect
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend
from backend import channel_store, memory_policy
from backend.tests._support import NoNetworkMixin, load_app, request


TEST_SECRET = "Synthetic-Memory-HMAC-Key-2026-Alpha!Z9q7"
KEY_ID = "phase1-test-key"


class _TaskState:
    def done(self) -> bool:
        return False


class MemoryCandidateDecisionIntegrationTests(
    NoNetworkMixin,
    unittest.IsolatedAsyncioTestCase,
):
    def setUp(self):
        super().setUp()
        names = (
            "backend.app", "backend.telegram_integration",
            "backend.channel_store", "backend.kelivo_service",
            "backend.heartbeat_service", "backend.memory_policy",
            "backend.memory_runtime", "backend.memory_store",
            "backend.memory_service", "backend.memory_candidate_integrity",
            "backend.memory_candidate_decision_ledger",
            "backend.memory_candidate_decision_adapters",
            "backend.memory_candidate_decision_composition",
            "backend.memory_candidate_review",
            "backend.memory_candidate_review_adapters",
            "backend.memory_candidate_review_composition",
            "backend.memory_explicit_actions", "backend.memory_context",
            "backend.memory_context_integration", "backend.memory_retrieval",
            "backend.memory_formation_extractor",
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
        corrupt_candidate: bool = False,
    ) -> Path:
        path = Path(root) / "test-relay.sqlite3"
        with channel_store.connect(str(path)) as conn:
            for statement in channel_store.RELAY_TABLE_DDL.values():
                conn.execute(statement)
        channel_store.run_migrations(str(path))
        stamp = channel_store.now_iso()
        with channel_store.connect(str(path)) as conn:
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
            if corrupt_candidate:
                conn.execute(
                    """INSERT INTO memory_items
                       (memory_key,kind,scope_type,scope_ref,normalized_content,
                        normalized_fingerprint,fingerprint_version,status,
                        explicitness,confidence,sensitivity,first_observed_at,
                        last_confirmed_at,created_at,updated_at)
                       VALUES(?,'project','global_user','',
                              'corrupt candidate plaintext',zeroblob(32),1,
                              'candidate','inferred',0.0,'normal',?,?,?,?)""",
                    ("C" * 32, stamp, stamp, stamp, stamp),
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

    async def test_default_off_shape_globals_and_routes_are_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(root)
            response = await self.ready(module)
            routes = tuple(sorted(route.path for route in module.app.routes))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("memory_candidate_decisions", response.json()["checks"])
        self.assertNotIn(
            "memory_candidate_decisions",
            response.json().get("errors", {}),
        )
        self.assertIsNone(module.MEMORY_CANDIDATE_DECISION_SERVICE)
        self.assertIsNone(module.MEMORY_CANDIDATE_DECISION_OPERATOR)
        self.assertIsNone(module.MEMORY_CANDIDATE_DECISION_MCP)
        self.assertIsNone(module.MEMORY_CANDIDATE_DECISION_ERROR)
        self.assertFalse(any("candidate" in route.lower() for route in routes))

    async def test_review_only_preserves_read_only_authority_graph(self):
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
        self.assertIsNotNone(module.MEMORY_CANDIDATE_REVIEW_SERVICE)
        self.assertIsNone(module.MEMORY_PRIVILEGED_RUNTIME)
        self.assertIsNone(module.MEMORY_CANDIDATE_DECISION_SERVICE)
        self.assertIsNone(module.MEMORY_CANDIDATE_DECISION_OPERATOR)
        self.assertIsNone(module.MEMORY_CANDIDATE_DECISION_MCP)
        self.assertIsNone(module.MEMORY_CANDIDATE_DECISION_ERROR)
        self.assertIsNone(module.MEMORY_EXPLICIT_ENTRY_SERVICES)
        self.assertIsNone(module.MEMORY_CANDIDATE_PERSISTENCE)

    async def test_decision_only_mode_composes_one_privileged_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            self.seed_database(root)
            module = load_app(
                root,
                memory=True,
                memory_secret=TEST_SECRET,
                memory_candidate_review=True,
                memory_candidate_decisions=True,
            )
            response = await self.ready(module)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["checks"]["memory_candidate_review"])
        self.assertTrue(response.json()["checks"]["memory_candidate_decisions"])
        runtime = module.MEMORY_PRIVILEGED_RUNTIME
        self.assertIsNotNone(runtime)
        self.assertIs(module.MEMORY_CANDIDATE_DECISION_SERVICE, runtime.candidate_decisions)
        self.assertIs(
            module.MEMORY_CANDIDATE_DECISION_OPERATOR._writer,
            runtime.candidate_decisions,
        )
        self.assertIs(
            module.MEMORY_CANDIDATE_DECISION_MCP._writer,
            runtime.candidate_decisions,
        )
        self.assertIsNone(module.MEMORY_CANDIDATE_DECISION_ERROR)
        self.assertIsNone(module.MEMORY_EXPLICIT_ENTRY_SERVICES)
        self.assertIsNone(module.MEMORY_CANDIDATE_PERSISTENCE)

    async def test_dual_and_multi_capabilities_share_store_and_authority(self):
        cases = (
            {"memory_writes": True, "memory_entry": True},
            {
                "kelivo": True,
                "memory_auto_formation": True,
                "memory_candidate_persistence": True,
            },
            {
                "kelivo": True,
                "memory_writes": True,
                "memory_entry": True,
                "memory_auto_formation": True,
                "memory_candidate_persistence": True,
            },
        )
        for options in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as root:
                self.seed_database(root)
                module = load_app(
                    root,
                    memory=True,
                    memory_secret=TEST_SECRET,
                    memory_candidate_review=True,
                    memory_candidate_decisions=True,
                    **options,
                )
                response = await self.ready(module)
                self.assertEqual(response.status_code, 200)
                runtime = module.MEMORY_PRIVILEGED_RUNTIME
                writer = module.MEMORY_CANDIDATE_DECISION_SERVICE
                self.assertIs(writer, runtime.candidate_decisions)
                self.assertIs(writer._store, runtime.privileged_actions._store)
                self.assertIs(
                    writer._authority,
                    runtime.privileged_actions._authority,
                )
                if options.get("memory_candidate_persistence"):
                    self.assertIs(
                        module.MEMORY_CANDIDATE_PERSISTENCE._store,
                        writer._store,
                    )
                    self.assertIs(
                        module.MEMORY_CANDIDATE_PERSISTENCE._authority,
                        writer._authority,
                    )

    async def test_dynamic_schema_tamper_only_fails_decision_gate(self):
        with tempfile.TemporaryDirectory() as root:
            self.seed_database(root)
            module = load_app(
                root,
                memory=True,
                memory_secret=TEST_SECRET,
                memory_candidate_review=True,
                memory_candidate_decisions=True,
            )
            first = await self.ready(module)
            with module.channel_store.connect(module.DB_PATH) as conn:
                conn.execute(
                    "DROP TRIGGER memory_candidate_decisions_immutable_update"
                )
            second = await self.ready(module)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 503)
        self.assertTrue(second.json()["checks"]["memory_candidate_review"])
        self.assertFalse(second.json()["checks"]["memory_candidate_decisions"])
        self.assertEqual(
            second.json()["errors"]["memory_candidate_decisions"],
            "candidate_decision_schema_invalid",
        )

    async def test_dynamic_profile_tamper_fails_review_and_decision_gates(self):
        with tempfile.TemporaryDirectory() as root:
            self.seed_database(root)
            module = load_app(
                root,
                memory=True,
                memory_secret=TEST_SECRET,
                memory_candidate_review=True,
                memory_candidate_decisions=True,
            )
            first = await self.ready(module)
            with module.channel_store.connect(module.DB_PATH) as conn:
                conn.execute(
                    "UPDATE memory_fingerprint_profile SET key_id='wrong-key'"
                )
            second = await self.ready(module)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 503)
        self.assertFalse(second.json()["checks"]["memory_candidate_review"])
        self.assertFalse(second.json()["checks"]["memory_candidate_decisions"])
        self.assertEqual(
            second.json()["errors"]["memory_candidate_decisions"],
            "candidate_decision_profile_mismatch",
        )

    async def test_candidate_corruption_does_not_fail_readyz_but_decision_fails(self):
        with tempfile.TemporaryDirectory() as root:
            self.seed_database(root, corrupt_candidate=True)
            module = load_app(
                root,
                memory=True,
                memory_secret=TEST_SECRET,
                memory_candidate_review=True,
                memory_candidate_decisions=True,
            )
            response = await self.ready(module)
            request_model = (
                module.memory_candidate_decision_composition
                .memory_candidate_decision_adapters
                .ApproveCandidateRequestV1
            )
            with self.assertRaises(
                module.memory_candidate_decision_ledger
                .MemoryCandidateDecisionLedgerError
            ) as ctx:
                module.MEMORY_CANDIDATE_DECISION_OPERATOR.approve_candidate(
                    request_model("R" * 32, "C" * 32)
                )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["checks"]["memory_candidate_decisions"])
        self.assertEqual(ctx.exception.category, "candidate_decision_state_invalid")

    async def test_each_readyz_rechecks_review_and_writer_health(self):
        with tempfile.TemporaryDirectory() as root:
            self.seed_database(root)
            module = load_app(
                root,
                memory=True,
                memory_secret=TEST_SECRET,
                memory_candidate_review=True,
                memory_candidate_decisions=True,
            )
            review_type = type(module.MEMORY_CANDIDATE_REVIEW_SERVICE)
            writer_type = type(module.MEMORY_CANDIDATE_DECISION_SERVICE)
            original_review = review_type.readiness
            original_writer = writer_type.readiness
            calls = {"review": 0, "writer": 0}

            def review_probe(service):
                calls["review"] += 1
                return original_review(service)

            def writer_probe(writer):
                calls["writer"] += 1
                return original_writer(writer)

            with (
                mock.patch.object(review_type, "readiness", new=review_probe),
                mock.patch.object(writer_type, "readiness", new=writer_probe),
            ):
                first = await self.ready(module)
                second = await self.ready(module)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(calls, {"review": 2, "writer": 2})

    async def test_review_failure_blocks_decision_even_if_writer_is_healthy(self):
        with tempfile.TemporaryDirectory() as root:
            self.seed_database(root)
            module = load_app(
                root,
                memory=True,
                memory_secret=TEST_SECRET,
                memory_candidate_review=True,
                memory_candidate_decisions=True,
            )
            calls = []
            original = module.MEMORY_CANDIDATE_DECISION_SERVICE.readiness

            def writer_probe():
                calls.append(True)
                return original()

            module.MEMORY_CANDIDATE_REVIEW_SERVICE = None
            with mock.patch.object(
                module.MEMORY_CANDIDATE_DECISION_SERVICE,
                "readiness",
                new=writer_probe,
            ):
                response = await self.ready(module)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(calls, [True])
        self.assertFalse(response.json()["checks"]["memory_candidate_review"])
        self.assertFalse(response.json()["checks"]["memory_candidate_decisions"])

    async def test_composition_failure_is_bounded_and_preserves_other_graphs(self):
        secret_detail = "do-not-expose-composition-detail"
        with tempfile.TemporaryDirectory() as root:
            self.seed_database(root)
            module = load_app(
                root,
                memory=True,
                memory_writes=True,
                memory_entry=True,
                memory_secret=TEST_SECRET,
                memory_candidate_review=True,
                memory_candidate_decisions=True,
            )
            review = module.MEMORY_CANDIDATE_REVIEW_SERVICE
            explicit = module.MEMORY_EXPLICIT_ENTRY_SERVICES
            with mock.patch.object(
                module.memory_candidate_decision_composition,
                "compose_candidate_decisions",
                side_effect=RuntimeError(secret_detail),
            ):
                module._compose_memory_candidate_decisions()
            response = await self.ready(module)
        self.assertIs(module.MEMORY_CANDIDATE_REVIEW_SERVICE, review)
        self.assertIs(module.MEMORY_EXPLICIT_ENTRY_SERVICES, explicit)
        self.assertIsNone(module.MEMORY_CANDIDATE_DECISION_SERVICE)
        self.assertIsNone(module.MEMORY_CANDIDATE_DECISION_OPERATOR)
        self.assertIsNone(module.MEMORY_CANDIDATE_DECISION_MCP)
        self.assertEqual(
            module.MEMORY_CANDIDATE_DECISION_ERROR,
            "candidate_decision_state_invalid",
        )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(secret_detail, str(response.json()))

    async def test_routes_and_public_authority_surfaces_are_unchanged(self):
        with tempfile.TemporaryDirectory() as default_root:
            default = load_app(default_root)
            default_routes = tuple(sorted(route.path for route in default.app.routes))
        with tempfile.TemporaryDirectory() as decision_root:
            self.seed_database(decision_root)
            decision = load_app(
                decision_root,
                memory=True,
                memory_secret=TEST_SECRET,
                memory_candidate_review=True,
                memory_candidate_decisions=True,
            )
            decision_routes = tuple(
                sorted(route.path for route in decision.app.routes)
            )
        self.assertEqual(decision_routes, default_routes)
        for path in decision_routes:
            lowered = path.lower()
            self.assertFalse("candidate" in lowered or "/memory/" in lowered)

        app_source = inspect.getsource(decision)
        decision_lines = [
            line.lower()
            for line in app_source.splitlines()
            if "memory_candidate_decision" in line.lower()
        ]
        for line in decision_lines:
            for forbidden in (
                "telegram", "operit", "pwa", "kelivo", "create_task",
                ".decide(", "approve_candidate(", "reject_candidate(",
            ):
                self.assertNotIn(forbidden, line)

        review_adapter = (
            decision.memory_candidate_review_composition
            .memory_candidate_review_adapters.MemoryCandidateReviewAdapter
        )
        review_surface = {
            name
            for name, value in inspect.getmembers(review_adapter)
            if callable(value) and not name.startswith("_")
        }
        self.assertEqual(review_surface, {"list_candidates", "get_candidate"})
        actions = type(decision.MEMORY_PRIVILEGED_RUNTIME.privileged_actions)
        for forbidden in ("approve", "reject", "decide", "promote_candidate"):
            self.assertFalse(hasattr(actions, forbidden))


if __name__ == "__main__":
    unittest.main()
