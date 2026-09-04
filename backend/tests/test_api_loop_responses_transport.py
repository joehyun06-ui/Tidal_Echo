from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from backend.tests._support import NoNetworkMixin


class ApiLoopResponsesTransportTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        env = {
            "LOOP_CONFIG": str(root / "loop.json"),
            "RELAY_DB": str(root / "relay.sqlite3"),
            "RELAY_SECRET": "invalid-test-relay-secret",
            "RELAY_URL": "http://invalid.test",
            "LLM_API_BASE": "https://provider.invalid/v1",
            "LLM_API_KEY": "invalid-key",
            "LLM_MODEL": "gpt-5.6-sol",
            "LLM_MAX_TOKENS": "2000",
            "LLM_TEMPERATURE": "0.7",
            "LOOP_STREAM": "1",
            "API_LOOP_INTERNAL_TOKEN": "test-internal-loop-token-1234567890",
            "CODEX_CONTROL_ENABLED": "false",
            "RENDER_TELEGRAM_MVP": "false",
        }
        self.env_patch = mock.patch.dict(os.environ, env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        sys.modules.pop("examples.api_loop", None)
        self.addCleanup(lambda: sys.modules.pop("examples.api_loop", None))
        self.module = importlib.import_module("examples.api_loop")
        self.messages = [
            {"role": "system", "content": "persona"},
            {"role": "assistant", "content": "old"},
            {"role": "user", "content": "current"},
        ]

    def _client_factory(self, handler):
        real_client = httpx.AsyncClient

        def factory(**kwargs):
            return real_client(
                transport=httpx.MockTransport(handler),
                timeout=kwargs.get("timeout"),
            )

        return factory

    async def test_gpt56_nonstream_routes_to_responses_with_stateless_body(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "id": "resp_test",
                "object": "response",
                "status": "completed",
                "output": [{
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "ok", "annotations": []}],
                }],
                "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
            })

        with mock.patch.object(
            self.module,
            "_provider_client",
            side_effect=lambda **kwargs: self._client_factory(handler)(**kwargs),
        ):
            out = await self.module.run_model(self.messages, emit_stream=False)

        self.assertEqual(out["outcome"], "success")
        self.assertEqual(out["text"], "ok")
        self.assertEqual(seen["path"], "/v1/responses")
        self.assertEqual(seen["body"], {
            "model": "gpt-5.6-sol",
            "input": self.messages,
            "max_output_tokens": 2000,
            "stream": False,
            "store": False,
        })
        self.assertNotIn("temperature", seen["body"])
        self.assertNotIn("messages", seen["body"])
        self.assertNotIn("max_tokens", seen["body"])
        self.assertNotIn("max_completion_tokens", seen["body"])

    async def test_gpt56_stream_routes_to_responses_and_emits_text_deltas(self):
        seen = {}
        emitted = []

        def handler(request):
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            payload = (
                'event: response.created\n'
                'data: {"type":"response.created","response":{"status":"in_progress"}}\n\n'
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"SMOKE_"}\n\n'
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"OK_56"}\n\n'
                'event: response.completed\n'
                'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":3,"output_tokens":2,"total_tokens":5}}}\n\n'
            )
            return httpx.Response(200, text=payload)

        async def relay_stub(payload):
            if payload.get("type") == "reply_delta" and payload.get("done") is False:
                emitted.append(payload.get("text"))
            return True, {"id": 1}, False

        with mock.patch.object(
            self.module.httpx,
            "AsyncClient",
            new=self._client_factory(handler),
        ), mock.patch.object(self.module, "relay_out", new=relay_stub):
            out = await self.module.run_model(
                self.messages,
                stream_id="responses-test",
                session_id="api-test",
                emit_stream=True,
            )

        self.assertEqual(out["outcome"], "success")
        self.assertEqual(out["text"], "SMOKE_OK_56")
        self.assertEqual(emitted, ["SMOKE_", "OK_56"])
        self.assertEqual(seen["path"], "/v1/responses")
        self.assertTrue(seen["body"]["stream"])
        self.assertFalse(seen["body"]["store"])
        self.assertEqual(seen["body"]["max_output_tokens"], 2000)

    async def test_kelivo_gpt56_maps_internal_budget_to_responses_without_temperature(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "id": "resp_kelivo",
                "object": "response",
                "status": "completed",
                "output_text": "[]",
                "output": [],
                "usage": {},
            })

        with mock.patch.object(
            self.module,
            "_provider_client",
            side_effect=lambda **kwargs: self._client_factory(handler)(**kwargs),
        ), mock.patch.object(
            self.module.deployment_config,
            "resolve_kelivo_provider_contract_defaults",
            return_value=mock.Mock(provider_model="gpt-5.6-sol"),
        ):
            out = await self.module.run_kelivo_provider_contract(
                "gpt-5.6-sol",
                [{"role": "developer", "content": "extract"}, {"role": "user", "content": "source"}],
                temperature=0.0,
                max_tokens=321,
            )

        self.assertEqual(out["outcome"], "success")
        self.assertEqual(out["text"], "[]")
        self.assertEqual(seen["path"], "/v1/responses")
        self.assertEqual(seen["body"]["max_output_tokens"], 321)
        self.assertNotIn("temperature", seen["body"])

    async def test_legacy_model_still_uses_chat_completions(self):
        self.module.main_chain = lambda: [{
            "url": "https://provider.invalid/v1",
            "key": "invalid-key",
            "model": "legacy-model",
        }]
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "legacy ok"}}],
                "usage": {},
            })

        with mock.patch.object(
            self.module,
            "_provider_client",
            side_effect=lambda **kwargs: self._client_factory(handler)(**kwargs),
        ):
            out = await self.module.run_model(self.messages, emit_stream=False)

        self.assertEqual(out["outcome"], "success")
        self.assertEqual(out["text"], "legacy ok")
        self.assertEqual(seen["path"], "/v1/chat/completions")
        self.assertIn("messages", seen["body"])
        self.assertNotIn("input", seen["body"])

    async def test_responses_failed_terminal_event_never_returns_partial_success(self):
        emitted = []

        def handler(_request):
            payload = (
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
                'event: response.failed\n'
                'data: {"type":"response.failed","response":{"status":"failed","error":{"code":"server_error","message":"PRIVATE"}}}\n\n'
            )
            return httpx.Response(200, text=payload)

        async def relay_stub(payload):
            if payload.get("type") == "reply_delta" and payload.get("done") is False:
                emitted.append(payload.get("text"))
            return True, {"id": 1}, False

        with mock.patch.object(
            self.module.httpx,
            "AsyncClient",
            new=self._client_factory(handler),
        ), mock.patch.object(self.module, "relay_out", new=relay_stub):
            out = await self.module.run_model(
                self.messages,
                stream_id="responses-failed-test",
                emit_stream=True,
            )

        self.assertEqual(out["outcome"], "dispatch_uncertain")
        self.assertEqual(out["error"], "provider_response_uncertain")
        self.assertEqual(emitted, ["partial"])


if __name__ == "__main__":
    unittest.main()
