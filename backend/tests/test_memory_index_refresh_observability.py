from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import runpy
import sqlite3
import sys
import types
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from fastapi import FastAPI, HTTPException

import backend
from backend import (
    memory_index_outbox_consumer as outbox,
    memory_index_refresh_observability as observe,
    memory_index_refresh_worker as worker,
    memory_retrieval_hybrid_runtime_active as active,
    memory_retrieval_hybrid_runtime_composition as composition,
    memory_retrieval_hybrid_runtime_shadow as shadow,
)
from backend.tests._support import NoNetworkMixin, request
from backend.tests.test_memory_index_refresh_worker import (
    MemoryIndexRefreshFixture,
    RecordingEmbedding,
    enabled_env,
)


PRIVATE = "PRIVATE-content-path-model-key-provider-body"
STATUS_PATH = "/app/memory/index-refresh/status"
RECEIPT = {
    "batch_pending_count": 3,
    "batch_completed_count": 3,
    "rebuilt": True,
    "source_atomic_count": 2,
    "bm25_document_count": 2,
    "vector_document_count": 1,
    "provider_call_count": 1,
}


class Poison:
    def __getattr__(self, _name):
        raise AssertionError(PRIVATE)

    def __repr__(self):
        raise AssertionError(PRIVATE)

    def __str__(self):
        raise AssertionError(PRIVATE)

    def __bool__(self):
        raise AssertionError(PRIVATE)


def relay_with_markers(*, worker_on=False, shadow_on=False):
    relay = types.SimpleNamespace(app=FastAPI())
    for module, enabled in ((worker, worker_on), (shadow, shadow_on), (active, False)):
        setattr(relay, module.INSTALL_MARKER, True)
        setattr(relay, module.ENABLED_MARKER, enabled)
    if shadow_on:
        setattr(relay, shadow.READONLY_MARKER, worker_on)
    if worker_on:
        setattr(relay, worker.OBSERVABILITY_MARKER, observe.IndexRefreshObservabilityV1())
    return relay


def completed_receipt():
    return worker.MemoryIndexDrainReceiptV1(
        contract_version=worker.WORKER_CONTRACT_VERSION,
        pending_count=RECEIPT["batch_pending_count"],
        completed_count=RECEIPT["batch_completed_count"],
        reconciled=True,
        **{name: value for name, value in RECEIPT.items() if not name.startswith("batch_")},
    )


def payload(snapshot, **overrides):
    values = dict(
        enabled=True, installed=True, shadow_enabled=True, active_enabled=False,
        mode="worker_readonly_shadow", task_state="running", observability_available=True,
    )
    values.update(overrides)
    return observe.project_status_payload_v1(snapshot, **values)


class TrackerTests(unittest.TestCase):
    def test_empty_snapshot_matches_new_tracker_without_creating_runtime_state(self):
        tracker = observe.IndexRefreshObservabilityV1()
        self.assertEqual(tracker.snapshot(), observe.empty_snapshot_v1())
        result = payload(tracker.snapshot())
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(result["outcomes"], dict(idle=0, completed=0, failed=0, cancelled=0))
        self.assertIsNone(result["last_completed_receipt"])

    def test_completed_receipt_is_historical_across_idle_failure_and_recovery(self):
        tracker = observe.IndexRefreshObservabilityV1()
        tracker.record_attempt()
        tracker.record_completed(**RECEIPT)
        tracker.record_attempt()
        tracker.record_idle()
        tracker.record_attempt()
        tracker.record_failed("memory_index_refresh_completion_failed", 2.0)
        result = payload(tracker.snapshot())
        self.assertEqual(result["last_completed_receipt"], RECEIPT)
        self.assertEqual(result["outcomes"], dict(idle=1, completed=1, failed=1, cancelled=0))
        self.assertEqual(result["last"]["failure_category"], "memory_index_refresh_completion_failed")
        self.assertEqual(result["consecutive_failures"], 1)
        self.assertEqual(result["backoff_seconds"], 2)
        tracker.record_attempt()
        self.assertEqual(tracker.snapshot().backoff_seconds, 0)
        tracker.record_completed(**dict(RECEIPT, rebuilt=False, provider_call_count=0))
        result = payload(tracker.snapshot())
        self.assertEqual(result["attempts"], 4)
        self.assertEqual(result["outcomes"]["completed"], 2)
        self.assertEqual(result["last"]["failure_category"], "")
        self.assertEqual(result["consecutive_failures"], 0)
        self.assertEqual(result["last_completed_receipt"]["provider_call_count"], 0)
        self.assertNotIn("backlog", json.dumps(result))
        self.assertNotIn("healthy", result)

    def test_cancel_counts_only_inflight_attempt_once_and_retains_receipt(self):
        tracker = observe.IndexRefreshObservabilityV1()
        tracker.record_attempt()
        tracker.record_completed(**RECEIPT)
        tracker.record_cancelled()  # polling cancellation
        self.assertEqual(tracker.snapshot().cancelled_count, 0)
        tracker.record_attempt()
        tracker.record_cancelled()
        tracker.record_cancelled()  # lifespan cleanup is idempotent
        result = payload(tracker.snapshot())
        self.assertEqual(result["outcomes"]["cancelled"], 1)
        self.assertFalse(result["in_flight"])
        self.assertEqual(result["last_completed_receipt"], RECEIPT)
        tracker.record_attempt()
        tracker.record_failed("memory_index_refresh_reconcile_failed", 60)
        tracker.record_cancelled()  # backoff cancellation
        self.assertEqual(tracker.snapshot().cancelled_count, 1)
        self.assertEqual(tracker.snapshot().backoff_seconds, 0)
        self.assertEqual(tracker.snapshot().last_status, "failed")

    def test_counts_and_receipts_saturate_without_unbounded_history(self):
        tracker = observe.IndexRefreshObservabilityV1()
        tracker._counts = dict.fromkeys(tracker._counts, observe.MAX_COUNTER)
        tracker._consecutive_failures = observe.MAX_COUNTER
        tracker.record_attempt()
        tracker.record_failed("memory_index_refresh_reconcile_failed", 60)
        tracker.record_attempt()
        tracker.record_completed(**dict(
            RECEIPT, batch_pending_count=10**20, batch_completed_count=10**20,
            source_atomic_count=10**20, provider_call_count=10**20,
        ))
        result = payload(tracker.snapshot())
        self.assertEqual(result["attempts"], observe.MAX_COUNTER)
        self.assertEqual(result["outcomes"]["failed"], observe.MAX_COUNTER)
        self.assertEqual(result["outcomes"]["completed"], observe.MAX_COUNTER)
        self.assertEqual(result["last_completed_receipt"]["batch_pending_count"], observe.MAX_COUNTER)
        self.assertEqual(result["last_completed_receipt"]["provider_call_count"], observe.MAX_COUNTER)

    def test_invalid_receipt_inputs_do_not_mutate_tracker_or_leak_values(self):
        tracker = observe.IndexRefreshObservabilityV1()
        tracker.record_attempt()
        before = tracker.snapshot()
        invalid = (
            {"batch_pending_count": True}, {"batch_completed_count": 4},
            {"source_atomic_count": -1}, {"vector_document_count": 1.5},
            {"provider_call_count": Poison()}, {"rebuilt": "true"},
            {"batch_pending_count": 10**20, "batch_completed_count": 10**20 + 1},
        )
        for change in invalid:
            with self.subTest(fields=tuple(change)):
                with self.assertRaisesRegex(ValueError, "^invalid_memory_index_refresh_observability$"):
                    tracker.record_completed(**dict(RECEIPT, **change))
                self.assertEqual(tracker.snapshot(), before)

    def test_categories_are_fixed_and_backoff_rejects_nonfinite_or_non_numeric(self):
        tracker = observe.IndexRefreshObservabilityV1()
        for category in (PRIVATE, Poison(), {}, None):
            tracker.record_attempt()
            tracker.record_failed(category, 2)
            result = payload(tracker.snapshot())
            self.assertEqual(result["last"]["failure_category"], "memory_index_refresh_worker_error")
            self.assertNotIn(PRIVATE, json.dumps(result) + repr(tracker) + repr(tracker.snapshot()))
        for invalid in (True, -1, 61, float("nan"), float("inf"), "2", Poison()):
            with self.assertRaises(ValueError):
                tracker.record_failed("memory_index_refresh_worker_error", invalid)

    def test_corrupt_snapshot_and_public_payload_types_fail_closed(self):
        tracker = observe.IndexRefreshObservabilityV1()
        tracker.record_attempt()
        tracker.record_completed(**RECEIPT)
        snapshot = tracker.snapshot()
        for change in (
            {"attempt_count": True}, {"last_status": PRIVATE},
            {"last_failure_category": PRIVATE}, {"in_flight": True},
            {"backoff_seconds": 61}, {"last_completed_receipt": Poison()},
        ):
            with self.assertRaises(ValueError):
                dataclasses.replace(snapshot, **change)
        for change in (
            {"enabled": 1}, {"mode": PRIVATE}, {"task_state": Poison()},
            {"mode": "worker_only"}, {"active_enabled": True},
            {"mode": "unavailable"}, {"task_state": "disabled"},
        ):
            with self.assertRaises(ValueError):
                payload(snapshot, **change)
        object.__setattr__(snapshot.last_completed_receipt, "rebuilt", PRIVATE)
        with self.assertRaises(ValueError):
            payload(snapshot)
        self.assertNotIn(PRIVATE, repr(snapshot.last_completed_receipt))

    def test_invalidation_is_sticky_and_does_not_block_further_observation(self):
        tracker = observe.IndexRefreshObservabilityV1()
        tracker.invalidate()
        tracker.record_attempt()
        tracker.record_completed(**RECEIPT)
        result = payload(tracker.snapshot())
        self.assertFalse(result["observability_available"])
        self.assertEqual(result["outcomes"]["completed"], 1)


class RuntimeStatusTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    def test_four_modes_use_only_installed_markers_not_live_environment(self):
        modes = {
            (False, False): "disabled", (True, False): "worker_only",
            (True, True): "worker_readonly_shadow", (False, True): "legacy_query_repair",
        }
        for (worker_on, shadow_on), mode in modes.items():
            relay = relay_with_markers(worker_on=worker_on, shadow_on=shadow_on)
            before = vars(relay).copy()
            with mock.patch.object(worker, "os", types.SimpleNamespace(environ=Poison())):
                result = worker.status_payload_v1(relay)
            self.assertEqual(result["mode"], mode)
            self.assertTrue(result["observability_available"])
            self.assertEqual(result["task_state"], "not_running" if worker_on else "disabled")
            self.assertEqual(vars(relay), before)

    def test_missing_nonboolean_and_conflicting_markers_are_not_reported_disabled(self):
        cases = (
            {}, {worker.ENABLED_MARKER: "false"}, {worker.INSTALL_MARKER: 1},
            {shadow.ENABLED_MARKER: Poison()}, {active.ENABLED_MARKER: True},
            {shadow.READONLY_MARKER: True}, {active.INSTALL_MARKER: False},
        )
        for overrides in cases:
            relay = relay_with_markers() if overrides else types.SimpleNamespace()
            vars(relay).update(overrides)
            result = worker.status_payload_v1(relay)
            self.assertFalse(result["observability_available"])
            self.assertEqual(result["mode"], "unavailable")
            self.assertNotIn(PRIVATE, json.dumps(result))
        for worker_on, readonly in ((True, False), (False, True)):
            relay = relay_with_markers(worker_on=worker_on, shadow_on=True)
            setattr(relay, shadow.READONLY_MARKER, readonly)
            self.assertEqual(worker.status_payload_v1(relay)["mode"], "unavailable")
        self.assertFalse(worker.status_payload_v1(Poison())["observability_available"])

    async def test_task_liveness_is_not_inferred_from_old_successful_receipt(self):
        relay = relay_with_markers(worker_on=True)
        tracker = getattr(relay, worker.OBSERVABILITY_MARKER)
        tracker.record_attempt()
        tracker.record_completed(**RECEIPT)
        release = asyncio.Event()
        task = asyncio.create_task(release.wait())
        setattr(relay, worker.TASK_MARKER, task)
        try:
            result = worker.status_payload_v1(relay)
            self.assertEqual(result["task_state"], "running")
            self.assertTrue(result["task_running"])
            release.set()
            await task
            result = worker.status_payload_v1(relay)
            self.assertEqual(result["task_state"], "done")
            self.assertFalse(result["task_running"])
            self.assertEqual(result["last_completed_receipt"], RECEIPT)
        finally:
            task.cancel()
            await task
        cancelled = asyncio.create_task(asyncio.Event().wait())
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        setattr(relay, worker.TASK_MARKER, cancelled)
        self.assertEqual(worker.status_payload_v1(relay)["task_state"], "cancelled")
        setattr(relay, worker.ENABLED_MARKER, False)
        self.assertEqual(worker.status_payload_v1(relay)["mode"], "unavailable")

    def test_missing_corrupt_or_forged_tracker_is_explicitly_unavailable(self):
        relay = relay_with_markers(worker_on=True)
        for value in (None, Poison(), types.SimpleNamespace(snapshot=lambda: observe.empty_snapshot_v1())):
            setattr(relay, worker.OBSERVABILITY_MARKER, value)
            self.assertFalse(worker.status_payload_v1(relay)["observability_available"])
        tracker = observe.IndexRefreshObservabilityV1()
        tracker._last_status = PRIVATE
        setattr(relay, worker.OBSERVABILITY_MARKER, tracker)
        result = worker.status_payload_v1(relay)
        self.assertFalse(result["observability_available"])
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertEqual(tracker._last_status, PRIVATE)  # status does not repair telemetry

    def test_unknown_task_never_invokes_arbitrary_liveness_methods(self):
        relay = relay_with_markers(worker_on=True)
        task = types.SimpleNamespace(done=mock.Mock(side_effect=AssertionError(PRIVATE)))
        setattr(relay, worker.TASK_MARKER, task)
        result = worker.status_payload_v1(relay)
        self.assertEqual(result["task_state"], "unavailable")
        self.assertFalse(result["observability_available"])
        task.done.assert_not_called()

    def test_inflight_without_live_task_is_not_available(self):
        relay = relay_with_markers(worker_on=True)
        getattr(relay, worker.OBSERVABILITY_MARKER).record_attempt()
        result = worker.status_payload_v1(relay)
        self.assertTrue(result["in_flight"])
        self.assertFalse(result["observability_available"])
        self.assertEqual(result["task_state"], "not_running")


class WorkerObservationTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def run_until_poll(self, tracker, *, receipt=None, error=None):
        drain = mock.AsyncMock(side_effect=error, return_value=receipt)
        with mock.patch.object(worker, "drain_once_v1", drain), \
             mock.patch.object(worker.asyncio, "sleep", side_effect=asyncio.CancelledError), \
             mock.patch.object(worker, "_log_line"):
            with self.assertRaises(asyncio.CancelledError):
                await worker._worker(Poison(), Poison(), tracker)
        drain.assert_awaited_once()
        return tracker.snapshot()

    async def test_successful_receipt_and_idle_have_separate_outcomes(self):
        for receipt, status in ((completed_receipt(), "completed"), (worker._idle_receipt(), "idle")):
            tracker = observe.IndexRefreshObservabilityV1()
            snapshot = await self.run_until_poll(tracker, receipt=receipt)
            self.assertEqual(snapshot.attempt_count, 1)
            self.assertEqual(snapshot.last_status, status)
            self.assertEqual(snapshot.cancelled_count, 0)
            self.assertEqual(snapshot.failed_count, 0)
            self.assertEqual(snapshot.last_completed_receipt is None, status == "idle")

    async def test_worker_errors_and_unexpected_exceptions_are_sanitized(self):
        for error, category in (
            (worker.MemoryIndexRefreshWorkerError("memory_index_refresh_completion_failed"),
             "memory_index_refresh_completion_failed"),
            (RuntimeError(PRIVATE), "memory_index_refresh_worker_error"),
        ):
            tracker = observe.IndexRefreshObservabilityV1()
            snapshot = await self.run_until_poll(tracker, error=error)
            self.assertEqual(snapshot.failed_count, 1)
            self.assertEqual(snapshot.completed_count, 0)
            self.assertEqual(snapshot.last_failure_category, category)
            self.assertEqual(snapshot.backoff_seconds, 2)
            self.assertIsNone(snapshot.last_completed_receipt)
            self.assertNotIn(PRIVATE, json.dumps(payload(snapshot)))

    async def test_telemetry_failure_never_turns_a_completed_drain_into_worker_failure(self):
        tracker = observe.IndexRefreshObservabilityV1()
        with mock.patch.object(
            observe.IndexRefreshObservabilityV1, "record_completed", side_effect=SystemExit(PRIVATE)
        ):
            snapshot = await self.run_until_poll(tracker, receipt=completed_receipt())
        self.assertFalse(snapshot.available)
        self.assertEqual(snapshot.failed_count, 0)

    async def test_cancellation_during_drain_is_one_cancelled_attempt(self):
        tracker = observe.IndexRefreshObservabilityV1()
        snapshot = await self.run_until_poll(tracker, error=asyncio.CancelledError())
        self.assertEqual(snapshot.attempt_count, 1)
        self.assertEqual(snapshot.cancelled_count, 1)
        self.assertFalse(snapshot.in_flight)
        self.assertIsNone(snapshot.last_completed_receipt)

    def test_wrong_receipt_contract_invalidates_without_accepting_private_data(self):
        tracker = observe.IndexRefreshObservabilityV1()
        worker._observe_receipt(tracker, dataclasses.replace(completed_receipt(), contract_version=PRIVATE))
        self.assertFalse(tracker.snapshot().available)
        self.assertIsNone(tracker.snapshot().last_completed_receipt)


@contextmanager
def installed_p3_routes(relay):
    # Load real P3 routing without importing the real bridge or opening any DB.
    fake_bridge = types.ModuleType("backend.legacy_chat_bridge_app")
    fake_bridge.relay_app, fake_bridge.app = relay, relay.app
    with ExitStack() as stack:
        stack.enter_context(mock.patch.dict(sys.modules, {fake_bridge.__name__: fake_bridge}))
        stack.enter_context(mock.patch.object(backend, "legacy_chat_bridge_app", fake_bridge, create=True))
        for name in (
            "memory_formation_v2_authority", "memory_formation_v2_runtime_patch",
            "memory_hierarchy_summary_runtime_shadow", "memory_hierarchy_live_refresh_shadow",
            "memory_retrieval_hybrid_provider_wire",
        ):
            stack.enter_context(mock.patch(f"backend.{name}.install", return_value=True))
        stack.enter_context(mock.patch.dict(os.environ, {
            worker.ENV_GATE: "false", shadow.ENV_GATE: "false", active.ENV_GATE: "false",
        }))
        namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / "p3_relay_app.py"))
        yield namespace


class StatusRouteTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    def relay(self):
        relay = relay_with_markers()

        def check_auth(incoming):
            if incoming.headers.get("Authorization") != "Bearer test-status-secret":
                raise HTTPException(status_code=401, detail="Unauthorized")

        relay.check_auth = check_auth

        @relay.app.get("/readyz")
        async def ready():
            return {"ready": True}

        return relay

    async def test_auth_precedes_status_read_route_is_get_only_and_idempotent(self):
        relay = self.relay()
        with installed_p3_routes(relay) as namespace:
            namespace["_install_index_refresh_status_route"]()
            routes = [route for route in relay.app.routes if route.path == STATUS_PATH]
            self.assertEqual(len(routes), 1)
            with mock.patch.object(worker, "status_payload_v1", wraps=worker.status_payload_v1) as status:
                response = await request(relay, "GET", STATUS_PATH)
                self.assertEqual(response.status_code, 401)
                status.assert_not_called()
                response = await request(relay, "GET", STATUS_PATH, headers={
                    "Authorization": "Bearer test-status-secret",
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["mode"], "disabled")
                status.assert_called_once_with(relay)
                response = await request(relay, "POST", STATUS_PATH, headers={
                    "Authorization": "Bearer test-status-secret",
                })
                self.assertEqual(response.status_code, 405)
                status.assert_called_once()

    async def test_status_never_reads_db_config_provider_or_changes_readyz_and_shadow(self):
        relay = self.relay()
        with installed_p3_routes(relay):
            setattr(relay, worker.ENABLED_MARKER, True)
            setattr(relay, worker.TASK_MARKER, asyncio.current_task())
            tracker = observe.IndexRefreshObservabilityV1()
            tracker.record_attempt()
            tracker.record_completed(**RECEIPT)
            setattr(relay, worker.OBSERVABILITY_MARKER, tracker)
            before_shadow = shadow.status_payload_v1(relay)
            before_tracker = tracker.snapshot()
            before_markers = vars(relay).copy()
            before_lifespan = relay.app.router.lifespan_context
            guards = (
                (sqlite3, "connect"), (Path, "read_bytes"), (Path, "read_text"),
                (Path, "open"), (Path, "resolve"),
                (outbox, "peek_pending_batch_v1"), (outbox, "complete_pending_batch_v1"),
                (composition, "load_hybrid_index_config_v1"), (composition, "_reader"),
                (composition.HybridRetrievalShadowRunnerV1, "reconcile_index_pair_v1"),
                (composition, "_commit_pair"), (worker, "enabled_from_environment"),
                (composition.embedding_openai.OpenAICompatibleEmbeddingAdapterV1, "__call__"),
            )
            with ExitStack() as stack:
                mocks = [stack.enter_context(mock.patch.object(
                    module, name, side_effect=AssertionError(PRIVATE)
                )) for module, name in guards]
                for _ in range(2):
                    response = await request(relay, "GET", STATUS_PATH, headers={
                        "Authorization": "Bearer test-status-secret",
                    })
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json()["observability_available"])
                    self.assertEqual(response.json()["mode"], "worker_only")
                    self.assertEqual(response.json()["last_completed_receipt"], RECEIPT)
                    self.assertEqual(set(response.json()), {
                        "contract_version", "enabled", "installed", "shadow_enabled",
                        "active_enabled", "mode", "observability_available", "task_state",
                        "task_running", "in_flight", "attempts", "outcomes",
                        "consecutive_failures", "backoff_seconds", "last", "last_completed_receipt",
                    })
                for guard in mocks:
                    guard.assert_not_called()
            self.assertEqual(shadow.status_payload_v1(relay), before_shadow)
            self.assertEqual(tracker.snapshot(), before_tracker)
            self.assertEqual(vars(relay), before_markers)
            self.assertIs(relay.app.router.lifespan_context, before_lifespan)
            setattr(relay, worker.TASK_MARKER, Poison())
            bad_status = await request(relay, "GET", STATUS_PATH, headers={
                "Authorization": "Bearer test-status-secret",
            })
            self.assertFalse(bad_status.json()["observability_available"])
            ready = await request(relay, "GET", "/readyz")
            self.assertEqual(ready.status_code, 200)
            self.assertEqual(ready.json(), {"ready": True})
            app_text = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
            ready_source = app_text.split('@app.get("/readyz")', 1)[1].split(
                "# ---- Kelivo OpenAI-compatible API", 1
            )[0]
            self.assertNotIn("index_refresh", ready_source)


class WorkerIntegrationTests(
    MemoryIndexRefreshFixture, NoNetworkMixin, unittest.IsolatedAsyncioTestCase
):
    async def test_failed_ack_after_real_synthetic_rebuild_is_not_a_completed_receipt(self):
        self.seed_atomic()
        self.enqueue_dirty()
        writer = self.runner(RecordingEmbedding())
        tracker = observe.IndexRefreshObservabilityV1()
        with mock.patch.object(
            outbox, "complete_pending_batch_v1", side_effect=RuntimeError(PRIVATE)
        ), mock.patch.object(worker.asyncio, "sleep", side_effect=asyncio.CancelledError), \
             mock.patch.object(worker, "_log_line"):
            with self.assertRaises(asyncio.CancelledError):
                await worker._worker(self.db_path, writer, tracker)
        self.assertTrue(writer.config.bm25_path.exists())
        self.assertTrue(writer.config.vector_path.exists())
        self.assertIsNone(self.rows()[0][1])
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot.last_failure_category, "memory_index_refresh_completion_failed")
        self.assertEqual(snapshot.completed_count, 0)
        self.assertEqual(snapshot.failed_count, 1)
        self.assertIsNone(snapshot.last_completed_receipt)

    async def test_telemetry_constructor_failure_does_not_prevent_lifespan_worker(self):
        writer = self.runner(RecordingEmbedding())
        started = asyncio.Event()

        async def blocked(_path, _runner, tracker):
            self.assertIsNone(tracker)
            started.set()
            await asyncio.Event().wait()

        with mock.patch.object(
            observe.IndexRefreshObservabilityV1, "__init__", side_effect=RuntimeError(PRIVATE)
        ), mock.patch.object(worker, "_worker", blocked):
            self.assertTrue(worker.install(self.relay, environ=enabled_env(), runner=writer))
            active.install(self.relay, environ={active.ENV_GATE: "false"})
            with mock.patch.dict(os.environ, {shadow.ENV_GATE: "false"}):
                shadow.install(self.relay)
            async with self.relay.app.router.lifespan_context(self.relay.app):
                await asyncio.wait_for(started.wait(), timeout=2)
                result = worker.status_payload_v1(self.relay)
                self.assertEqual(result["mode"], "worker_only")
                self.assertTrue(result["task_running"])
                self.assertFalse(result["observability_available"])
                task = getattr(self.relay, worker.TASK_MARKER)
            self.assertTrue(task.cancelled())

    async def test_fatal_task_exit_is_unavailable_not_a_cancelled_drain(self):
        class FatalWorkerExit(BaseException):
            pass

        started = asyncio.Event()

        async def fatal(_path, _runner, tracker):
            tracker.record_attempt()
            started.set()
            raise FatalWorkerExit(PRIVATE)

        with mock.patch.object(worker, "_worker", fatal):
            worker.install(self.relay, environ=enabled_env(), runner=self.runner(RecordingEmbedding()))
            async with self.relay.app.router.lifespan_context(self.relay.app):
                await asyncio.wait_for(started.wait(), timeout=2)
                task = getattr(self.relay, worker.TASK_MARKER)
                self.assertTrue(task.done())
                self.assertFalse(task.cancelled())
        snapshot = getattr(self.relay, worker.OBSERVABILITY_MARKER).snapshot()
        self.assertFalse(snapshot.available)
        self.assertEqual(snapshot.cancelled_count, 0)
        self.assertEqual(snapshot.completed_count, 0)
        self.assertIsNone(getattr(self.relay, worker.TASK_MARKER))
        self.assertNotIn(PRIVATE, json.dumps(payload(snapshot)))

    async def test_restart_rollback_matrix_changes_writer_ownership_not_live_env(self):
        writer = self.runner(RecordingEmbedding())
        self.relay.DEPLOYMENT.memory.context_injection_enabled = True
        self.relay.DEPLOYMENT.memory.smart_retrieval_enabled = True
        stopped_tasks = []
        observed_modes = []
        for worker_on, shadow_on, expected in (
            (True, True, "worker_readonly_shadow"),
            (True, False, "worker_only"),
            (False, True, "legacy_query_repair"),
            (False, False, "disabled"),
        ):
            relay = types.SimpleNamespace(
                app=FastAPI(), DEPLOYMENT=self.relay.DEPLOYMENT,
                memory_context_integration=types.SimpleNamespace(
                    prepare_transient_memory_dispatch=mock.Mock()
                ),
            )
            original_prepare = relay.memory_context_integration.prepare_transient_memory_dispatch
            started = asyncio.Event()

            async def blocked(*_args):
                started.set()
                await asyncio.Event().wait()

            env = enabled_env(**{
                worker.ENV_GATE: str(worker_on).lower(), shadow.ENV_GATE: str(shadow_on).lower(),
            })
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(worker, "_worker", blocked):
                worker.install(relay, runner=writer)
                active.install(relay)
                reader = composition.compose_hybrid_retrieval_shadow_runner_v1(relay)
                shadow.install(relay, runner=reader)
                if shadow_on:
                    runner_type = (composition.HybridRetrievalReadOnlyRunnerV1 if worker_on
                                   else composition.HybridRetrievalShadowRunnerV1)
                    self.assertIs(type(reader), runner_type)
                else:
                    self.assertIsNone(reader)
                    self.assertIs(relay.memory_context_integration.prepare_transient_memory_dispatch, original_prepare)
                async with relay.app.router.lifespan_context(relay.app):
                    if worker_on:
                        await asyncio.wait_for(started.wait(), timeout=2)
                        stopped_tasks.append(getattr(relay, worker.TASK_MARKER))
                    result = worker.status_payload_v1(relay)
                    self.assertEqual(result["mode"], expected)
                    self.assertEqual(result["task_running"], worker_on)
                    self.assertTrue(result["observability_available"])
                    observed_modes.append(result["mode"])
                    os.environ.update({worker.ENV_GATE: "invalid", shadow.ENV_GATE: "invalid"})
                    self.assertEqual(worker.status_payload_v1(relay), result)
                    self.assertEqual(worker.install(relay), worker_on)  # no live reconfiguration
            self.assertIsNone(getattr(relay, worker.TASK_MARKER, None))
            self.assertTrue(all(task.cancelled() for task in stopped_tasks))
        self.assertEqual(observed_modes, [
            "worker_readonly_shadow", "worker_only", "legacy_query_repair", "disabled",
        ])
        self.assertEqual(len(stopped_tasks), 2)
        self.assertEqual(writer.config.embedding_adapter.calls, 0)
        self.assertFalse(writer.config.bm25_path.exists())
        self.assertFalse(writer.config.vector_path.exists())


if __name__ == "__main__":
    unittest.main()
