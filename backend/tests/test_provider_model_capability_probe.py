from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from backend import provider_model_capability_probe as probe


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


def config_payload(model: str = MODEL) -> dict:
    return {
        "history_n": 24,
        "main_chain": [
            {"url": URL, "key": KEY, "model": model},
        ],
        "sessions": [],
        "active_session": "",
    }


class ProviderModelCapabilityProbeTests(unittest.IsolatedAsyncioTestCase):
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
            return await probe.probe_authoritative_primary_model(self.path)

    async def test_exact_model_visible_uses_bodyless_get_and_returns_only_safe_fields(self):
        requests = []

        def handler(request: httpx.Request):
            requests.append(request)
            return httpx.Response(200, stream=ExplodingBody())

        result = await self._run_with_handler(handler)

        self.assertEqual(result, {
            "ok": True,
            "contract_version": probe.CONTRACT_VERSION,
            "probe": probe.PROBE_VERSION,
            "model": MODEL,
            "capability": probe.MODEL_VISIBLE,
            "provider_http_status": 200,
        })
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(requests[0].content, b"")
        self.assertEqual(requests[0].url.path, "/v1/models/gpt-5.6-sol")
        self.assertEqual(requests[0].headers["authorization"], f"Bearer {KEY}")
        rendered = repr(result)
        self.assertNotIn(KEY, rendered)
        self.assertNotIn(URL, rendered)
        self.assertNotIn(SECRET_BODY, rendered)

    async def test_model_404_then_catalog_200_distinguishes_not_visible(self):
        paths = []

        def handler(request: httpx.Request):
            paths.append(request.url.path)
            if len(paths) == 1:
                return httpx.Response(404, stream=ExplodingBody())
            return httpx.Response(200, stream=ExplodingBody())

        result = await self._run_with_handler(handler)

        self.assertEqual(paths, [
            "/v1/models/gpt-5.6-sol",
            "/v1/models",
        ])
        self.assertEqual(result["capability"], probe.MODEL_NOT_VISIBLE)
        self.assertEqual(result["provider_http_status"], 404)
        self.assertEqual(result["catalog_http_status"], 200)

    async def test_model_404_and_catalog_404_is_probe_unsupported(self):
        async def _unused():
            return None

        def handler(_request: httpx.Request):
            return httpx.Response(404, stream=ExplodingBody())

        result = await self._run_with_handler(handler)
        self.assertEqual(result["capability"], probe.PROBE_UNSUPPORTED)
        self.assertEqual(result["provider_http_status"], 404)
        self.assertEqual(result["catalog_http_status"], 404)

    async def test_http_status_classes_are_data_free_and_exact(self):
        cases = {
            401: probe.AUTH_REJECTED,
            403: probe.AUTH_REJECTED,
            408: probe.PROVIDER_TIMEOUT,
            429: probe.RATE_LIMITED,
            503: probe.UPSTREAM_UNAVAILABLE,
            405: probe.PROBE_UNSUPPORTED,
            501: probe.PROBE_UNSUPPORTED,
            422: probe.EXPLICIT_REJECTION,
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

    async def test_transport_failure_returns_fixed_data_free_error_without_fake_status(self):
        def handler(request: httpx.Request):
            raise httpx.ConnectError(
                f"transport failed {KEY} {SECRET_BODY}",
                request=request,
            )

        with self.assertRaises(probe.ProviderModelCapabilityProbeError) as raised:
            await self._run_with_handler(handler)
        self.assertEqual(raised.exception.category, probe.UNAVAILABLE)
        self.assertEqual(raised.exception.status_code, 503)
        rendered = repr(raised.exception)
        self.assertNotIn(KEY, rendered)
        self.assertNotIn(SECRET_BODY, rendered)
        self.assertNotIn("provider_http_status", rendered)

    async def test_model_path_is_percent_encoded_and_never_supplied_by_browser(self):
        self.path.write_text(
            json.dumps(config_payload("gpt/test model"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        observed = []

        def handler(request: httpx.Request):
            observed.append(request.url.raw_path.decode("ascii"))
            return httpx.Response(200, stream=ExplodingBody())

        result = await self._run_with_handler(handler)
        self.assertEqual(result["model"], "gpt/test model")
        self.assertEqual(observed, ["/v1/models/gpt%2Ftest%20model"])

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
                probe.ProviderModelCapabilityProbeError
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
        start = source.index('@app.post("/app/provider/model-capability-probe")')
        end = source.index(
            'relay_app._P3_PROVIDER_MODEL_CAPABILITY_PROBE_INSTALLED = True',
            start,
        )
        block = source[start:end]
        auth_index = block.index("relay_app.check_auth(request)")
        body_index = block.index("await request.body()")
        probe_index = block.index("probe_authoritative_primary_model")
        self.assertLess(auth_index, body_index)
        self.assertLess(body_index, probe_index)
        self.assertNotIn('"url"', block)
        self.assertNotIn('"key"', block)
        self.assertNotIn('"model"', block)


if __name__ == "__main__":
    unittest.main()
