from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from fastapi import FastAPI

from backend import memory_index_outbox_consumer as outbox
from backend import memory_index_refresh_worker as worker
from backend import memory_policy
from backend import memory_retrieval_hybrid_runtime_composition as composition
from backend.tests._support import load_app


FINGERPRINT_SECRET = "C5-Fingerprint-Secret-0123456789-AbCd!"
FINGERPRINT_KEY_ID = "c5-memory-fingerprint-v1"
TERM_SECRET = "C5-Term-Secret-0123456789-XyZ-AbCd!"
EMBEDDING_KEY = "C5-Embedding-Key-0123456789-AbCdEfGh!"
STAMP = "2026-09-06T11:00:00+00:00"
MEMORY_KEY = "c5" + "m" * 30
PRIVATE_CONTENT = "This private test project uses PostgreSQL 16."


def enabled_env(**overrides):
    env = {
        worker.ENV_GATE: "true",
        "MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED": "false",
        "MEMORY_HYBRID_RETRIEVAL_ACTIVE_ENABLED": "false",
        composition.TERM_KEY_ID_ENV: "c5-hybrid-term-v1",
        composition.TERM_SECRET_ENV: TERM_SECRET,
        composition.EMBEDDING_API_BASE_ENV: "https://embedding.example/v1",
        composition.EMBEDDING_API_KEY_ENV: EMBEDDING_KEY,
        composition.EMBEDDING_MODEL_ENV: "c5-embedding-test-v1",
        composition.EMBEDDING_DIMENSIONS_ENV: "8",
    }
    env.update(overrides)
    return env


class RecordingEmbedding:
    def __init__(self, before_return=None):
        self.calls = 0
        self.before_return = before_return

    async def __call__(self, texts, model, dimensions):
        self.calls += 1
        if self.before_return is not None:
            produced = self.before_return()
            if asyncio.iscoroutine(produced):
                await produced
        unit = tuple([1.0] + [0.0] * (dimensions - 1))
        return tuple(unit for _text in texts)


class FailingEmbedding:
    def __init__(self):
        self.calls = 0

    async def __call__(self, *_args):
        self.calls += 1
        raise RuntimeError("private provider response body")


class BlockingEmbedding:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, texts, _model, dimensions):
        self.started.set()
        await self.release.wait()
        unit = tuple([1.0] + [0.0] * (dimensions - 1))
        return tuple(unit for _text in texts)


class MemoryIndexRefreshIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.module = load_app(
            self.temp.name,
            telegram=False,
            memory=True,
            memory_writes=True,
            memory_secret=FINGERPRINT_SECRET,
        )
        self.db_path = Path(self.module.DB_PATH).resolve()
        self._seed_profile()
        memory = types.SimpleNamespace(
            enabled=True,
            configuration_valid=True,
            fingerprint_key_id=FINGERPRINT_KEY_ID,
            fingerprint_hmac_secret=FINGERPRINT_SECRET,
            max_item_chars=1000,
            sensitive_storage_enabled=False,
        )
        self.relay = types.SimpleNamespace(
            app=FastAPI(),
            DEPLOYMENT=types.SimpleNamespace(
                db_path=self.db_path,
                persistent_root=self.root,
                memory=memory,
            ),
        )

    def _seed_profile(self):
        with self.module.db() as conn:
            if int(conn.execute(
                "SELECT count(*) FROM memory_fingerprint_profile"
            ).fetchone()[0]) == 0:
                conn.execute(
                    """INSERT INTO memory_fingerprint_profile
                       (singleton,key_id,key_check,normalization_version,
                        fingerprint_version,created_at,updated_at)
                       VALUES(1,?,?,?,?,?,?)""",
                    (
                        FINGERPRINT_KEY_ID,
                        memory_policy.fingerprint_profile_check(
                            FINGERPRINT_SECRET
                        ),
                        memory_policy.NORMALIZATION_VERSION,
                        memory_policy.FINGERPRINT_VERSION,
                        STAMP,
                        STAMP,
                    ),
                )

    def seed_atomic(self, content: str = PRIVATE_CONTENT):
        policy = memory_policy.MemoryPolicy(
            max_item_chars=1000,
            sensitive_storage_enabled=False,
        )
        normalized = policy.validate_content(content, "normal")
        fingerprint = memory_policy.fingerprint_content(
            FINGERPRINT_SECRET,
            scope_type="global_user",
            scope_ref="",
            kind="project",
            normalized_content=normalized,
        )
        with self.module.db() as conn:
            conn.execute(
                """INSERT INTO memory_items
                   (memory_key,kind,scope_type,scope_ref,normalized_content,
                    normalized_fingerprint,fingerprint_version,status,
                    explicitness,confidence,sensitivity,first_observed_at,
                    last_confirmed_at,superseded_by_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)""",
                (
                    MEMORY_KEY,
                    "project",
                    "global_user",
                    "",
                    normalized,
                    fingerprint,
                    memory_policy.FINGERPRINT_VERSION,
                    "active",
                    "explicit",
                    1.0,
                    "normal",
                    STAMP,
                    STAMP,
                    STAMP,
                    STAMP,
                ),
            )

    def enqueue_dirty(self, stamp: str = STAMP) -> int:
        with self.module.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            event_id = self.module.channel_store.enqueue_memory_index_dirty(
                conn,
                created_at=stamp,
            )
            conn.execute("COMMIT")
        return event_id

    def rows(self):
        with self.module.db() as conn:
            return tuple(
                tuple(row)
                for row in conn.execute(
                    """SELECT id,completed_at FROM memory_index_outbox
                       ORDER BY id"""
                )
            )

    def runner(self, embedding, **env_overrides):
        config = composition.load_hybrid_index_config_v1(
            self.relay,
            enabled_env(**env_overrides),
        )
        config = dataclasses.replace(config, embedding_adapter=embedding)
        return composition.HybridRetrievalShadowRunnerV1(
            config=config,
            reader=composition._reader(config),
        )

    async def test_rebuild_acknowledges_then_current_pair_skips_embedding(self):
        self.seed_atomic()
        first_id = self.enqueue_dirty()
        embedding = RecordingEmbedding()
        runner = self.runner(embedding)

        first = await worker.drain_once_v1(self.db_path, runner)
        self.assertTrue(first.rebuilt)
        self.assertEqual(first.pending_count, 1)
        self.assertEqual(first.completed_count, 1)
        self.assertEqual(first.provider_call_count, 1)
        self.assertEqual(embedding.calls, 1)
        first_generations = (
            composition.bm25_store.load_bm25_store_snapshot(
                runner.config.bm25_path
            ).generation,
            composition.vector_store.load_vector_store_snapshot(
                runner.config.vector_path
            ).generation,
        )

        second_id = self.enqueue_dirty("2026-09-06T11:01:00+00:00")
        second = await worker.drain_once_v1(self.db_path, runner)
        self.assertFalse(second.rebuilt)
        self.assertEqual(second.provider_call_count, 0)
        self.assertEqual(second.completed_count, 1)
        self.assertEqual(embedding.calls, 1)
        rows = self.rows()
        self.assertEqual(tuple(event_id for event_id, _completed in rows), (first_id, second_id))
        self.assertTrue(all(completed is not None for _id, completed in rows))
        self.assertEqual(
            first_generations,
            (
                composition.bm25_store.load_bm25_store_snapshot(
                    runner.config.bm25_path
                ).generation,
                composition.vector_store.load_vector_store_snapshot(
                    runner.config.vector_path
                ).generation,
            ),
        )

    async def test_event_arriving_during_rebuild_stays_pending(self):
        self.seed_atomic()
        first = self.enqueue_dirty()
        inserted = []

        def insert_later():
            inserted.append(
                self.enqueue_dirty("2026-09-06T11:00:30+00:00")
            )

        runner = self.runner(RecordingEmbedding(insert_later))
        receipt = await worker.drain_once_v1(self.db_path, runner)
        self.assertTrue(receipt.rebuilt)
        rows = self.rows()
        self.assertIsNotNone(rows[0][1])
        self.assertEqual(rows[0][0], first)
        self.assertEqual(rows[1], (inserted[0], None))

    async def test_provider_failure_leaves_event_pending_and_no_sidecar_write(self):
        self.seed_atomic()
        self.enqueue_dirty()
        embedding = FailingEmbedding()
        runner = self.runner(embedding)
        with self.assertRaises(worker.MemoryIndexRefreshWorkerError) as raised:
            await worker.drain_once_v1(self.db_path, runner)
        self.assertEqual(
            raised.exception.category,
            "memory_index_refresh_reconcile_failed",
        )
        self.assertEqual(embedding.calls, 1)
        self.assertEqual(self.rows()[0][1], None)
        self.assertFalse(runner.config.bm25_path.exists())
        self.assertFalse(runner.config.vector_path.exists())
        for private in (PRIVATE_CONTENT, MEMORY_KEY, "private provider response body"):
            self.assertNotIn(private, str(raised.exception))
            self.assertNotIn(private, repr(raised.exception))

    async def test_completion_failure_retries_without_reembedding(self):
        self.seed_atomic()
        self.enqueue_dirty()
        embedding = RecordingEmbedding()
        runner = self.runner(embedding)
        failure = outbox.MemoryIndexOutboxConsumerError(
            "memory_index_outbox_completion_failed"
        )
        with mock.patch.object(
            worker.outbox,
            "complete_pending_batch_v1",
            side_effect=failure,
        ):
            with self.assertRaises(worker.MemoryIndexRefreshWorkerError) as raised:
                await worker.drain_once_v1(self.db_path, runner)
        self.assertEqual(
            raised.exception.category,
            "memory_index_refresh_completion_failed",
        )
        self.assertEqual(embedding.calls, 1)
        self.assertEqual(self.rows()[0][1], None)

        restarted_runner = self.runner(embedding)
        receipt = await worker.drain_once_v1(self.db_path, restarted_runner)
        self.assertFalse(receipt.rebuilt)
        self.assertEqual(receipt.provider_call_count, 0)
        self.assertEqual(receipt.completed_count, 1)
        self.assertEqual(embedding.calls, 1)
        self.assertIsNotNone(self.rows()[0][1])

    async def test_startup_reconcile_without_events_and_idle_pass(self):
        self.seed_atomic()
        embedding = RecordingEmbedding()
        runner = self.runner(embedding)
        startup = await worker.drain_once_v1(
            self.db_path,
            runner,
            reconcile_without_pending=True,
        )
        self.assertTrue(startup.reconciled)
        self.assertTrue(startup.rebuilt)
        self.assertEqual(startup.pending_count, 0)
        self.assertEqual(startup.completed_count, 0)
        self.assertEqual(embedding.calls, 1)

        idle = await worker.drain_once_v1(self.db_path, runner)
        self.assertFalse(idle.reconciled)
        self.assertFalse(idle.rebuilt)
        self.assertEqual(embedding.calls, 1)
        restarted = await worker.drain_once_v1(
            self.db_path,
            self.runner(embedding),
            reconcile_without_pending=True,
        )
        self.assertTrue(restarted.reconciled)
        self.assertFalse(restarted.rebuilt)
        self.assertEqual(restarted.provider_call_count, 0)
        self.assertEqual(embedding.calls, 1)

    async def test_periodic_reconcile_repairs_corrupt_disposable_sidecar(self):
        self.seed_atomic()
        embedding = RecordingEmbedding()
        runner = self.runner(embedding)
        first = await worker.drain_once_v1(
            self.db_path,
            runner,
            reconcile_without_pending=True,
        )
        self.assertTrue(first.rebuilt)
        self.assertEqual(embedding.calls, 1)
        runner.config.vector_path.write_bytes(b"corrupt disposable index")

        repaired = await worker.drain_once_v1(
            self.db_path,
            runner,
            reconcile_without_pending=True,
        )
        self.assertTrue(repaired.rebuilt)
        self.assertEqual(repaired.provider_call_count, 1)
        self.assertEqual(embedding.calls, 2)
        stored = composition.vector_store.load_vector_store_snapshot(
            runner.config.vector_path
        )
        self.assertEqual(stored.plan.document_count, 1)

    async def test_changed_index_identity_forces_one_complete_pair_rebuild(self):
        self.seed_atomic()
        embedding = RecordingEmbedding()
        first_runner = self.runner(embedding)
        await worker.drain_once_v1(
            self.db_path,
            first_runner,
            reconcile_without_pending=True,
        )
        self.assertEqual(embedding.calls, 1)

        changed_runner = self.runner(
            embedding,
            **{composition.TERM_KEY_ID_ENV: "c5-hybrid-term-v2"},
        )
        changed = await worker.drain_once_v1(
            self.db_path,
            changed_runner,
            reconcile_without_pending=True,
        )
        self.assertTrue(changed.rebuilt)
        self.assertEqual(changed.provider_call_count, 1)
        self.assertEqual(embedding.calls, 2)
        sparse = composition.bm25_store.load_bm25_store_snapshot(
            changed_runner.config.bm25_path
        )
        self.assertEqual(sparse.plan.term_key_id, "c5-hybrid-term-v2")

    async def test_cancellation_during_embedding_never_acknowledges(self):
        self.seed_atomic()
        self.enqueue_dirty()
        embedding = BlockingEmbedding()
        runner = self.runner(embedding)
        task = asyncio.create_task(worker.drain_once_v1(self.db_path, runner))
        await asyncio.wait_for(embedding.started.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.rows()[0][1], None)
        self.assertFalse(runner.config.bm25_path.exists())
        self.assertFalse(runner.config.vector_path.exists())

    async def test_empty_authority_writes_valid_empty_pair_without_provider(self):
        self.enqueue_dirty()
        embedding = RecordingEmbedding()
        runner = self.runner(embedding)
        receipt = await worker.drain_once_v1(self.db_path, runner)
        self.assertTrue(receipt.rebuilt)
        self.assertEqual(receipt.source_atomic_count, 0)
        self.assertEqual(receipt.bm25_document_count, 0)
        self.assertEqual(receipt.vector_document_count, 0)
        self.assertEqual(receipt.provider_call_count, 0)
        self.assertEqual(embedding.calls, 0)
        self.assertIsNotNone(self.rows()[0][1])

    async def test_structural_receipt_and_log_hide_all_private_material(self):
        self.seed_atomic()
        self.enqueue_dirty()
        embedding = RecordingEmbedding()
        runner = self.runner(embedding)
        receipt = await worker.drain_once_v1(self.db_path, runner)
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            worker._log_completed(receipt)
        rendered = repr(receipt) + buffer.getvalue()
        for private in (
            PRIVATE_CONTENT,
            MEMORY_KEY,
            FINGERPRINT_SECRET,
            TERM_SECRET,
            EMBEDDING_KEY,
            str(self.db_path),
            "c5-embedding-test-v1",
        ):
            self.assertNotIn(private, rendered)


class MemoryIndexRefreshInstallTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def relay():
        app = FastAPI()
        return types.SimpleNamespace(
            app=app,
            DEPLOYMENT=types.SimpleNamespace(
                memory=types.SimpleNamespace(
                    enabled=True,
                    configuration_valid=True,
                )
            ),
        )

    def test_gate_off_is_exact_lifecycle_and_config_noop(self):
        relay = self.relay()
        original = relay.app.router.lifespan_context
        env = {
            worker.ENV_GATE: "false",
            composition.TERM_SECRET_ENV: "invalid secret",
            composition.EMBEDDING_API_BASE_ENV: "not a url",
        }
        self.assertFalse(worker.install(relay, environ=env))
        self.assertIs(relay.app.router.lifespan_context, original)
        self.assertFalse(getattr(relay, worker.ENABLED_MARKER))
        self.assertIsNone(getattr(relay, worker.TASK_MARKER, None))

    def test_enabled_worker_rejects_either_per_query_runtime(self):
        for gate in (
            "MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED",
            "MEMORY_HYBRID_RETRIEVAL_ACTIVE_ENABLED",
        ):
            with self.subTest(gate=gate):
                relay = self.relay()
                with self.assertRaises(worker.MemoryIndexRefreshWorkerError) as raised:
                    worker.install(relay, environ=enabled_env(**{gate: "true"}))
                self.assertEqual(
                    raised.exception.category,
                    "memory_index_refresh_conflicts_runtime",
                )

    async def test_lifespan_owns_exactly_one_cancellable_task(self):
        relay = self.relay()
        fake_config = types.SimpleNamespace(
            authority_path=Path("/tmp/c5-worker-test-relay.db")
        )
        fake_runner = composition.HybridRetrievalShadowRunnerV1(
            config=fake_config,
            reader=types.SimpleNamespace(),
        )
        started = asyncio.Event()

        async def blocked(_path, _runner):
            started.set()
            await asyncio.Event().wait()

        with mock.patch.object(worker, "_worker", blocked):
            self.assertTrue(
                worker.install(
                    relay,
                    environ=enabled_env(),
                    runner=fake_runner,
                )
            )
            async with relay.app.router.lifespan_context(relay.app):
                await asyncio.wait_for(started.wait(), timeout=1)
                task = getattr(relay, worker.TASK_MARKER)
                self.assertIsInstance(task, asyncio.Task)
                self.assertFalse(task.done())
            self.assertIsNone(getattr(relay, worker.TASK_MARKER))
            self.assertTrue(task.cancelled())

    async def test_worker_failure_backoff_is_exponential_and_bounded(self):
        delays = []

        async def fail(*_args, **_kwargs):
            raise worker.MemoryIndexRefreshWorkerError(
                "memory_index_refresh_reconcile_failed"
            )

        async def stop_after_seven(delay):
            delays.append(delay)
            if len(delays) == 7:
                raise asyncio.CancelledError()

        with mock.patch.object(worker, "drain_once_v1", fail), \
             mock.patch.object(worker.asyncio, "sleep", stop_after_seven), \
             mock.patch.object(worker, "_log_failed"):
            with self.assertRaises(asyncio.CancelledError):
                await worker._worker(object(), object())
        self.assertEqual(delays, [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0])
        self.assertLessEqual(max(delays), worker.MAX_BACKOFF_SECONDS)

    def test_static_wiring_is_default_off_and_exposes_no_status_route(self):
        root = Path(__file__).resolve().parents[2]
        p3 = (root / "backend" / "p3_relay_app.py").read_text(encoding="utf-8")
        blueprint = json.loads((root / "render.yaml").read_text(encoding="utf-8"))
        env = {
            item["key"]: item
            for item in blueprint["services"][0]["envVars"]
        }
        self.assertIn("memory_index_refresh_worker.install(relay_app)", p3)
        self.assertNotIn("memory-index-refresh/status", p3)
        self.assertEqual(env[worker.ENV_GATE].get("value"), "false")


if __name__ == "__main__":
    unittest.main()
