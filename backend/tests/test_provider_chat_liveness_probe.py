from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from backend import provider_chat_liveness_probe as probe


URL = "https://provider.invalid/v1"
KEY = "server-secret-never-returned"
MODEL = "gpt-5.6-sol"
SECRET_BODY = "UPSTREAM-BODY-MUST-NEVER-BE-READ-OR-RETURNED"


class ExplodingBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise AssertionError("provider response body must not be read")
        yield b"unreachable"

    async def aclose(self):
        return None


def config_payload() -> dict:
    return {
        "history_n": 24,
        "main_chain": [
            {"url": URL, "key": KEY, "model": MODEL},
        ],
        "sessions": [],
        "active_session": "",
    }


class ProviderChatLivenessProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "loop.json"
        self.path.write_text(
            json.dumps(config_payload(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    async def _run_with_handler(self, handler):
        real_client = httpx.AsyncClient

        def client_factory(**kwargs):
            return real_client(
                transport=httpx.MockTransport(handler),
                timeout=kwargs.get("timeout"),
                trust_env=kwargs.get("trust_env", True),
                follow_redirects=kwargs.get("follow_redirects", False),
            )

        with mock.patch.object(probe.httpx, "AsyncClient", new=client_factory):
            return await probe.probe_authoritative_chat_endpoint(self.path)

    async def test_validation_reachable_sends_only_fixed_empty_object_and_reads_no_body(self):
        requests = []

        def handler(request: httpx.Request):
            requests.append(request)
            return httpx.Response(
                400,
                stream=ExplodingBody(),
                headers={"X-Secret-Upstream": SECRET_BODY},
            )

        result = await self._run_with_handler(handler)

        self.assertEqual(result, {
            "ok": True,
            "contract_version": probe.CONTRACT_VERSION,
            "probe": probe.PROBE_VERSION,
            "configured_model": MODEL,
            "capability": probe.ENDPOINT_VALIDATING,
            "provider_http_status": 400,
        })
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/v1/chat/completions")
        self.assertEqual(request.content, b"{}")
        self.assertEqual(json.loads(request.content), {})
        self.assertEqual(request.headers["authorization"], f"Bearer {KEY}")
        self.assertEqual(request.headers["content-type"], "application/json")
        rendered_request = request.content.decode("utf-8")
        for forbidden in ("model", "messages", "prompt", MODEL):
            self.assertNotIn(forbidden, rendered_request)
        rendered_result = repr(result)
        self.assertNotIn(KEY, rendered_result)
        self.assertNotIn(URL, rendered_result)
        self.assertNotIn(SECRET_BODY, rendered_result)

    async def test_422_is_also_endpoint_validating(self):
        def handler(_request: httpx.Request):
            return httpx.Response(422, stream=ExplodingBody())

        result = await self._run_with_handler(handler)
        self.assertEqual(result["capability"], probe.ENDPOINT_VALIDATING)
        self.assertEqual(result["provider_http_status"], 422)

    async def test_http_status_classes_are_exact_and_data_free(self):
        cases = {
            200: probe.UNEXPECTED_ACCEPTANCE,
            302: probe.ENDPOINT_REDIRECTED,
            401: probe.AUTH_REJECTED,
            403: probe.AUTH_REJECTED,
            404: probe.ENDPOINT_UNSUPPORTED,
            405: probe.ENDPOINT_UNSUPPORTED,
            408: probe.PROVIDER_TIMEOUT,
            409: probe.EXPLICIT_REJECTION,
            429: probe.RATE_LIMITED,
            500: probe.UPSTREAM_UNAVAILABLE,
            503: probe.UPSTREAM_UNAVAILABLE,
            501: probe.UPSTREAM_UNAVAILABLE,
        }
        for status, capability in cases.items():
            with self.subTest(status=status):
                def handler(_request: httpx.Request, status=status):
                    return httpx.Response(
                        status,
                        stream=ExplodingBody(),
                        headers={"X-Secret-Upstream": SECRET_BODY},
                    )

                result = await self._run_with_handler(handler)
                self.assertEqual(result["provider_http_status"], status)
                self.assertEqual(result["capability"], capability)
                rendered = repr(result)
                self.assertNotIn(SECRET_BODY, rendered)
                self.assertNotIn(KEY, rendered)
                self.assertNotIn(URL, rendered)

    async def test_transport_failure_returns_fixed_error_without_fake_status(self):
        def handler(request: httpx.Request):
            raise httpx.ConnectError(
                f"transport failed {KEY} {SECRET_BODY}",
                request=request,
            )

        with self.assertRaises(probe.ProviderChatLivenessProbeError) as raised:
            await self._run_with_handler(handler)
        self.assertEqual(raised.exception.category, probe.UNAVAILABLE)
        self.assertEqual(raised.exception.status_code, 503)
        rendered = repr(raised.exception)
        self.assertNotIn(KEY, rendered)
        self.assertNotIn(SECRET_BODY, rendered)
        self.assertNotIn("provider_http_status", rendered)

    def test_probe_request_is_strictly_empty_and_identity_only(self):
        for raw, length, encoding in (
            (b"{}", None, ""),
            (b"x", "1", ""),
            (b"", "1", ""),
            (b"", "bad", ""),
            (b"", None, "gzip"),
            ("", None, ""),
        ):
            with self.subTest(raw=raw, length=length, encoding=encoding), self.assertRaises(
                probe.ProviderChatLivenessProbeError
            ) as raised:
                probe.validate_empty_probe_request(
                    raw,
                    content_length=length,
                    content_encoding=encoding,
                )
            self.assertEqual(raised.exception.category, probe.INVALID_REQUEST)
            self.assertEqual(raised.exception.status_code, 400)

        self.assertIsNone(probe.validate_empty_probe_request(b""))
        self.assertIsNone(probe.validate_empty_probe_request(b"", content_length="0"))

    def test_p3_route_authenticates_before_body_read_and_probe(self):
        root = Path(__file__).resolve().parents[2]
        source = (root / "backend" / "p3_relay_app.py").read_text(encoding="utf-8")
        start = source.index('@app.post("/app/provider/chat-liveness-probe")')
        end = source.index(
            'relay_app._P3_PROVIDER_CHAT_LIVENESS_PROBE_INSTALLED = True',
            start,
        )
        block = source[start:end]
        auth_index = block.index("relay_app.check_auth(request)")
        body_index = block.index("await request.body()")
        probe_index = block.index("probe_authoritative_chat_endpoint")
        self.assertLess(auth_index, body_index)
        self.assertLess(body_index, probe_index)
        for forbidden in ('"url"', '"key"', '"model"', '"messages"', '"prompt"'):
            self.assertNotIn(forbidden, block)


if __name__ == "__main__":
    unittest.main()
