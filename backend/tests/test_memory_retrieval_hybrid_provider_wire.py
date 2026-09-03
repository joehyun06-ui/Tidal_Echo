from __future__ import annotations

import asyncio
import types
import unittest
from pathlib import Path
from unittest import mock

from backend import (
    kelivo_service,
    memory_context_integration,
    memory_retrieval_hybrid_provider_wire as provider_wire,
    memory_retrieval_hybrid_runtime_active as runtime_active,
)


class FakeSelection:
    selected_count = 1
    total_chars = 37
    query_embedding_performed = True


class HybridActiveProviderWireTests(unittest.IsolatedAsyncioTestCase):
    def _relay(self, generator, *, active_enabled: bool = True):
        tracker = runtime_active.HybridActiveObservabilityV1()
        relay = types.SimpleNamespace(KELIVO_GENERATOR=generator)
        setattr(relay, runtime_active.INSTALL_MARKER, True)
        setattr(relay, runtime_active.ENABLED_MARKER, active_enabled)
        if active_enabled:
            setattr(relay, runtime_active.TRACKER_MARKER, tracker)
        return relay, tracker

    def test_gate_off_is_exact_noop(self):
        async def generator(*_args, **_kwargs):
            return "ok"

        relay, _tracker = self._relay(generator, active_enabled=False)
        self.assertFalse(provider_wire.install(relay))
        self.assertIs(relay.KELIVO_GENERATOR, generator)
        self.assertTrue(getattr(relay, provider_wire.INSTALL_MARKER))
        self.assertFalse(getattr(relay, provider_wire.ENABLED_MARKER))

    async def test_provider_success_commits_completion_only_after_generator_returns(self):
        relay = None
        tracker = None

        async def active_generator(*_args, **_kwargs):
            tracker.record_attempt()
            tracker.record_completed(FakeSelection(), 4)
            # D3C2.1 must defer terminal completion while provider work is live.
            snapshot = tracker.snapshot()
            self.assertEqual(snapshot["completed"], 0)
            self.assertEqual(snapshot["in_flight"], 1)
            return {"text": "ok"}

        relay, tracker = self._relay(active_generator)
        self.assertTrue(provider_wire.install(relay))
        result = await relay.KELIVO_GENERATOR(
            ( {"role": "user", "content": "query"}, ),
            "session",
            "model",
            0.7,
            2000,
            {"prompt_contract_version": kelivo_service.PROMPT_CONTRACT_VERSION},
        )
        self.assertEqual(result, {"text": "ok"})
        status = runtime_active.status_payload_v1(relay)
        self.assertEqual(status["attempts"], 1)
        self.assertEqual(status["outcomes"]["completed"], 1)
        self.assertEqual(status["outcomes"]["failed"], 0)
        self.assertEqual(status["in_flight"], 0)
        self.assertEqual(status["last"]["status"], "completed")
        self.assertEqual(status["last"]["selected_count"], 1)
        self.assertEqual(status["last"]["total_chars"], 37)
        self.assertTrue(status["last"]["query_embedding_performed"])

    async def test_provider_explicit_rejection_reclassifies_premature_completion_as_failed(self):
        relay = None
        tracker = None

        async def active_generator(*_args, **_kwargs):
            tracker.record_attempt()
            tracker.record_completed(FakeSelection(), 5)
            raise kelivo_service.GenerationError("provider_explicit_rejection", False)

        relay, tracker = self._relay(active_generator)
        provider_wire.install(relay)
        with self.assertRaises(kelivo_service.GenerationError):
            await relay.KELIVO_GENERATOR(
                ({"role": "user", "content": "query"},),
                "session",
                "model",
                0.7,
                2000,
                {},
            )
        status = runtime_active.status_payload_v1(relay)
        self.assertEqual(status["attempts"], 1)
        self.assertEqual(status["outcomes"]["completed"], 0)
        self.assertEqual(status["outcomes"]["failed"], 1)
        self.assertEqual(status["outcomes"]["cancelled"], 0)
        self.assertEqual(status["in_flight"], 0)
        self.assertEqual(status["last"]["status"], "failed")
        self.assertEqual(
            status["last"]["failure_category"],
            "provider_explicit_rejection",
        )
        # Retrieval structural evidence remains visible without retaining data.
        self.assertEqual(status["last"]["selected_count"], 1)
        self.assertEqual(status["last"]["total_chars"], 37)
        self.assertTrue(status["last"]["query_embedding_performed"])

    async def test_unknown_provider_error_is_bounded_and_data_free(self):
        private = "PRIVATE query and memory text must never surface"
        relay = None
        tracker = None

        async def active_generator(*_args, **_kwargs):
            tracker.record_attempt()
            tracker.record_completed(FakeSelection(), 5)
            raise kelivo_service.GenerationError(private, False)

        relay, tracker = self._relay(active_generator)
        provider_wire.install(relay)
        with mock.patch("builtins.print") as printed:
            with self.assertRaises(kelivo_service.GenerationError):
                await relay.KELIVO_GENERATOR(
                    ({"role": "user", "content": private},),
                    "session",
                    "model",
                    0.7,
                    2000,
                    {},
                )
        status = runtime_active.status_payload_v1(relay)
        encoded = repr(status) + repr(printed.call_args_list)
        self.assertEqual(
            status["last"]["failure_category"],
            "provider_generation_failed",
        )
        self.assertNotIn(private, encoded)

    async def test_provider_cancellation_reclassifies_completion_as_cancelled(self):
        relay = None
        tracker = None

        async def active_generator(*_args, **_kwargs):
            tracker.record_attempt()
            tracker.record_completed(FakeSelection(), 5)
            raise asyncio.CancelledError()

        relay, tracker = self._relay(active_generator)
        provider_wire.install(relay)
        with self.assertRaises(asyncio.CancelledError):
            await relay.KELIVO_GENERATOR(
                ({"role": "user", "content": "query"},),
                "session",
                "model",
                0.7,
                2000,
                {},
            )
        status = runtime_active.status_payload_v1(relay)
        self.assertEqual(status["outcomes"]["completed"], 0)
        self.assertEqual(status["outcomes"]["cancelled"], 1)
        self.assertEqual(status["in_flight"], 0)
        self.assertEqual(status["last"]["status"], "cancelled")
        self.assertEqual(status["last"]["selected_count"], 1)

    async def test_retrieval_failure_before_provider_is_not_double_counted(self):
        relay = None
        tracker = None

        async def active_generator(*_args, **_kwargs):
            tracker.record_attempt()
            tracker.record_failed("hybrid_active_stale", 3)
            raise memory_context_integration.MemoryContextIntegrationError()

        relay, tracker = self._relay(active_generator)
        provider_wire.install(relay)
        with self.assertRaises(memory_context_integration.MemoryContextIntegrationError):
            await relay.KELIVO_GENERATOR(
                ({"role": "user", "content": "query"},),
                "session",
                "model",
                0.7,
                2000,
                {},
            )
        status = runtime_active.status_payload_v1(relay)
        self.assertEqual(status["outcomes"]["completed"], 0)
        self.assertEqual(status["outcomes"]["failed"], 1)
        self.assertEqual(status["in_flight"], 0)
        self.assertEqual(status["last"]["failure_category"], "hybrid_active_stale")

    async def test_non_active_generator_calls_are_argument_transparent(self):
        captured = []

        async def generator(*args, **kwargs):
            captured.append((args, kwargs))
            return "passthrough"

        relay, tracker = self._relay(generator)
        provider_wire.install(relay)
        messages = ({"role": "user", "content": "normal"},)
        context = {"prompt_contract_version": kelivo_service.PROMPT_CONTRACT_VERSION}
        result = await relay.KELIVO_GENERATOR(
            messages,
            "session",
            "model",
            0.5,
            100,
            context,
        )
        self.assertEqual(result, "passthrough")
        self.assertEqual(
            captured[0][0],
            (messages, "session", "model", 0.5, 100, context),
        )
        self.assertEqual(captured[0][1], {})
        self.assertEqual(runtime_active.status_payload_v1(relay)["attempts"], 0)

    def test_p3_installs_provider_wire_between_active_and_shadow(self):
        root = Path(__file__).resolve().parents[2]
        source = (root / "backend" / "p3_relay_app.py").read_text(encoding="utf-8")
        self.assertIn("memory_retrieval_hybrid_provider_wire", source)
        active_pos = source.index("memory_retrieval_hybrid_runtime_active.install(relay_app)")
        wire_pos = source.index("memory_retrieval_hybrid_provider_wire.install(relay_app)")
        shadow_pos = source.index("memory_retrieval_hybrid_runtime_shadow.install(")
        self.assertLess(active_pos, wire_pos)
        self.assertLess(wire_pos, shadow_pos)


if __name__ == "__main__":
    unittest.main()
