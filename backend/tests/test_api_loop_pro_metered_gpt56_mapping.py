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


MODEL = "[Pro按量]gpt-5.6-sol"
BASE = "https://provider.invalid/v1"
KEY = "invalid-key"


class ApiLoopProMeteredGpt56MappingTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        env = {
            "LOOP_CONFIG": str(root / "loop.json"),
            "RELAY_DB": str(root / "relay.sqlite3"),
            "RELAY_SECRET": "invalid-test-relay-secret",
            "RELAY_URL": "http://invalid.test",
            "LLM_API_BASE": BASE,
            "LLM_API_KEY": KEY,
            "LLM_MODEL": MODEL,
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

    async def test_nonstream_routes_exact_display_model_to_chat_completions(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {},
            })

        with mock.patch.object(
            self.module,
            "_provider_client",
            side_effect=lambda **kwargs: self._client_factory(handler)(**kwargs),
        ):
            out = await self.module.run_model(self.messages, emit_stream=False)

        self.assertEqual(out["outcome"], "success")
        self.assertEqual(out["model"], MODEL)
        self.assertEqual(seen["path"], "/v1/chat/completions")
        self.assertEqual(seen["body"], {
            "model": MODEL,
            "messages": self.messages,
            "max_completion_tokens": 2000,
            "stream": False,
        })
        self.assertNotIn("temperature", seen["body"])
        self.assertNotIn("max_tokens", seen["body"])
        self.assertNotIn("input", seen["body"])
        self.assertNotIn("max_output_tokens", seen["body"])
        self.assertFalse(self.module._uses_responses_api({"model": MODEL}))

    async def test_stream_routes_exact_display_model_to_chat_completions(self):
        seen = {}
        emitted = []

        def handler(request: httpx.Request):
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            payload = (
                'data: {"choices":[{"delta":{"content":"SMOKE_"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n'
                'data: [DONE]\n\n'
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
                stream_id="pro-metered-gpt56-test",
                session_id="api-test",
                emit_stream=True,
            )

        self.assertEqual(out["outcome"], "success")
        self.assertEqual(out["text"], "SMOKE_OK")
        self.assertEqual(emitted, ["SMOKE_", "OK"])
        self.assertEqual(seen["path"], "/v1/chat/completions")
        self.assertEqual(seen["body"]["model"], MODEL)
        self.assertEqual(seen["body"]["max_completion_tokens"], 2000)
        self.assertTrue(seen["body"]["stream"])
        self.assertNotIn("temperature", seen["body"])
        self.assertNotIn("max_tokens", seen["body"])

    async def test_kelivo_budget_uses_chat_completion_compat_payload(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "[]"}}],
                "usage": {},
            })

        with mock.patch.object(
            self.module,
            "_provider_client",
            side_effect=lambda **kwargs: self._client_factory(handler)(**kwargs),
        ), mock.patch.object(
            self.module.deployment_config,
            "resolve_kelivo_provider_contract_defaults",
            return_value=mock.Mock(provider_model=MODEL),
        ):
            out = await self.module.run_kelivo_provider_contract(
                MODEL,
                [
                    {"role": "developer", "content": "extract"},
                    {"role": "user", "content": "source"},
                ],
                temperature=0.0,
                max_tokens=321,
            )

        self.assertEqual(out["outcome"], "success")
        self.assertEqual(out["text"], "[]")
        self.assertEqual(seen["path"], "/v1/chat/completions")
        self.assertEqual(seen["body"]["model"], MODEL)
        self.assertEqual(seen["body"]["max_completion_tokens"], 321)
        self.assertNotIn("temperature", seen["body"])
        self.assertNotIn("max_tokens", seen["body"])

    def test_compatibility_match_is_exact(self):
        body = self.module._chat_completion_body(
            {"model": "[Pro按量]gpt-5.6-sol-preview"},
            self.messages,
            stream=False,
        )
        self.assertEqual(body["temperature"], 0.7)
        self.assertEqual(body["max_tokens"], 2000)
        self.assertNotIn("max_completion_tokens", body)


if __name__ == "__main__":
    unittest.main()
