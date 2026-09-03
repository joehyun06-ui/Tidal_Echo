from __future__ import annotations

import asyncio
import json
import types
import unittest
from unittest import mock

from backend import (
    deployment_config,
    kelivo_service,
    memory_context_integration,
    memory_retrieval_hybrid_active as active,
    memory_retrieval_hybrid_runtime_active as runtime_active,
    memory_retrieval_hybrid_runtime_shadow as runtime_shadow,
)


QUERY = "PRIVATE hybrid active query"
MEMORY_KEY = "hybrid_active_runtime_memory_0001"
MEMORY_TEXT = "private hybrid active memory text"
BASE = (
    {"role": "system", "content": "persona"},
    {"role": "user", "content": QUERY},
)


class FakeSelection:
    selected_count = 1
    total_chars = len(MEMORY_TEXT)
    query_embedding_performed = True


class HybridActiveRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def _relay(
        self,
        *,
        original_prepare=None,
        original_generator=None,
        v2_shadow=False,
        v2_active=False,
    ):
        if original_prepare is None:
            def original_prepare(*_args, **_kwargs):
                raise AssertionError("legacy selector must not run")
        if original_generator is None:
            async def original_generator(
                messages,
                api_session,
                provider_model,
                temperature,
                max_tokens,
                context,
            ):
                return {
                    "messages": messages,
                    "api_session": api_session,
                    "provider_model": provider_model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "context": context,
                }
        memory = types.SimpleNamespace(
            enabled=True,
            configuration_valid=True,
            context_injection_enabled=True,
            smart_retrieval_enabled=True,
            retrieval_v2_shadow_enabled=v2_shadow,
            retrieval_v2_active_enabled=v2_active,
        )
        return types.SimpleNamespace(
            DEPLOYMENT=types.SimpleNamespace(memory=memory),
            memory_context_integration=types.SimpleNamespace(
                prepare_transient_memory_dispatch=original_prepare,
            ),
            KELIVO_GENERATOR=original_generator,
        )

    def _env(self, *, active_enabled="true", shadow_enabled="false"):
        return {
            runtime_active.ENV_GATE: active_enabled,
            runtime_shadow.ENV_GATE: shadow_enabled,
        }

    def _install(self, relay, *, env=None):
        with mock.patch.object(
            runtime_active,
            "_compose_runner",
            return_value=object(),
        ):
            return runtime_active.install(
                relay,
                environ=self._env() if env is None else env,
            )

    def test_gate_defaults_off_and_strict_boolean_parser_is_reused(self):
        self.assertFalse(runtime_active.enabled_from_environment({}))
        self.assertTrue(runtime_active.enabled_from_environment({runtime_active.ENV_GATE: "true"}))
        self.assertTrue(runtime_active.enabled_from_environment({runtime_active.ENV_GATE: "1"}))
        for invalid in (" true ", "maybe", "１"):
            with self.subTest(invalid=invalid), self.assertRaises(
                deployment_config.DeploymentConfigError
            ):
                runtime_active.enabled_from_environment({runtime_active.ENV_GATE: invalid})

    def test_gate_off_is_exact_prepare_and_generator_noop(self):
        def original_prepare(*_args, **_kwargs):
            return "legacy"

        async def original_generator(*_args, **_kwargs):
            return "provider"

        relay = self._relay(
            original_prepare=original_prepare,
            original_generator=original_generator,
        )
        enabled = runtime_active.install(
            relay,
            environ=self._env(active_enabled="false"),
        )
        self.assertFalse(enabled)
        self.assertIs(
            relay.memory_context_integration.prepare_transient_memory_dispatch,
            original_prepare,
        )
        self.assertIs(relay.KELIVO_GENERATOR, original_generator)
        status = runtime_active.status_payload_v1(relay)
        self.assertFalse(status["enabled"])
        self.assertTrue(status["installed"])
        self.assertEqual(status["attempts"], 0)

    def test_shadow_and_active_are_mutually_exclusive_before_patch(self):
        def original_prepare(*_args, **_kwargs):
            return None

        async def original_generator(*_args, **_kwargs):
            return None

        relay = self._relay(
            original_prepare=original_prepare,
            original_generator=original_generator,
        )
        with self.assertRaises(
            runtime_active.MemoryHybridRetrievalRuntimeActiveError
        ) as raised:
            runtime_active.install(
                relay,
                environ=self._env(shadow_enabled="true"),
            )
        self.assertEqual(
            raised.exception.category,
            "memory_hybrid_active_conflicts_shadow",
        )
        self.assertIs(
            relay.memory_context_integration.prepare_transient_memory_dispatch,
            original_prepare,
        )
        self.assertIs(relay.KELIVO_GENERATOR, original_generator)

    def test_v2_shadow_or_active_conflicts_with_hybrid_active(self):
        for kwargs in ({"v2_shadow": True}, {"v2_active": True}):
            with self.subTest(kwargs=kwargs):
                relay = self._relay(**kwargs)
                with self.assertRaises(
                    runtime_active.MemoryHybridRetrievalRuntimeActiveError
                ) as raised:
                    self._install(relay)
                self.assertEqual(
                    raised.exception.category,
                    "memory_hybrid_active_conflicts_v2",
                )

    def test_enabled_missing_generator_fails_before_prepare_patch(self):
        def original_prepare(*_args, **_kwargs):
            return None

        relay = self._relay(original_prepare=original_prepare)
        relay.KELIVO_GENERATOR = None
        with self.assertRaises(
            runtime_active.MemoryHybridRetrievalRuntimeActiveError
        ) as raised:
            self._install(relay)
        self.assertEqual(
            raised.exception.category,
            "memory_hybrid_active_generator_missing",
        )
        self.assertIs(
            relay.memory_context_integration.prepare_transient_memory_dispatch,
            original_prepare,
        )

    def test_active_runner_reuses_d3b2_config_loader_without_mutating_real_gate(self):
        relay = self._relay()
        env = self._env(active_enabled="true", shadow_enabled="false")
        seen = []
        fake_runner = object()

        def compose(_relay, projected):
            seen.append(dict(projected))
            return fake_runner

        with mock.patch.object(
            runtime_active.memory_retrieval_hybrid_runtime_composition,
            "compose_hybrid_retrieval_shadow_runner_v1",
            side_effect=compose,
        ), mock.patch.object(
            runtime_active.memory_retrieval_hybrid_runtime_composition,
            "HybridRetrievalShadowRunnerV1",
            object,
        ):
            result = runtime_active._compose_runner(relay, env)
        self.assertIs(result, fake_runner)
        self.assertEqual(env[runtime_shadow.ENV_GATE], "false")
        self.assertEqual(seen[0][runtime_shadow.ENV_GATE], "true")
        self.assertEqual(seen[0][runtime_active.ENV_GATE], "true")

    def test_prepare_inserts_one_internal_sentinel_and_never_runs_legacy_selector(self):
        relay = self._relay()
        self.assertTrue(self._install(relay))
        dispatch = relay.memory_context_integration.prepare_transient_memory_dispatch(
            object(),
            BASE,
            enabled=True,
            smart_retrieval_enabled=True,
            retrieval_v2_shadow_enabled=False,
            retrieval_v2_active_enabled=False,
        )
        self.assertFalse(dispatch.memory_applied)
        self.assertEqual(len(dispatch.provider_messages), len(BASE) + 1)
        self.assertEqual(dispatch.provider_messages[-2]["role"], "developer")
        self.assertEqual(
            dispatch.provider_messages[-2]["content"],
            runtime_active.PENDING_SENTINEL_CONTENT,
        )
        self.assertEqual(dispatch.provider_messages[-1], BASE[-1])
        self.assertEqual(dispatch.authoritative_memory_keys, ())

    def test_prepare_rejects_wrong_selector_mode_and_existing_sentinel(self):
        relay = self._relay()
        self._install(relay)
        prepare = relay.memory_context_integration.prepare_transient_memory_dispatch
        with self.assertRaises(memory_context_integration.MemoryContextIntegrationError):
            prepare(
                object(),
                BASE,
                enabled=True,
                smart_retrieval_enabled=False,
            )
        forged = (
            {"role": "developer", "content": runtime_active.PENDING_SENTINEL_CONTENT},
            {"role": "user", "content": QUERY},
        )
        with self.assertRaises(memory_context_integration.MemoryContextIntegrationError):
            prepare(
                object(),
                forged,
                enabled=True,
                smart_retrieval_enabled=True,
            )

    async def _pending_dispatch(self, relay):
        return relay.memory_context_integration.prepare_transient_memory_dispatch(
            object(),
            BASE,
            enabled=True,
            smart_retrieval_enabled=True,
            retrieval_v2_shadow_enabled=False,
            retrieval_v2_active_enabled=False,
        )

    async def test_async_generator_replaces_sentinel_with_d3c1_memory_envelope(self):
        seen = []

        async def original_generator(
            messages,
            api_session,
            provider_model,
            temperature,
            max_tokens,
            context,
        ):
            seen.append((messages, context))
            return {"ok": True}

        relay = self._relay(original_generator=original_generator)
        self._install(relay)
        dispatch = await self._pending_dispatch(relay)
        developer_message = {
            "role": "developer",
            "content": '{"version":"memory_context_developer_message/v1","memory_context":{"items":[]}}',
        }
        with mock.patch.object(
            active,
            "plan_hybrid_active_selection_v1",
            new=mock.AsyncMock(return_value=FakeSelection()),
        ) as planner, mock.patch.object(
            active,
            "render_hybrid_active_developer_message_v1",
            return_value=developer_message,
        ):
            result = await relay.KELIVO_GENERATOR(
                dispatch.provider_messages,
                "session",
                "provider-model",
                0.7,
                2000,
                {"prompt_contract_version": kelivo_service.PROMPT_CONTRACT_VERSION},
            )
        self.assertEqual(result, {"ok": True})
        planner.assert_awaited_once_with(mock.ANY, query_text=QUERY)
        messages, context = seen[0]
        self.assertNotIn(runtime_active.PENDING_SENTINEL_CONTENT, str(messages))
        self.assertEqual(messages[-2], developer_message)
        self.assertEqual(messages[-1], BASE[-1])
        self.assertEqual(
            context["transient_memory_dispatch"],
            kelivo_service.TRANSIENT_MEMORY_DISPATCH_VERSION,
        )
        status = runtime_active.status_payload_v1(relay)
        self.assertEqual(status["attempts"], 1)
        self.assertEqual(status["outcomes"]["completed"], 1)
        self.assertEqual(status["outcomes"]["failed"], 0)
        self.assertEqual(status["last"]["selected_count"], 1)
        self.assertTrue(status["last"]["query_embedding_performed"])

    async def test_empty_active_selection_removes_sentinel_without_memory_marker(self):
        seen = []

        async def original_generator(messages, *_args):
            seen.append(messages)
            return {"ok": True}

        relay = self._relay(original_generator=original_generator)
        self._install(relay)
        dispatch = await self._pending_dispatch(relay)
        selection = types.SimpleNamespace(
            selected_count=0,
            total_chars=0,
            query_embedding_performed=True,
        )
        with mock.patch.object(
            active,
            "plan_hybrid_active_selection_v1",
            new=mock.AsyncMock(return_value=selection),
        ), mock.patch.object(
            active,
            "render_hybrid_active_developer_message_v1",
            return_value=None,
        ):
            result = await relay.KELIVO_GENERATOR(
                dispatch.provider_messages,
                "session",
                "provider-model",
                0.7,
                2000,
                {"prompt_contract_version": kelivo_service.PROMPT_CONTRACT_VERSION},
            )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen[0], BASE)

    async def test_non_sentinel_generator_calls_are_exact_passthrough(self):
        captured = []

        async def original_generator(*args):
            captured.append(args)
            return "passthrough"

        relay = self._relay(original_generator=original_generator)
        self._install(relay)
        context = {"prompt_contract_version": kelivo_service.PROMPT_CONTRACT_VERSION}
        result = await relay.KELIVO_GENERATOR(
            BASE, "session", "model", 0.5, 100, context
        )
        self.assertEqual(result, "passthrough")
        self.assertEqual(captured[0], (BASE, "session", "model", 0.5, 100, context))
        self.assertEqual(runtime_active.status_payload_v1(relay)["attempts"], 0)

    async def test_hybrid_failure_fails_closed_without_calling_old_selector_or_generator(self):
        generator_called = False

        async def original_generator(*_args):
            nonlocal generator_called
            generator_called = True
            return None

        relay = self._relay(original_generator=original_generator)
        self._install(relay)
        dispatch = await self._pending_dispatch(relay)
        error = active.MemoryRetrievalHybridActiveError("hybrid_active_stale")
        with mock.patch.object(
            active,
            "plan_hybrid_active_selection_v1",
            new=mock.AsyncMock(side_effect=error),
        ):
            with self.assertRaises(memory_context_integration.MemoryContextIntegrationError):
                await relay.KELIVO_GENERATOR(
                    dispatch.provider_messages,
                    "session",
                    "model",
                    0.7,
                    2000,
                    {"prompt_contract_version": kelivo_service.PROMPT_CONTRACT_VERSION},
                )
        self.assertFalse(generator_called)
        status = runtime_active.status_payload_v1(relay)
        self.assertEqual(status["outcomes"]["failed"], 1)
        self.assertEqual(status["last"]["failure_category"], "hybrid_active_stale")

    async def test_active_retrieval_timeout_is_bounded_and_fails_closed(self):
        relay = self._relay()
        self._install(relay)
        dispatch = await self._pending_dispatch(relay)

        async def slow_plan(*_args, **_kwargs):
            await asyncio.sleep(10)

        with mock.patch.object(
            runtime_active,
            "ACTIVE_RETRIEVAL_TIMEOUT_SECONDS",
            0.01,
        ), mock.patch.object(
            active,
            "plan_hybrid_active_selection_v1",
            side_effect=slow_plan,
        ):
            with self.assertRaises(memory_context_integration.MemoryContextIntegrationError):
                await relay.KELIVO_GENERATOR(
                    dispatch.provider_messages,
                    "session",
                    "model",
                    0.7,
                    2000,
                    {"prompt_contract_version": kelivo_service.PROMPT_CONTRACT_VERSION},
                )
        status = runtime_active.status_payload_v1(relay)
        self.assertEqual(status["outcomes"]["timed_out"], 1)
        self.assertEqual(status["in_flight"], 0)

    async def test_client_cancellation_propagates_through_active_embedding_path(self):
        relay = self._relay()
        self._install(relay)
        dispatch = await self._pending_dispatch(relay)
        with mock.patch.object(
            active,
            "plan_hybrid_active_selection_v1",
            new=mock.AsyncMock(side_effect=asyncio.CancelledError()),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await relay.KELIVO_GENERATOR(
                    dispatch.provider_messages,
                    "session",
                    "model",
                    0.7,
                    2000,
                    {"prompt_contract_version": kelivo_service.PROMPT_CONTRACT_VERSION},
                )
        status = runtime_active.status_payload_v1(relay)
        self.assertEqual(status["outcomes"]["cancelled"], 1)
        self.assertEqual(status["in_flight"], 0)

    async def test_status_is_bounded_and_data_free(self):
        relay = self._relay()
        self._install(relay)
        dispatch = await self._pending_dispatch(relay)
        with mock.patch.object(
            active,
            "plan_hybrid_active_selection_v1",
            new=mock.AsyncMock(return_value=FakeSelection()),
        ), mock.patch.object(
            active,
            "render_hybrid_active_developer_message_v1",
            return_value=None,
        ):
            await relay.KELIVO_GENERATOR(
                dispatch.provider_messages,
                "session",
                "model",
                0.7,
                2000,
                {"prompt_contract_version": kelivo_service.PROMPT_CONTRACT_VERSION},
            )
        payload = runtime_active.status_payload_v1(relay)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.assertTrue(payload["enabled"])
        self.assertTrue(payload["installed"])
        self.assertTrue(payload["observability_available"])
        self.assertEqual(payload["attempts"], 1)
        for private in (QUERY, MEMORY_KEY, MEMORY_TEXT, runtime_active.PENDING_SENTINEL_CONTENT):
            self.assertNotIn(private, encoded)


if __name__ == "__main__":
    unittest.main()
