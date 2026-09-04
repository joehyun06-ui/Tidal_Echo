from __future__ import annotations

import importlib
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import httpx

from backend.tests._support import NoNetworkMixin


class ProviderHttpStatusObservabilityTests(
    NoNetworkMixin,
    unittest.IsolatedAsyncioTestCase,
):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.env = mock.patch.dict(
            os.environ,
            {
                "LOOP_CONFIG": str(root / "loop.json"),
                "RELAY_DB": str(root / "relay.db"),
                "RELAY_SECRET": "PRIVATE-RELAY-SECRET",
                "RELAY_URL": "http://relay.private.invalid",
                "LLM_API_BASE": "https://provider.private.invalid/v1",
                "LLM_API_KEY": "PRIVATE-PROVIDER-KEY",
                "LLM_MODEL": "model-one",
                "LOOP_STREAM": "0",
                "RENDER_TELEGRAM_MVP": "false",
                "API_LOOP_INTERNAL_TOKEN": "test-internal-loop-token-1234567890",
                "CODEX_CONTROL_ENABLED": "false",
            },
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        sys.modules.pop("examples.api_loop", None)
        self.module = importlib.import_module("examples.api_loop")
        self.addCleanup(sys.modules.pop, "examples.api_loop", None)

    def _client_factory(self, handler):
        real_client = httpx.AsyncClient

        def factory(**kwargs):
            return real_client(
                transport=httpx.MockTransport(handler),
                timeout=kwargs.get("timeout"),
            )

        return factory

    async def test_429_logs_exact_status_without_provider_data(self):
        body_sentinel = "PRIVATE-UPSTREAM-BODY"
        prompt_sentinel = "PRIVATE-PROMPT"

        def handler(_request):
            return httpx.Response(
                429,
                json={"error": {"message": body_sentinel}},
            )

        captured = io.StringIO()
        with mock.patch.object(
            self.module.httpx,
            "AsyncClient",
            new=self._client_factory(handler),
        ), redirect_stderr(captured):
            result = await self.module.run_model(
                [{"role": "user", "content": prompt_sentinel}],
                emit_stream=False,
            )

        log = captured.getvalue()
        self.assertEqual(
            (result["outcome"], result["error"]),
            ("dispatch_uncertain", "provider_response_uncertain"),
        )
        self.assertIn(
            "model_dispatch=provider_response_uncertain provider_http_status=429",
            log,
        )
        for private in (
            body_sentinel,
            prompt_sentinel,
            "PRIVATE-PROVIDER-KEY",
            "provider.private.invalid",
        ):
            self.assertNotIn(private, log)
        self.assertNotIn("provider_http_status", repr(result))

    async def test_stream_503_logs_exact_status_without_response_body(self):
        self.module.STREAM_OUTPUT = True
        body_sentinel = "PRIVATE-STREAM-BODY"

        def handler(_request):
            return httpx.Response(
                503,
                json={"error": {"message": body_sentinel}},
            )

        captured = io.StringIO()
        with mock.patch.object(
            self.module.httpx,
            "AsyncClient",
            new=self._client_factory(handler),
        ), redirect_stderr(captured):
            result = await self.module.run_model(
                [{"role": "user", "content": "stream probe"}],
                stream_id="status-test",
                emit_stream=True,
            )

        log = captured.getvalue()
        self.assertEqual(result["error"], "provider_response_uncertain")
        self.assertIn("provider_http_status=503", log)
        self.assertNotIn(body_sentinel, log)
        self.assertNotIn("PRIVATE-PROVIDER-KEY", log)

    async def test_kelivo_contract_logs_408_but_does_not_return_status(self):
        def handler(_request):
            return httpx.Response(408, text="PRIVATE-408-BODY")

        captured = io.StringIO()
        with mock.patch.object(
            self.module.httpx,
            "AsyncClient",
            new=self._client_factory(handler),
        ), redirect_stderr(captured):
            result = await self.module.run_kelivo_provider_contract(
                "model-one",
                [{"role": "user", "content": "private kelivo prompt"}],
                temperature=0.4,
                max_tokens=64,
            )

        log = captured.getvalue()
        self.assertEqual(
            (result["outcome"], result["error"]),
            ("dispatch_uncertain", "provider_response_uncertain"),
        )
        self.assertIn("provider_http_status=408", log)
        self.assertNotIn("provider_http_status", repr(result))
        self.assertNotIn("PRIVATE-408-BODY", log)

    async def test_transport_failure_never_invents_provider_status(self):
        def handler(_request):
            raise httpx.ReadError("PRIVATE-TRANSPORT-DETAIL")

        captured = io.StringIO()
        with mock.patch.object(
            self.module.httpx,
            "AsyncClient",
            new=self._client_factory(handler),
        ), redirect_stderr(captured):
            result = await self.module.run_model(
                [{"role": "user", "content": "transport probe"}],
                emit_stream=False,
            )

        log = captured.getvalue()
        self.assertEqual(result["error"], "model_transport_uncertain")
        self.assertNotIn("provider_http_status=", log)
        self.assertNotIn("PRIVATE-TRANSPORT-DETAIL", log)


if __name__ == "__main__":
    unittest.main()
