from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from backend.codex_generation_protocol import (
    CodexGenerationConfig,
    CodexGenerationError,
    CodexGenerationProtocol,
    CodexProcessActivityGate,
    correlated_turn_from_page,
    deterministic_workspace,
    final_answer_from_turn,
    input_digest,
    project_notification,
    resolve_model,
)


class FakeTransport:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    async def request(self, method, params):
        self.calls.append((method, params))
        response = self.responses.get(method)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(params)
        if response is None:
            return {}
        return response


class GenerationProtocolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def config(self, enabled=True):
        return CodexGenerationConfig(enabled, self.root)

    def happy_transport(self):
        return FakeTransport({
            "account/read": {"account": {"type": "chatgpt", "planType": "plus"}},
            "model/list": {
                "data": [
                    {"model": "gpt-5.6-sol", "isDefault": True, "defaultReasoningEffort": "high"},
                    {"model": "gpt-5.4", "isDefault": False, "defaultReasoningEffort": "medium"},
                ]
            },
            "thread/start": lambda params: {
                "thread": {
                    "id": "thr-123",
                    "ephemeral": False,
                    "historyMode": "paginated",
                },
                "model": params["model"],
                "modelProvider": "openai",
                "cwd": params["cwd"],
            },
            "thread/resume": lambda params: {
                "thread": {"id": params["threadId"], "historyMode": "paginated"},
                "initialTurnsPage": {"data": []},
            },
            "turn/start": {"turn": {"id": "turn-123", "status": "inProgress"}},
            "turn/interrupt": {},
            "thread/unsubscribe": {},
        })

    async def test_disabled_mode_is_network_and_transport_free(self):
        transport = self.happy_transport()
        protocol = CodexGenerationProtocol(self.config(enabled=False), transport)
        with self.assertRaisesRegex(CodexGenerationError, "codex_generation_disabled"):
            await protocol.qualify()
        self.assertEqual(transport.calls, [])

    def test_environment_config_defaults_off_and_is_strict(self):
        config = CodexGenerationConfig.from_environ({}, persistent_root=Path("/var/data"))
        self.assertFalse(config.enabled)
        self.assertEqual(config.workspace_root, Path("/var/data/codex-workspace"))
        self.assertEqual(config.model_policy, "default")
        with self.assertRaises(CodexGenerationError):
            CodexGenerationConfig.from_environ({"CODEX_GENERATION_ENABLED": "1"})

    def test_workspace_is_attempt_scoped_and_bounded(self):
        path = deterministic_workspace(self.root, "api-abc", "attempt-1")
        self.assertEqual(path, self.root / "sessions" / "api-abc" / "attempt-1")
        with self.assertRaises(CodexGenerationError):
            deterministic_workspace(self.root, "../escape", "attempt-1")

    def test_model_selection_requires_one_default_and_pins_effort(self):
        selected = resolve_model({
            "data": [{"model": "gpt-5.6-sol", "isDefault": True, "defaultReasoningEffort": "high"}]
        })
        self.assertEqual((selected.model, selected.reasoning_effort), ("gpt-5.6-sol", "high"))
        with self.assertRaises(CodexGenerationError):
            resolve_model({"data": [
                {"model": "a", "isDefault": True},
                {"model": "b", "isDefault": True},
            ]})

    async def test_start_thread_pins_paginated_model_provider_and_persona_contract(self):
        transport = self.happy_transport()
        protocol = CodexGenerationProtocol(self.config(), transport)
        result = await protocol.start_thread(
            api_session="api-abc", attempt_id="attempt-1", persona="companion persona"
        )
        self.assertEqual(result.thread_id, "thr-123")
        self.assertEqual(result.model_provider, "openai")
        methods = [method for method, _ in transport.calls]
        self.assertEqual(methods, ["account/read", "model/list", "thread/start"])
        params = transport.calls[-1][1]
        self.assertEqual(params["historyMode"], "paginated")
        self.assertIs(params["ephemeral"], False)
        self.assertEqual(params["baseInstructions"], "companion persona")
        self.assertEqual(params["model"], "gpt-5.6-sol")
        self.assertNotIn("config", params)
        self.assertNotIn("environments", params)

    async def test_start_thread_rejects_non_paginated_response(self):
        transport = self.happy_transport()
        transport.responses["thread/start"] = lambda params: {
            "thread": {"id": "thr-123", "ephemeral": False, "historyMode": "legacy"},
            "model": params["model"], "modelProvider": "openai", "cwd": params["cwd"],
        }
        protocol = CodexGenerationProtocol(self.config(), transport)
        with self.assertRaisesRegex(CodexGenerationError, "thread_contract_mismatch"):
            await protocol.start_thread(api_session="api-abc", attempt_id="attempt-1", persona="x")

    async def test_resume_requests_bounded_summary_page_and_pinned_provider(self):
        transport = self.happy_transport()
        protocol = CodexGenerationProtocol(self.config(), transport)
        page = await protocol.resume_thread(
            thread_id="thr-123",
            model="gpt-5.6-sol",
            model_provider="openai",
            reasoning_effort="high",
            cwd=self.root / "sessions" / "api-abc" / "attempt-1",
            persona="current persona",
        )
        self.assertEqual(page, {"data": []})
        method, params = transport.calls[-1]
        self.assertEqual(method, "thread/resume")
        self.assertTrue(params["excludeTurns"])
        self.assertEqual(params["modelProvider"], "openai")
        self.assertEqual(params["initialTurnsPage"], {
            "limit": 8,
            "sortDirection": "desc",
            "itemsView": "summary",
        })
        self.assertEqual(params["baseInstructions"], "current persona")
        self.assertNotIn("config", params)

    async def test_turn_start_carries_stable_client_id_and_no_environment(self):
        transport = self.happy_transport()
        protocol = CodexGenerationProtocol(self.config(), transport)
        result = await protocol.start_turn(
            thread_id="thr-123",
            client_message_id="gen-123",
            text="hello",
            model="gpt-5.6-sol",
            reasoning_effort="high",
        )
        self.assertEqual(result.turn_id, "turn-123")
        method, params = transport.calls[-1]
        self.assertEqual(method, "turn/start")
        self.assertEqual(params["clientUserMessageId"], "gen-123")
        self.assertEqual(params["environments"], [])
        self.assertEqual(params["input"], [{"type": "text", "text": "hello"}])

    async def test_interrupt_and_unsubscribe_are_narrow(self):
        transport = self.happy_transport()
        protocol = CodexGenerationProtocol(self.config(), transport)
        await protocol.interrupt(thread_id="thr-123", turn_id="turn-123")
        await protocol.unsubscribe(thread_id="thr-123")
        self.assertEqual([c[0] for c in transport.calls], ["turn/interrupt", "thread/unsubscribe"])

    def test_digest_is_stable_without_persisting_plaintext(self):
        self.assertEqual(input_digest("hello"), input_digest("hello"))
        self.assertNotEqual(input_digest("hello"), input_digest("hello!"))

    def test_final_answer_matches_pinned_0147_agent_message_wire_shape(self):
        turn = {
            "items": [
                {"type": "agentMessage", "id": "a1", "text": "draft"},
                {"type": "agentMessage", "id": "a2", "phase": "finalAnswer", "text": "final"},
            ]
        }
        self.assertEqual(final_answer_from_turn(turn), "final")
        self.assertEqual(final_answer_from_turn({
            "items": [{"type": "agentMessage", "id": "a3", "text": "fallback"}]
        }), "fallback")
        self.assertIsNone(final_answer_from_turn({
            "items": [{"type": "agentMessage", "id": "legacy-shape", "content": [{"type": "text", "text": "must-not-match"}]}]
        }))

    def test_recovery_page_correlates_by_client_id_and_projects_final_answer(self):
        page = {
            "data": [
                {
                    "id": "turn-2",
                    "status": "completed",
                    "items": [
                        {"type": "userMessage", "id": "u2", "clientId": "gen-2", "content": []},
                        {"type": "agentMessage", "id": "a2", "phase": "finalAnswer", "text": "answer"},
                    ],
                },
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [{"type": "userMessage", "id": "u1", "clientId": "gen-1", "content": []}],
                },
            ]
        }
        found = correlated_turn_from_page(page, "gen-2")
        self.assertEqual((found.turn_id, found.status, found.final_answer), ("turn-2", "completed", "answer"))
        self.assertIsNone(correlated_turn_from_page(page, "gen-missing"))

    def test_notifications_are_minimal_and_error_text_is_dropped(self):
        event = project_notification("error", {
            "threadId": "thr-1",
            "turnId": "turn-1",
            "willRetry": False,
            "error": {
                "message": "PRIVATE-UPSTREAM-MESSAGE",
                "codexErrorInfo": "rateLimitExceeded",
                "additionalDetails": {"secret": "PRIVATE"},
            },
        })
        self.assertEqual(event.error_info, "rateLimitExceeded")
        self.assertTrue(event.terminal)
        self.assertNotIn("PRIVATE", repr(event))
        self.assertIsNone(project_notification("item/completed", {}))

    def test_usage_notification_projects_last_turn_only(self):
        event = project_notification("thread/tokenUsage/updated", {
            "threadId": "thr-1",
            "turnId": "turn-1",
            "tokenUsage": {
                "total": {"inputTokens": 999999},
                "last": {"inputTokens": 12, "outputTokens": 7, "cachedInputTokens": 3, "reasoningOutputTokens": 2},
            },
        })
        self.assertEqual(event.usage, {
            "input_tokens": 12,
            "output_tokens": 7,
            "cached_input_tokens": 3,
            "reasoning_output_tokens": 2,
        })

    async def test_activity_gate_blocks_control_during_generation(self):
        gate = CodexProcessActivityGate()
        async with gate.generation():
            self.assertTrue(gate.generation_active)
            with self.assertRaisesRegex(CodexGenerationError, "codex_generation_busy"):
                async with gate.control():
                    pass
        self.assertFalse(gate.generation_active)

    async def test_activity_gate_generation_waits_for_existing_control(self):
        gate = CodexProcessActivityGate()
        entered_generation = asyncio.Event()

        async def run_generation():
            async with gate.generation():
                entered_generation.set()

        async with gate.control():
            task = asyncio.create_task(run_generation())
            await asyncio.sleep(0)
            self.assertFalse(entered_generation.is_set())
        await task
        self.assertTrue(entered_generation.is_set())


if __name__ == "__main__":
    unittest.main()