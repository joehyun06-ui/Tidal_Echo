from __future__ import annotations

import asyncio
import contextlib
import io
import os
import types
import unittest
from unittest import mock

from backend import (
    memory_context_integration,
    memory_retrieval_hybrid_fusion as fusion,
    memory_retrieval_hybrid_query as hybrid_query,
    memory_retrieval_hybrid_runtime_shadow as runtime_shadow,
    memory_retrieval_hybrid_shadow as shadow,
)


K1 = "hybrid_shadow_atomic_000001"
K2 = "hybrid_shadow_atomic_000002"
K3 = "hybrid_shadow_atomic_000003"


def safe_item(key: str, content: str) -> dict:
    return {
        "memory_key": key,
        "kind": "project",
        "scope_type": "global_user",
        "scope_ref": "",
        "normalized_content": content,
        "fingerprint_version": 1,
        "status": "active",
        "explicitness": "explicit",
        "confidence": 1.0,
        "sensitivity": "normal",
        "first_observed_at": "2026-01-01T00:00:00Z",
        "last_confirmed_at": "2026-01-02T00:00:00Z",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "provenance": [],
    }


class FakeReadService:
    def __init__(self, items):
        self.items = list(items)

    def get_active_memories(self, **_kwargs):
        return list(self.items)


def hybrid_result(
    keys: tuple[str, ...],
    *,
    bm25_available: bool = True,
    vector_available: bool = True,
    embedding: bool = True,
):
    hits = tuple(
        fusion.HybridFusionHitV1(
            memory_key=key,
            exact_rank=index,
            lexical_rank=index,
            bm25_rank=index if bm25_available else None,
            vector_rank=index if vector_available else None,
            exact_match_count=1,
            channel_count=2 + int(bm25_available) + int(vector_available),
            rank_fusion_score=0.5,
            confidence_boost=0.01,
            recency_boost=0.01,
            touch_boost=0.0,
            final_score=0.52,
        )
        for index, key in enumerate(keys, start=1)
    )
    fused = fusion.HybridFusionResultV1(
        contract_version=fusion.HYBRID_FUSION_CONTRACT_VERSION,
        hits=hits,
        eligible_atomic_count=max(len(keys), 1),
        exact_hit_count=len(keys),
        lexical_hit_count=len(keys),
        bm25_hit_count=len(keys) if bm25_available else 0,
        vector_hit_count=len(keys) if vector_available else 0,
        touch_hint_count=0,
        bm25_available=bm25_available,
        vector_available=vector_available,
    )
    return hybrid_query.HybridQueryResultV1(
        contract_version=hybrid_query.HYBRID_QUERY_CONTRACT_VERSION,
        source_atomic_count=max(len(keys), 1),
        bm25_generation=1 if bm25_available else None,
        vector_generation=1 if vector_available else None,
        query_embedding_performed=embedding,
        fusion_result=fused,
    )


class HybridShadowContractTests(unittest.TestCase):
    def test_reordered_report_is_identity_free(self):
        report = shadow.compare_hybrid_retrieval_shadow_v1(
            (K1, K2),
            hybrid_result((K2, K1)),
        )
        self.assertEqual(report.status, "completed")
        self.assertEqual(report.relation, "reordered")
        self.assertEqual(report.authority_selected_count, 2)
        self.assertEqual(report.hybrid_selected_count, 2)
        self.assertEqual(report.overlap_count, 2)
        line = shadow.render_hybrid_retrieval_shadow_telemetry_v1(report)
        self.assertIn("relation=reordered", line)
        self.assertIn("authority=2", line)
        self.assertIn("hybrid=2", line)
        for private in (K1, K2):
            self.assertNotIn(private, repr(report))
            self.assertNotIn(private, line)

    def test_subset_superset_and_mixed_relations_are_structural(self):
        subset = shadow.compare_hybrid_retrieval_shadow_v1(
            (K1, K2), hybrid_result((K1,))
        )
        superset = shadow.compare_hybrid_retrieval_shadow_v1(
            (K1,), hybrid_result((K1, K2))
        )
        mixed = shadow.compare_hybrid_retrieval_shadow_v1(
            (K1, K2), hybrid_result((K2, K3))
        )
        self.assertEqual(subset.relation, "hybrid_subset")
        self.assertEqual(superset.relation, "hybrid_superset")
        self.assertEqual(mixed.relation, "mixed")

    def test_invalid_or_duplicate_identity_input_fails_soft(self):
        for keys in ((K1, K1), ("bad",), [K1]):
            with self.subTest(keys=keys):
                report = shadow.compare_hybrid_retrieval_shadow_v1(
                    keys, hybrid_result((K1,))
                )
                self.assertEqual(report.status, "failed")
                self.assertEqual(report.category, shadow.SHADOW_FAILURE_CATEGORY)
                line = shadow.render_hybrid_retrieval_shadow_telemetry_v1(report)
                self.assertEqual(
                    line,
                    "[memory-hybrid-retrieval-shadow] status=failed "
                    "category=memory_hybrid_retrieval_shadow_unavailable",
                )


class AuthorityKeyProjectionTests(unittest.TestCase):
    def setUp(self):
        self.base = (
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "render deployment"},
        )

    def test_smart_v1_keys_follow_exact_provider_visible_selection(self):
        relevant = safe_item(K1, "render deployment decision")
        unrelated = safe_item(K2, "android frontend")
        result = memory_context_integration.prepare_transient_memory_dispatch(
            FakeReadService((unrelated, relevant)),
            self.base,
            enabled=True,
            smart_retrieval_enabled=True,
        )
        self.assertEqual(result.authoritative_memory_keys, (K1,))
        self.assertNotIn(K1, repr(result))
        self.assertNotIn(K2, repr(result))

    def test_empty_smart_selection_exposes_empty_tuple_not_unknown(self):
        result = memory_context_integration.prepare_transient_memory_dispatch(
            FakeReadService((safe_item(K1, "orchid memory"),)),
            (
                {"role": "system", "content": "persona"},
                {"role": "user", "content": "weather today"},
            ),
            enabled=True,
            smart_retrieval_enabled=True,
        )
        self.assertEqual(result.authoritative_memory_keys, ())
        self.assertFalse(result.memory_applied)


class HybridRuntimeShadowTests(unittest.IsolatedAsyncioTestCase):
    def _relay(self, original_prepare):
        @contextlib.asynccontextmanager
        async def base_lifespan(_application):
            yield

        context_module = types.SimpleNamespace(
            prepare_transient_memory_dispatch=original_prepare,
        )
        memory = types.SimpleNamespace(
            enabled=True,
            configuration_valid=True,
            context_injection_enabled=True,
            smart_retrieval_enabled=True,
        )
        return types.SimpleNamespace(
            DEPLOYMENT=types.SimpleNamespace(memory=memory),
            memory_context_integration=context_module,
            app=types.SimpleNamespace(
                router=types.SimpleNamespace(lifespan_context=base_lifespan)
            ),
        )

    async def test_gate_off_is_exact_callable_and_lifespan_noop(self):
        def original(*_args, **_kwargs):
            raise AssertionError("not called")

        relay = self._relay(original)
        original_lifespan = relay.app.router.lifespan_context
        with mock.patch.dict(
            os.environ,
            {runtime_shadow.ENV_GATE: "false"},
            clear=False,
        ):
            enabled = runtime_shadow.install(relay, runner=object())
        self.assertFalse(enabled)
        self.assertIs(
            relay.memory_context_integration.prepare_transient_memory_dispatch,
            original,
        )
        self.assertIs(relay.app.router.lifespan_context, original_lifespan)

    async def test_enabled_without_runner_fails_closed_before_patch(self):
        def original(*_args, **_kwargs):
            return None

        relay = self._relay(original)
        with mock.patch.dict(
            os.environ,
            {runtime_shadow.ENV_GATE: "true"},
            clear=False,
        ):
            with self.assertRaises(
                runtime_shadow.MemoryHybridRetrievalRuntimeShadowError
            ) as raised:
                runtime_shadow.install(relay)
        self.assertEqual(
            raised.exception.category,
            "memory_hybrid_retrieval_shadow_runner_missing",
        )
        self.assertIs(
            relay.memory_context_integration.prepare_transient_memory_dispatch,
            original,
        )

    async def test_worker_hook_returns_authority_dispatch_before_shadow_finishes(self):
        base = (
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "render deployment"},
        )
        dispatch = memory_context_integration.TransientMemoryDispatch(
            base,
            False,
            authoritative_memory_keys=(K1,),
        )

        def original(*_args, **_kwargs):
            return dispatch

        relay = self._relay(original)
        started = asyncio.Event()
        release = asyncio.Event()
        seen = []

        async def runner(*, query_text):
            seen.append(query_text)
            started.set()
            await release.wait()
            return hybrid_result((K1,))

        stderr = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {runtime_shadow.ENV_GATE: "true"},
                clear=False,
            ),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertTrue(runtime_shadow.install(relay, runner=runner))
            async with relay.app.router.lifespan_context(None):
                returned = await asyncio.to_thread(
                    relay.memory_context_integration.prepare_transient_memory_dispatch,
                    object(),
                    base,
                    enabled=True,
                    smart_retrieval_enabled=True,
                )
                self.assertIs(returned, dispatch)
                self.assertEqual(returned.provider_messages, base)
                await asyncio.wait_for(started.wait(), timeout=1)
                self.assertEqual(seen, ["render deployment"])
                self.assertNotIn("status=completed", stderr.getvalue())
                release.set()
                task = getattr(relay, runtime_shadow.TASK_MARKER)
                await asyncio.wait_for(asyncio.shield(task), timeout=1)
        output = stderr.getvalue()
        self.assertIn("status=completed", output)
        self.assertIn("relation=identical", output)
        self.assertNotIn(K1, output)
        self.assertNotIn("render deployment", output)

    async def test_single_inflight_shadow_drops_busy_query_without_queueing_text(self):
        base_one = ({"role": "user", "content": "first query"},)
        base_two = ({"role": "user", "content": "second query"},)

        def original(_read, messages, **_kwargs):
            return memory_context_integration.TransientMemoryDispatch(
                messages,
                False,
                authoritative_memory_keys=(K1,),
            )

        relay = self._relay(original)
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def runner(*, query_text):
            calls.append(query_text)
            started.set()
            await release.wait()
            return hybrid_result((K1,))

        stderr = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {runtime_shadow.ENV_GATE: "true"},
                clear=False,
            ),
            contextlib.redirect_stderr(stderr),
        ):
            runtime_shadow.install(relay, runner=runner)
            async with relay.app.router.lifespan_context(None):
                await asyncio.to_thread(
                    relay.memory_context_integration.prepare_transient_memory_dispatch,
                    object(), base_one,
                )
                await asyncio.wait_for(started.wait(), timeout=1)
                await asyncio.to_thread(
                    relay.memory_context_integration.prepare_transient_memory_dispatch,
                    object(), base_two,
                )
                await asyncio.sleep(0.05)
                self.assertEqual(calls, ["first query"])
                self.assertIn("status=skipped reason=busy", stderr.getvalue())
                self.assertNotIn("second query", stderr.getvalue())
                release.set()
                task = getattr(relay, runtime_shadow.TASK_MARKER)
                await asyncio.wait_for(asyncio.shield(task), timeout=1)

    async def test_runner_failure_is_shadow_only_and_data_free(self):
        base = ({"role": "user", "content": "PRIVATE-QUERY-TEXT"},)
        dispatch = memory_context_integration.TransientMemoryDispatch(
            base,
            False,
            authoritative_memory_keys=(K1,),
        )

        def original(*_args, **_kwargs):
            return dispatch

        relay = self._relay(original)
        finished = asyncio.Event()

        async def runner(*, query_text):
            try:
                raise RuntimeError(query_text + K1)
            finally:
                finished.set()

        stderr = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {runtime_shadow.ENV_GATE: "true"},
                clear=False,
            ),
            contextlib.redirect_stderr(stderr),
        ):
            runtime_shadow.install(relay, runner=runner)
            async with relay.app.router.lifespan_context(None):
                returned = await asyncio.to_thread(
                    relay.memory_context_integration.prepare_transient_memory_dispatch,
                    object(), base,
                )
                self.assertIs(returned, dispatch)
                await asyncio.wait_for(finished.wait(), timeout=1)
                task = getattr(relay, runtime_shadow.TASK_MARKER)
                if task is not None:
                    await asyncio.wait_for(asyncio.shield(task), timeout=1)
        output = stderr.getvalue()
        self.assertIn("status=failed", output)
        self.assertNotIn("PRIVATE-QUERY-TEXT", output)
        self.assertNotIn(K1, output)


if __name__ == "__main__":
    unittest.main()
