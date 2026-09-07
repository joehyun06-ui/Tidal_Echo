from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import io
import json
import os
import sqlite3
import threading
import types
import unittest
from contextlib import ExitStack, contextmanager, redirect_stderr
from pathlib import Path
from unittest import mock

from backend import (
    memory_index_outbox_consumer as outbox,
    memory_index_refresh_worker as worker,
    memory_policy,
    memory_retrieval_hybrid_observability as observability,
    memory_retrieval_hybrid_query as query,
    memory_retrieval_hybrid_runtime_active as active,
    memory_retrieval_hybrid_runtime_composition as composition,
    memory_retrieval_hybrid_runtime_shadow as shadow,
)
from backend.tests._support import NoNetworkMixin
from backend.tests.test_memory_index_refresh_worker import (
    EMBEDDING_KEY,
    FINGERPRINT_SECRET,
    MEMORY_KEY,
    PRIVATE_CONTENT,
    TERM_SECRET,
    MemoryIndexRefreshFixture,
    RecordingEmbedding,
    enabled_env,
)


QUERY = "PostgreSQL project"


class QueryEmbedding:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    async def __call__(self, texts, model, dimensions):
        self.calls.append((texts, model, dimensions))
        if self.failure is not None:
            raise self.failure
        return (tuple([1.0] + [0.0] * (dimensions - 1)),)


class WorkerReadOnlyTests(
    MemoryIndexRefreshFixture, NoNetworkMixin, unittest.IsolatedAsyncioTestCase
):
    def readonly(self, writer, embedding=None, **config_overrides):
        if embedding is None:
            embedding = QueryEmbedding()
        config = dataclasses.replace(
            writer.config,
            embedding_adapter=embedding,
            **config_overrides,
        )
        return composition.HybridRetrievalReadOnlyRunnerV1(
            config=config, reader=composition._reader(config)
        )

    async def current_pair(self, *, empty=False):
        if not empty:
            self.seed_atomic()
        self.enqueue_dirty()
        writer = self.runner(RecordingEmbedding())
        await worker.drain_once_v1(self.db_path, writer)
        return writer

    def signatures(self, config):
        paths = (self.db_path, config.bm25_path, config.vector_path)
        return tuple(
            (path.exists(), hashlib.sha256(path.read_bytes()).hexdigest())
            if path.exists() else (False, None)
            for path in paths
        )

    @contextmanager
    def forbid_query_writes(self):
        # Assert calls as well as throwing: shadow intentionally catches errors.
        targets = (
            (composition, "_commit_pair"),
            (composition, "_initialize_bm25"),
            (composition, "_initialize_vector"),
            (composition, "_unlink_disposable"),
            (composition, "_identity_matches"),
            (composition.HybridRetrievalShadowRunnerV1, "_rebuild_pair"),
            (composition.HybridRetrievalShadowRunnerV1, "reconcile_index_pair_v1"),
            (composition.vector, "build_vector_index_v1"),
            (composition.bm25_store, "_connect"),
            (composition.bm25_store, "initialize_bm25_store"),
            (composition.bm25_store, "apply_bm25_index_plan"),
            (composition.vector_store, "_connect"),
            (composition.vector_store, "initialize_vector_store"),
            (composition.vector_store, "apply_vector_index_plan"),
            (outbox, "peek_pending_batch_v1"),
            (outbox, "complete_pending_batch_v1"),
            (Path, "unlink"),
        )
        with ExitStack() as stack:
            guards = [
                stack.enter_context(mock.patch.object(
                    module, name, side_effect=AssertionError("query must be read-only")
                ))
                for module, name in targets
            ]
            yield
            for guard in guards:
                guard.assert_not_called()

    async def fails_without_writes(self, reader, category):
        before = self.signatures(reader.config)
        events = self.rows()
        with self.forbid_query_writes():
            with self.assertRaises(query.MemoryRetrievalHybridQueryError) as raised:
                await reader(query_text=QUERY)
        self.assertEqual(raised.exception.category, category)
        self.assertEqual(reader.config.embedding_adapter.calls, [])
        self.assertEqual(self.signatures(reader.config), before)
        self.assertEqual(self.rows(), events)

    def change_atomic(self):
        content = "This private test project uses PostgreSQL 17."
        fingerprint = memory_policy.fingerprint_content(
            FINGERPRINT_SECRET,
            scope_type="global_user",
            scope_ref="",
            kind="project",
            normalized_content=content,
        )
        with self.module.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE memory_items
                   SET normalized_content=?,normalized_fingerprint=?,updated_at=?
                   WHERE memory_key=?""",
                (content, fingerprint, "2026-09-07T12:00:00+00:00", MEMORY_KEY),
            )
            self.module.channel_store.enqueue_memory_index_dirty(
                conn, created_at="2026-09-07T12:00:00+00:00"
            )
            conn.execute("COMMIT")

    def prepare_context(self):
        self.relay.DEPLOYMENT.memory.context_injection_enabled = True
        self.relay.DEPLOYMENT.memory.smart_retrieval_enabled = True
        dispatch = self.module.memory_context_integration.TransientMemoryDispatch(
            provider_messages=({"role": "user", "content": QUERY},),
            memory_applied=False,
            authoritative_memory_keys=(),
        )
        original = mock.Mock(return_value=dispatch)
        self.relay.memory_context_integration = types.SimpleNamespace(
            prepare_transient_memory_dispatch=original
        )
        return dispatch, original

    async def test_current_pair_queries_only_exact_query_with_readonly_connections(self):
        writer = await self.current_pair()
        reader = self.readonly(writer)
        before = self.signatures(reader.config)
        events = self.rows()
        original_connect = sqlite3.connect
        connections = []

        def connect(database, *args, **kwargs):
            connections.append(str(database))
            self.assertIn("mode=ro", str(database))
            self.assertTrue(kwargs.get("uri"))
            return original_connect(database, *args, **kwargs)

        with self.forbid_query_writes(), mock.patch.object(
            sqlite3, "connect", side_effect=connect
        ):
            for _ in range(3):
                result = await reader(query_text=QUERY)
                self.assertTrue(result.query_embedding_performed)
                self.assertEqual(result.fusion_result.hits[0].memory_key, MEMORY_KEY)
        self.assertGreaterEqual(len(connections), 9)
        self.assertEqual(reader.config.embedding_adapter.calls, [
            ((QUERY,), reader.config.embedding_model, reader.config.embedding_dimensions)
        ] * 3)
        self.assertEqual(writer.config.embedding_adapter.calls, 1)
        self.assertEqual(self.signatures(reader.config), before)
        self.assertEqual(self.rows(), events)
        for name in ("_rebuild_pair", "reconcile_index_pair_v1", "_commit_pair"):
            self.assertFalse(hasattr(reader, name))
        self.assertNotIsInstance(reader, composition.HybridRetrievalShadowRunnerV1)

    async def test_missing_pair_never_creates_sidecars(self):
        self.seed_atomic()
        reader = self.readonly(self.runner(RecordingEmbedding()))
        await self.fails_without_writes(reader, "hybrid_query_bm25_invalid")

    async def test_missing_each_sidecar_is_not_repaired_by_query(self):
        writer = await self.current_pair()
        for path, category in (
            (writer.config.bm25_path, "hybrid_query_bm25_invalid"),
            (writer.config.vector_path, "hybrid_query_vector_invalid"),
        ):
            with self.subTest(category=category):
                path.unlink()
                await self.fails_without_writes(self.readonly(writer), category)
                await writer.reconcile_index_pair_v1()

    async def test_corrupt_each_sidecar_is_not_repaired_by_query(self):
        writer = await self.current_pair()
        for path, category in (
            (writer.config.bm25_path, "hybrid_query_bm25_invalid"),
            (writer.config.vector_path, "hybrid_query_vector_invalid"),
        ):
            with self.subTest(category=category):
                path.write_bytes(b"corrupt C6 synthetic sidecar")
                await self.fails_without_writes(self.readonly(writer), category)
                await writer.reconcile_index_pair_v1()

    async def test_stale_authority_leaves_outbox_pending_until_worker_repairs(self):
        writer = await self.current_pair()
        reader = self.readonly(writer)
        self.change_atomic()
        await self.fails_without_writes(reader, "hybrid_query_stale")
        self.assertIsNone(self.rows()[-1][1])
        await worker.drain_once_v1(self.db_path, writer)
        with self.forbid_query_writes():
            result = await reader(query_text=QUERY)
        self.assertTrue(result.query_embedding_performed)
        self.assertIsNotNone(self.rows()[-1][1])

    async def test_identity_mismatch_rejected_locally_before_query_embedding(self):
        writer = await self.current_pair()
        cases = (
            ({"embedding_model": "hybrid-embed-" + "f" * 40}, "hybrid_query_vector_invalid"),
            ({"embedding_dimensions": 3}, "hybrid_query_vector_invalid"),
            ({"term_key_id": "c6-different-key"}, "hybrid_query_bm25_invalid"),
            ({"term_hmac_secret": "C6-Different-Term-Secret-0123456789-AbCd!"},
             "hybrid_query_bm25_invalid"),
        )
        for overrides, category in cases:
            with self.subTest(overrides=tuple(overrides)):
                await self.fails_without_writes(
                    self.readonly(writer, **overrides), category
                )

    async def test_empty_pair_still_requires_current_model_and_dimensions(self):
        writer = await self.current_pair(empty=True)
        for overrides in (
            {"embedding_model": "hybrid-embed-" + "f" * 40},
            {"embedding_dimensions": 3},
        ):
            await self.fails_without_writes(
                self.readonly(writer, **overrides), "hybrid_query_vector_invalid"
            )
        reader = self.readonly(writer)
        with self.forbid_query_writes():
            result = await reader(query_text=QUERY)
        self.assertFalse(result.query_embedding_performed)
        self.assertEqual(result.fusion_result.hits, ())
        self.assertEqual(reader.config.embedding_adapter.calls, [])

    async def test_forged_atomic_revision_is_rejected_before_provider(self):
        writer = await self.current_pair()
        stored = composition.vector_store.load_vector_store_snapshot(writer.config.vector_path)
        plan = dataclasses.replace(stored.plan, documents=(
            dataclasses.replace(stored.plan.documents[0], atomic_revision_digest="f" * 64),
        ))
        composition.vector_store.apply_vector_index_plan(writer.config.vector_path, plan)
        await self.fails_without_writes(self.readonly(writer), "hybrid_query_vector_invalid")

    async def test_query_provider_failure_and_cancellation_never_rebuild(self):
        writer = await self.current_pair()
        for failure in (RuntimeError("private provider body"), asyncio.CancelledError()):
            reader = self.readonly(writer, QueryEmbedding(failure))
            before = self.signatures(reader.config)
            expected = (
                asyncio.CancelledError if isinstance(failure, asyncio.CancelledError)
                else query.MemoryRetrievalHybridQueryError
            )
            with self.forbid_query_writes(), self.assertRaises(expected) as raised:
                await reader(query_text=QUERY)
            if not isinstance(failure, asyncio.CancelledError):
                self.assertEqual(raised.exception.category, "hybrid_query_embedding_failed")
                self.assertNotIn("private provider", str(raised.exception))
            self.assertEqual(len(reader.config.embedding_adapter.calls), 1)
            self.assertEqual(self.signatures(reader.config), before)

    async def test_half_committed_pair_fails_shadow_then_recovers_after_worker_commit(self):
        writer = await self.current_pair()
        reader = self.readonly(writer)
        self.change_atomic()
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        original_apply = composition.vector_store.apply_vector_index_plan

        def blocked_apply(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(timeout=10):
                raise AssertionError("test did not release pair commit")
            return original_apply(*args, **kwargs)

        with mock.patch.object(
            composition.vector_store, "apply_vector_index_plan", side_effect=blocked_apply
        ):
            refresh = asyncio.create_task(worker.drain_once_v1(self.db_path, writer))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                # BM25 is new, vector is old, and outbox acknowledgement is still pending.
                await self.fails_without_writes(reader, "hybrid_query_stale")
                tracker = observability.HybridShadowObservabilityV1()
                logs = io.StringIO()
                with self.forbid_query_writes(), redirect_stderr(logs):
                    await shadow._run_shadow(
                        self.relay, reader, query_text=QUERY,
                        authoritative_memory_keys=(MEMORY_KEY,), tracker=tracker,
                    )
                self.assertEqual(tracker.snapshot().failed_count, 1)
                self.assertIn("status=failed", logs.getvalue())
                self.assertEqual(reader.config.embedding_adapter.calls, [])
                self.assertIsNone(self.rows()[-1][1])
                for private in (QUERY, MEMORY_KEY, PRIVATE_CONTENT, str(self.db_path)):
                    self.assertNotIn(private, logs.getvalue())
            finally:
                release.set()
                await asyncio.wait_for(refresh, timeout=5)
        with self.forbid_query_writes():
            result = await reader(query_text=QUERY)
        self.assertTrue(result.query_embedding_performed)
        self.assertEqual(len(reader.config.embedding_adapter.calls), 1)
        self.assertIsNotNone(self.rows()[-1][1])

    async def test_each_sidecar_load_keeps_one_read_snapshot_during_concurrent_commit(self):
        writer = await self.current_pair()
        for store, path, load_name, apply_name in (
            (composition.bm25_store, writer.config.bm25_path,
             "load_bm25_store_snapshot", "apply_bm25_index_plan"),
            (composition.vector_store, writer.config.vector_path,
             "load_vector_store_snapshot", "apply_vector_index_plan"),
        ):
            with self.subTest(store=store.__name__):
                load = getattr(store, load_name)
                apply = getattr(store, apply_name)
                old = load(path)
                changed = dataclasses.replace(old.plan, source_snapshot_digest="d" * 64)
                # WAL is test-only so the second connection can commit before
                # this read completes. Runtime journal configuration is unchanged.
                conn = sqlite3.connect(path)
                try:
                    self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
                finally:
                    conn.close()
                validate = store._validate_schema
                committed = []

                def interleave(read_conn):
                    meta = validate(read_conn)
                    if not committed:
                        self.assertTrue(read_conn.in_transaction)
                        committed.append(True)
                        apply(path, changed)
                    return meta

                with mock.patch.object(store, "_validate_schema", side_effect=interleave):
                    observed = load(path)
                self.assertEqual(observed, old)
                self.assertEqual(load(path).plan, changed)

    def test_factory_gate_matrix_and_off_noop(self):
        for worker_on in (False, True):
            for shadow_on in (False, True):
                with self.subTest(worker=worker_on, shadow=shadow_on):
                    env = enabled_env(**{
                        worker.ENV_GATE: str(worker_on).lower(),
                        shadow.ENV_GATE: str(shadow_on).lower(),
                    })
                    runner = composition.compose_hybrid_retrieval_shadow_runner_v1(self.relay, env)
                    if not shadow_on:
                        self.assertIsNone(runner)
                    else:
                        expected = (composition.HybridRetrievalReadOnlyRunnerV1 if worker_on
                                    else composition.HybridRetrievalShadowRunnerV1)
                        self.assertIs(type(runner), expected)
        self.assertIsNone(composition.compose_hybrid_retrieval_shadow_runner_v1(
            self.relay,
            {shadow.ENV_GATE: "false", worker.ENV_GATE: "invalid",
             composition.TERM_SECRET_ENV: "invalid"},
        ))
        self.assertFalse((self.root / composition.BM25_FILENAME).exists())
        self.assertFalse((self.root / composition.VECTOR_FILENAME).exists())

    def test_installed_worker_selects_readonly_even_if_environment_is_later_off(self):
        writer = self.runner(RecordingEmbedding())
        worker.install(self.relay, environ=enabled_env(), runner=writer)
        runner = composition.compose_hybrid_retrieval_shadow_runner_v1(
            self.relay, enabled_env(**{worker.ENV_GATE: "false", shadow.ENV_GATE: "true"})
        )
        self.assertIs(type(runner), composition.HybridRetrievalReadOnlyRunnerV1)

    def test_worker_backed_install_rejects_legacy_or_arbitrary_callable_before_patch(self):
        self.prepare_context()
        writer = self.runner(RecordingEmbedding())
        worker.install(self.relay, environ=enabled_env(), runner=writer)
        original_prepare = self.relay.memory_context_integration.prepare_transient_memory_dispatch
        original_lifespan = self.relay.app.router.lifespan_context
        for invalid in (writer, lambda **kwargs: None):
            with mock.patch.dict(os.environ, enabled_env(**{shadow.ENV_GATE: "true"}), clear=True):
                with self.assertRaises(shadow.MemoryHybridRetrievalRuntimeShadowError) as raised:
                    shadow.install(self.relay, runner=invalid)
            self.assertEqual(raised.exception.category, "memory_hybrid_retrieval_shadow_requires_readonly_runner")
            self.assertIs(self.relay.memory_context_integration.prepare_transient_memory_dispatch, original_prepare)
            self.assertIs(self.relay.app.router.lifespan_context, original_lifespan)
            self.assertFalse(getattr(self.relay, shadow.INSTALL_MARKER, False))

    def test_readonly_install_requires_worker_installed_first(self):
        self.prepare_context()
        reader = self.readonly(self.runner(RecordingEmbedding()))
        for worker_on in ("false", "true"):
            with mock.patch.dict(os.environ, enabled_env(**{
                shadow.ENV_GATE: "true", worker.ENV_GATE: worker_on,
            }), clear=True):
                with self.assertRaises(shadow.MemoryHybridRetrievalRuntimeShadowError) as raised:
                    shadow.install(self.relay, runner=reader)
            self.assertEqual(raised.exception.category, "memory_hybrid_retrieval_shadow_requires_index_worker")

    def test_preinstalled_legacy_shadow_cannot_be_promoted_to_worker_in_place(self):
        self.prepare_context()
        writer = self.runner(RecordingEmbedding())
        with mock.patch.dict(os.environ, enabled_env(**{
            shadow.ENV_GATE: "true", worker.ENV_GATE: "false",
        }), clear=True):
            self.assertTrue(shadow.install(self.relay, runner=writer))
        before = self.relay.app.router.lifespan_context
        self.assertFalse(getattr(self.relay, shadow.READONLY_MARKER))
        with self.assertRaises(worker.MemoryIndexRefreshWorkerError) as raised:
            worker.install(self.relay, environ=enabled_env(), runner=writer)
        self.assertEqual(raised.exception.category, "memory_index_refresh_conflicts_runtime")
        self.assertIs(self.relay.app.router.lifespan_context, before)
        self.assertFalse(getattr(self.relay, worker.INSTALL_MARKER, False))

    def test_worker_and_active_reject_each_other_from_env_or_installed_marker(self):
        for marker in (False, True):
            with self.subTest(marker=marker):
                setattr(self.relay, worker.ENABLED_MARKER, marker)
                env = enabled_env(**{
                    worker.ENV_GATE: "false" if marker else "true",
                    active.ENV_GATE: "true",
                })
                before = self.relay.app.router.lifespan_context
                with self.assertRaises(active.MemoryHybridRetrievalRuntimeActiveError) as raised:
                    active.install(self.relay, environ=env)
                self.assertEqual(raised.exception.category, "memory_hybrid_active_conflicts_index_worker")
                self.assertFalse(getattr(self.relay, active.INSTALL_MARKER, False))
                self.assertIs(self.relay.app.router.lifespan_context, before)
                setattr(self.relay, worker.ENABLED_MARKER, False)
                setattr(self.relay, active.ENABLED_MARKER, marker)
                env = enabled_env(**{active.ENV_GATE: "false" if marker else "true"})
                with self.assertRaises(worker.MemoryIndexRefreshWorkerError) as raised:
                    worker.install(self.relay, environ=env)
                self.assertEqual(raised.exception.category, "memory_index_refresh_conflicts_runtime")
                setattr(self.relay, active.ENABLED_MARKER, False)

    async def test_coexisting_lifespans_keep_dispatch_unchanged_and_cancel_worker(self):
        dispatch, original_prepare = self.prepare_context()
        writer = self.runner(RecordingEmbedding())
        reader = self.readonly(writer)
        env = enabled_env(**{shadow.ENV_GATE: "true"})
        started = asyncio.Event()
        cancelled = asyncio.Event()
        reported = asyncio.Event()
        tasks = []

        async def blocked_worker(*args):
            tasks.append(asyncio.current_task())
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            worker, "_worker", side_effect=blocked_worker
        ), mock.patch.object(shadow, "_log_report", side_effect=lambda report: reported.set()):
            self.assertTrue(worker.install(self.relay, runner=writer))
            self.assertTrue(shadow.install(self.relay, runner=reader))
            combined = self.relay.app.router.lifespan_context
            self.assertTrue(worker.install(self.relay, runner=writer))
            self.assertTrue(shadow.install(self.relay, runner=reader))
            self.assertIs(self.relay.app.router.lifespan_context, combined)
            async with combined(self.relay.app):
                await asyncio.wait_for(started.wait(), timeout=5)
                with self.forbid_query_writes():
                    observed = await asyncio.to_thread(
                        self.relay.memory_context_integration.prepare_transient_memory_dispatch,
                        object(), dispatch.provider_messages, enabled=True,
                    )
                    self.assertIs(observed, dispatch)
                    await asyncio.wait_for(reported.wait(), timeout=5)
                self.assertEqual(shadow.status_payload_v1(self.relay)["outcomes"]["failed"], 1)
                original_prepare.assert_called_once()
                self.assertEqual(reader.config.embedding_adapter.calls, [])
                self.assertTrue(getattr(self.relay, shadow.READONLY_MARKER))
        self.assertEqual(len(tasks), 1)
        self.assertTrue(tasks[0].cancelled())
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(getattr(self.relay, worker.TASK_MARKER))
        self.assertIsNone(getattr(self.relay, shadow.TASK_MARKER))
        self.assertIsNone(getattr(self.relay, shadow.LOOP_MARKER))

    def test_p3_order_and_defaults_do_not_activate_any_gate(self):
        root = Path(__file__).resolve().parents[2]
        source = (root / "backend" / "p3_relay_app.py").read_text(encoding="utf-8")
        order = (
            "memory_index_refresh_worker.install(relay_app)",
            "memory_retrieval_hybrid_runtime_active.install(relay_app)",
            ".compose_hybrid_retrieval_shadow_runner_v1(relay_app)",
            "memory_retrieval_hybrid_runtime_shadow.install(",
        )
        positions = tuple(source.index(call) for call in order)
        self.assertEqual(positions, tuple(sorted(positions)))
        example_lines = (root / "backend" / ".env.example").read_text(encoding="utf-8").splitlines()
        example = dict(
            line.split("=", 1) for line in example_lines
            if line and not line.startswith("#") and "=" in line
        )
        blueprint = json.loads((root / "render.yaml").read_text(encoding="utf-8"))
        env = {entry["key"]: entry for entry in blueprint["services"][0]["envVars"]}
        for module in (worker, shadow, active):
            self.assertFalse(module.enabled_from_environment({}))
            self.assertEqual(example.get(module.ENV_GATE, "false"), "false")
            self.assertEqual(env[module.ENV_GATE]["value"], "false")

    def test_readonly_repr_never_contains_private_configuration(self):
        reader = self.readonly(self.runner(RecordingEmbedding()))
        for private in (FINGERPRINT_SECRET, TERM_SECRET, EMBEDDING_KEY,
                        str(self.db_path), PRIVATE_CONTENT, MEMORY_KEY):
            self.assertNotIn(private, repr(reader))


if __name__ == "__main__":
    unittest.main()
