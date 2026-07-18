from __future__ import annotations

import asyncio
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import httpx

from backend.tests._support import NoNetworkMixin, request


class PartialThenTimeout(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise httpx.ReadTimeout("test timeout")


class ApiLoopReliabilityTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        db_path = root / "relay.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("""CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, direction TEXT,
                kind TEXT, text TEXT, meta TEXT)""")
            conn.commit()
        os.environ.update({
            "RELAY_DB": str(db_path), "LOOP_CONFIG": str(root / "loop.json"),
            "RELAY_SECRET": "invalid-test-relay-secret", "RELAY_URL": "http://invalid.test",
            "LLM_API_BASE": "https://model-one.invalid/v1", "LLM_API_KEY": "invalid-key-one",
            "LLM_MODEL": "model-one", "LLM_API_BASE_2": "https://model-two.invalid/v1",
            "LLM_API_KEY_2": "invalid-key-two", "LLM_MODEL_2": "model-two",
            "LOOP_STREAM": "1", "LOOP_MODEL_TOTAL_TIMEOUT_SECONDS": "0.08",
            "LOOP_CALLBACK_TIMEOUT_SECONDS": "0.02", "LOOP_TIMEOUT_SAFETY_MARGIN_SECONDS": "0.01",
            "LOOP_DISPATCH_TIMEOUT_SECONDS": "0.12",
        })
        sys.modules.pop("examples.api_loop", None)
        self.module = importlib.import_module("examples.api_loop")

    async def test_partial_delta_then_timeout_never_falls_back(self):
        calls = []
        real_client = httpx.AsyncClient
        def handler(req):
            calls.append(json.loads(req.content)["model"])
            if len(calls) == 1:
                return httpx.Response(200, stream=PartialThenTimeout())
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"second"}}]}\n\ndata: [DONE]\n\n')
        def client_factory(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))
        with mock.patch.object(self.module.httpx, "AsyncClient", new=client_factory), \
             mock.patch.object(self.module, "relay_out", new=mock.AsyncMock(return_value=(True, {"id": 1}, False))):
            result = await self.module.run_model([{"role": "user", "content": "x"}], stream_id="s", emit_stream=True)
        self.assertEqual(result["outcome"], "dispatch_uncertain")
        self.assertEqual(calls, ["model-one"])

    async def test_request_sent_then_disconnect_never_falls_back(self):
        calls = []
        real_client = httpx.AsyncClient
        def handler(req):
            calls.append(json.loads(req.content)["model"])
            raise httpx.ReadError("disconnected")
        def client_factory(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))
        with mock.patch.object(self.module.httpx, "AsyncClient", new=client_factory):
            result = await self.module.run_model([{"role": "user", "content": "x"}], emit_stream=False)
        self.assertEqual(result["outcome"], "dispatch_uncertain")
        self.assertEqual(calls, ["model-one"])

    async def test_generic_exception_never_falls_back(self):
        calls = []
        async def broken(route, messages):
            calls.append(route["model"])
            raise RuntimeError("generic")
        with mock.patch.object(self.module, "complete_chat", new=broken):
            result = await self.module.run_model([], emit_stream=False)
        self.assertEqual(result["outcome"], "dispatch_uncertain")
        self.assertEqual(calls, ["model-one"])

    async def test_safe_preflight_rejection_may_fallback_once(self):
        calls = []
        real_client = httpx.AsyncClient
        def handler(req):
            model = json.loads(req.content)["model"]
            calls.append(model)
            if model == "model-one":
                return httpx.Response(404, json={"error": {"code": "model_not_found"}})
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        def client_factory(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))
        with mock.patch.object(self.module.httpx, "AsyncClient", new=client_factory):
            result = await self.module.run_model([], emit_stream=False)
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(calls, ["model-one", "model-two"])

    async def test_total_deadline_shared_across_routes(self):
        calls = []
        # Leave enough headroom for asyncio debug mode on slower Windows CI;
        # the second route still blocks forever and must consume the shared deadline.
        self.module.LOOP_MODEL_TOTAL_TIMEOUT_SECONDS = 0.2
        async def slow_safe(route, messages):
            calls.append(route["model"])
            if len(calls) == 1:
                await asyncio.sleep(0.005)
                raise self.module.ModelRouteError("model_unsupported", "safe_to_fallback")
            await asyncio.Future()
            raise self.module.ModelRouteError("model_unsupported", "safe_to_fallback")
        with mock.patch.object(self.module, "complete_chat", new=slow_safe):
            result = await self.module.run_model([], emit_stream=False)
        self.assertEqual(result["outcome"], "dispatch_uncertain")
        self.assertEqual(result["error"], "model_timeout")
        self.assertEqual(calls, ["model-one", "model-two"])

    async def test_cancellation_stops_model_stream(self):
        stopped = asyncio.Event()
        self.module.LOOP_MODEL_TOTAL_TIMEOUT_SECONDS = 0.02
        async def never_finishes(route, messages, sink):
            try:
                await asyncio.Future()
            finally:
                stopped.set()
        callback = mock.AsyncMock(return_value=(True, {"id": 1}, False))
        with mock.patch.object(self.module, "stream_chat", new=never_finishes), \
             mock.patch.object(self.module, "relay_out", new=callback):
            result = await self.module.run_model([], stream_id="s", emit_stream=True)
        self.assertEqual(result["error"], "model_timeout")
        self.assertTrue(stopped.is_set())
        callback.assert_not_awaited()

    async def test_ingest_callback_failure_and_timeout_are_non_success(self):
        success = {"outcome": "success", "text": "answer", "model": "m", "tried": [], "usage": {}}
        payload = {"text": "hello", "api_session": "a", "generation_id": "g", "stream_id": "s",
                   "reply_to": "1", "channel": "telegram", "channel_account": "bot",
                   "channel_conversation": "chat"}
        with mock.patch.object(self.module, "run_model", new=mock.AsyncMock(return_value=success)), \
             mock.patch.object(self.module, "relay_out", new=mock.AsyncMock(return_value=(False, {"error": "rejected"}, False))):
            failed = await request(self.module, "POST", "/loop/ingest", json=payload)
        self.assertEqual(failed.status_code, 502)
        self.assertFalse(failed.json()["callback_delivered"])
        with mock.patch.object(self.module, "run_model", new=mock.AsyncMock(return_value=success)), \
             mock.patch.object(self.module, "relay_out", new=mock.AsyncMock(return_value=(False, {"error": "timeout"}, True))):
            uncertain = await request(self.module, "POST", "/loop/ingest", json=payload)
        self.assertEqual(uncertain.status_code, 504)
        self.assertTrue(uncertain.json()["dispatch_uncertain"])


if __name__ == "__main__":
    unittest.main()
