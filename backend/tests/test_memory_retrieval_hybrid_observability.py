from __future__ import annotations

import asyncio
import inspect
import os
import types
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import mock

from backend import (
    memory_retrieval_hybrid_fusion as fusion,
    memory_retrieval_hybrid_observability as observability,
    memory_retrieval_hybrid_query as hybrid_query,
    memory_retrieval_hybrid_runtime_shadow as runtime_shadow,
    memory_retrieval_hybrid_shadow as shadow,
)


K1 = "hybrid_observe_atomic_000001"


def completed_report(*, relation: str = "identical"):
    authority = 1 if relation != "both_empty" else 0
    hybrid = authority
    overlap = authority
    return shadow.HybridRetrievalShadowReportV1(
        contract_version=shadow.HYBRID_SHADOW_CONTRACT_VERSION,
        status="completed",
        relation=relation,
        authority_selected_count=authority,
        hybrid_selected_count=hybrid,
        overlap_count=overlap,
        authority_only_count=0,
        hybrid_only_count=0,
        exact_hit_count=authority,
        lexical_hit_count=authority,
        bm25_hit_count=authority,
        vector_hit_count=authority,
        bm25_available=True,
        vector_available=True,
        query_embedding_performed=True,
    )


def hybrid_result():
    hit = fusion.HybridFusionHitV1(
        memory_key=K1,
        exact_rank=1,
        lexical_rank=1,
        bm25_rank=1,
        vector_rank=1,
        exact_match_count=1,
        channel_count=4,
        rank_fusion_score=0.5,
        confidence_boost=0.01,
        recency_boost=0.01,
        touch_boost=0.0,
        final_score=0.52,
    )
    fused = fusion.HybridFusionResultV1(
        contract_version=fusion.HYBRID_FUSION_CONTRACT_VERSION,
        hits=(hit,),
        eligible_atomic_count=1,
        exact_hit_count=1,
        lexical_hit_count=1,
        bm25_hit_count=1,
        vector_hit_count=1,
        touch_hint_count=0,
        bm25_available=True,
        vector_available=True,
    )
    return hybrid_query.HybridQueryResultV1(
        contract_version=hybrid_query.HYBRID_QUERY_CONTRACT_VERSION,
        source_atomic_count=1,
        bm25_generation=1,
        vector_generation=1,
        query_embedding_performed=True,
        fusion_result=fused,
    )


class ObservabilityContractTests(unittest.TestCase):
    def test_tracker_accepts_only_structural_inputs_and_payload_is_identity_free(self):
        tracker = observability.HybridShadowObservabilityV1()
        public_methods = {
            name: inspect.signature(getattr(tracker, name))
            for name in (
                "record_attempt",
                "record_started",
                "record_skipped",
                "record_cancelled",
                "record_report",
                "snapshot",
            )
        }
        joined = " ".join(str(signature) for signature in public_methods.values())
        for forbidden in ("query", "memory_key", "content", "vector", "model", "path", "secret"):
            self.assertNotIn(forbidden, joined)

        tracker.record_attempt()
        tracker.record_started()
        tracker.record_report(completed_report())
        snapshot = tracker.snapshot()
        payload = observability.project_status_payload_v1(
            snapshot,
            enabled=True,
            installed=True,
            in_flight=False,
            observability_available=True,
        )
        self.assertEqual(payload["attempts"], 1)
        self.assertEqual(payload["started"], 1)
        self.assertEqual(payload["outcomes"]["completed"], 1)
        self.assertEqual(payload["relations"]["identical"], 1)
        self.assertEqual(payload["channels"]["bm25_available"], 1)
        self.assertEqual(payload["channels"]["vector_available"], 1)
        self.assertEqual(payload["channels"]["query_embedding_performed"], 1)
        self.assertNotIn(K1, repr(snapshot))
        self.assertNotIn(K1, repr(tracker))
        self.assertNotIn(K1, repr(payload))

    def test_failed_skipped_cancelled_and_saturation_are_bounded(self):
        tracker = observability.HybridShadowObservabilityV1()
        tracker.record_attempt()
        tracker.record_skipped("busy")
        tracker.record_report(shadow.HybridRetrievalShadowReportV1.failed())
        tracker.record_cancelled()
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot.attempt_count, 1)
        self.assertEqual(snapshot.skipped_busy_count, 1)
        self.assertEqual(snapshot.failed_count, 1)
        self.assertEqual(snapshot.cancelled_count, 1)
        self.assertEqual(snapshot.last_status, "cancelled")
        self.assertEqual(observability._inc(observability.MAX_COUNTER), observability.MAX_COUNTER)

    def test_invalid_report_is_counted_as_failure_without_retaining_object_data(self):
        private = "PRIVATE-REPORT-TEXT"
        tracker = observability.HybridShadowObservabilityV1()
        tracker.record_report(types.SimpleNamespace(private=private))
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot.failed_count, 1)
        self.assertEqual(snapshot.last_status, "failed")
        self.assertNotIn(private, repr(snapshot))
        self.assertNotIn(private, repr(tracker))


class RuntimeStatusTests(unittest.IsolatedAsyncioTestCase):
    def _relay(self):
        @asynccontextmanager
        async def lifespan(_application):
            yield

        app = types.SimpleNamespace(
            router=types.SimpleNamespace(lifespan_context=lifespan)
        )
        context_module = types.SimpleNamespace()

        def prepare(_read_service, base_messages, **_kwargs):
            return types.SimpleNamespace(
                authoritative_memory_keys=(K1,),
                provider_messages=base_messages,
                memory_applied=True,
            )

        context_module.prepare_transient_memory_dispatch = prepare
        memory = types.SimpleNamespace(
            enabled=True,
            configuration_valid=True,
            context_injection_enabled=True,
            smart_retrieval_enabled=True,
        )
        return types.SimpleNamespace(
            DEPLOYMENT=types.SimpleNamespace(memory=memory),
            memory_context_integration=context_module,
            app=app,
        )

    async def test_gate_off_status_is_zero_and_observability_is_non_gating(self):
        relay = self._relay()
        original_prepare = relay.memory_context_integration.prepare_transient_memory_dispatch
        original_lifespan = relay.app.router.lifespan_context
        with mock.patch.dict(os.environ, {runtime_shadow.ENV_GATE: "false"}, clear=False):
            self.assertFalse(runtime_shadow.install(relay, runner=None))
        self.assertIs(relay.memory_context_integration.prepare_transient_memory_dispatch, original_prepare)
        self.assertIs(relay.app.router.lifespan_context, original_lifespan)
        payload = runtime_shadow.status_payload_v1(relay)
        self.assertFalse(payload["enabled"])
        self.assertTrue(payload["installed"])
        self.assertTrue(payload["observability_available"])
        self.assertEqual(payload["attempts"], 0)
        self.assertEqual(payload["outcomes"]["completed"], 0)
        self.assertNotIn(runtime_shadow.OBSERVABILITY_MARKER, vars(relay))

    async def test_enabled_hook_records_completed_shadow_without_changing_dispatch(self):
        relay = self._relay()

        async def runner(*, query_text):
            self.assertEqual(query_text, "current query")
            return hybrid_result()

        with mock.patch.dict(os.environ, {runtime_shadow.ENV_GATE: "true"}, clear=False):
            self.assertTrue(runtime_shadow.install(relay, runner=runner))

        async with relay.app.router.lifespan_context(relay.app):
            base = ({"role": "user", "content": "current query"},)
            dispatch = await asyncio.to_thread(
                relay.memory_context_integration.prepare_transient_memory_dispatch,
                object(),
                base,
            )
            self.assertEqual(dispatch.provider_messages, base)
            for _ in range(50):
                payload = runtime_shadow.status_payload_v1(relay)
                if payload["outcomes"]["completed"] == 1:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(payload["attempts"], 1)
            self.assertEqual(payload["started"], 1)
            self.assertEqual(payload["outcomes"]["completed"], 1)
            self.assertEqual(payload["relations"]["identical"], 1)
            self.assertEqual(payload["last"]["authority_selected"], 1)
            self.assertEqual(payload["last"]["hybrid_selected"], 1)
            self.assertNotIn(K1, repr(payload))
            self.assertNotIn("current query", repr(payload))


class StaticStatusRouteTests(unittest.TestCase):
    def test_status_route_is_authenticated_and_readyz_is_not_modified_here(self):
        backend_root = Path(__file__).resolve().parents[1]
        p3 = (backend_root / "p3_relay_app.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/app/memory/hybrid-shadow/status")', p3)
        route_start = p3.index('async def app_memory_hybrid_shadow_status')
        route_end = p3.index('_P3_HYBRID_SHADOW_STATUS_INSTALLED', route_start)
        route = p3[route_start:route_end]
        self.assertIn("relay_app.check_auth(request)", route)
        self.assertIn("status_payload_v1(relay_app)", route)
        app_text = (backend_root / "app.py").read_text(encoding="utf-8")
        readyz_start = app_text.index('@app.get("/readyz")')
        readyz_end = app_text.index('# ---- Kelivo OpenAI-compatible API', readyz_start)
        readyz = app_text[readyz_start:readyz_end]
        self.assertNotIn("hybrid-shadow", readyz)
        self.assertNotIn("hybrid_retrieval", readyz)


if __name__ == "__main__":
    unittest.main()
