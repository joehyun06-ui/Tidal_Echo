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


class ProviderErrorMetadataObservabilityTests(
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
                "LLM_MODEL": "gpt-5.6-sol",
                "LOOP_STREAM": "1",
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

    async def test_stream_503_logs_only_safe_error_identifiers(self):
        message_sentinel = "PRIVATE-UPSTREAM-MESSAGE"
        prompt_sentinel = "PRIVATE-PROMPT"

        def handler(_request):
            return httpx.Response(
                503,
                json={
                    "error": {
                        "code": "upstream_unavailable",
                        "type": "server_error",
                        "message": message_sentinel,
                    }
                },
            )

        captured = io.StringIO()
        with mock.patch.object(
            self.module.httpx,
            "AsyncClient",
            new=self._client_factory(handler),
        ), redirect_stderr(captured):
            result = await self.module.run_model(
                [{"role": "user", "content": prompt_sentinel}],
                stream_id="metadata-stream-test",
                emit_stream=True,
            )

        log = captured.getvalue()
        self.assertEqual(
            (result["outcome"], result["error"]),
            ("dispatch_uncertain", "provider_response_uncertain"),
        )
        self.assertIn("provider_http_status=503", log)
        self.assertIn("provider_error_code=upstream_unavailable", log)
        self.assertIn("provider_error_type=server_error", log)
        for private in (
            message_sentinel,
            prompt_sentinel,
            "PRIVATE-PROVIDER-KEY",
            "provider.private.invalid",
        ):
            self.assertNotIn(private, log)
        self.assertNotIn("provider_error_code", repr(result))
        self.assertNotIn("provider_error_type", repr(result))

    async def test_nonstream_503_logs_only_safe_error_identifiers(self):
        self.module.STREAM_OUTPUT = False
        message_sentinel = "PRIVATE-NONSTREAM-MESSAGE"

        def handler(_request):
            return httpx.Response(
                503,
                json={
                    "error": {
                        "code": "gateway_overloaded",
                        "type": "upstream_error",
                        "message": message_sentinel,
                    }
                },
            )

        captured = io.StringIO()
        with mock.patch.object(
            self.module.httpx,
            "AsyncClient",
            new=self._client_factory(handler),
        ), redirect_stderr(captured):
            result = await self.module.run_model(
                [{"role": "user", "content": "nonstream private prompt"}],
                emit_stream=False,
            )

        log = captured.getvalue()
        self.assertEqual(result["error"], "provider_response_uncertain")
        self.assertIn("provider_http_status=503", log)
        self.assertIn("provider_error_code=gateway_overloaded", log)
        self.assertIn("provider_error_type=upstream_error", log)
        self.assertNotIn(message_sentinel, log)
        self.assertNotIn("provider_error_code", repr(result))
        self.assertNotIn("provider_error_type", repr(result))

    async def test_unsafe_identifiers_are_dropped(self):
        private_code = "bad code\nPRIVATE-CODE"
        private_type = "server error"
        private_message = "PRIVATE-MESSAGE"

        def handler(_request):
            return httpx.Response(
                503,
                json={
                    "error": {
                        "code": private_code,
                        "type": private_type,
                        "message": private_message,
                    }
                },
            )

        captured = io.StringIO()
        with mock.patch.object(
            self.module.httpx,
            "AsyncClient",
            new=self._client_factory(handler),
        ), redirect_stderr(captured):
            result = await self.module.run_model(
                [{"role": "user", "content": "private prompt"}],
                stream_id="unsafe-metadata-test",
                emit_stream=True,
            )

        log = captured.getvalue()
        self.assertEqual(result["error"], "provider_response_uncertain")
        self.assertIn("provider_http_status=503", log)
        self.assertNotIn("provider_error_code=", log)
        self.assertNotIn("provider_error_type=", log)
        for private in ("PRIVATE-CODE", private_type, private_message):
            self.assertNotIn(private, log)

    async def test_oversized_stream_error_body_is_not_parsed(self):
        self.module.LOOP_PROVIDER_RESPONSE_MAX_BYTES = 64
        safe_code = "should_not_escape"
        private_message = "PRIVATE-OVERSIZED-" + ("x" * 256)

        def handler(_request):
            return httpx.Response(
                503,
                json={
                    "error": {
                        "code": safe_code,
                        "type": "server_error",
                        "message": private_message,
                    }
                },
            )

        captured = io.StringIO()
        with mock.patch.object(
            self.module.httpx,
            "AsyncClient",
            new=self._client_factory(handler),
        ), redirect_stderr(captured):
            result = await self.module.run_model(
                [{"role": "user", "content": "oversized error prompt"}],
                stream_id="oversized-metadata-test",
                emit_stream=True,
            )

        log = captured.getvalue()
        self.assertEqual(result["error"], "provider_response_uncertain")
        self.assertIn("provider_http_status=503", log)
        self.assertNotIn("provider_error_code=", log)
        self.assertNotIn("provider_error_type=", log)
        self.assertNotIn(safe_code, log)
        self.assertNotIn("PRIVATE-OVERSIZED", log)


if __name__ == "__main__":
    unittest.main()
