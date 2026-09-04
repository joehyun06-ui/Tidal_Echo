from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from backend import provider_model_capability_probe as probe


URL = "https://provider.invalid/v1"
KEY = "invalid-server-key"
CURRENT_MODEL = "gpt-5.6-sol"


class ExplodingBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise AssertionError("alias visibility probe must not read provider body")
        yield b"unreachable"

    async def aclose(self):
        return None


def config_payload() -> dict:
    return {
        "history_n": 24,
        "main_chain": [{"url": URL, "key": KEY, "model": CURRENT_MODEL}],
        "sessions": [],
        "active_session": "",
    }


class ProviderGpt56AliasVisibilityProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "loop.json"
        self.path.write_text(json.dumps(config_payload()) + "\n", encoding="utf-8")

    async def _run(self, handler):
        real_client = httpx.AsyncClient

        def client_factory(**kwargs):
            return real_client(
                transport=httpx.MockTransport(handler),
                timeout=kwargs.get("timeout"),
                trust_env=kwargs.get("trust_env", True),
                follow_redirects=kwargs.get("follow_redirects", False),
            )

        with mock.patch.object(probe.httpx, "AsyncClient", new=client_factory):
            return await probe.probe_gpt56_alias_visibility(self.path)

    async def test_visible_alias_is_fixed_bodyless_get(self):
        requests = []

        def handler(request: httpx.Request):
            requests.append(request)
            return httpx.Response(200, stream=ExplodingBody())

        result = await self._run(handler)
        self.assertEqual(result, {
            "ok": True,
            "contract_version": probe.CONTRACT_VERSION,
            "probe": probe.ALIAS_PROBE_VERSION,
            "alias_model": "gpt-5.6",
            "capability": probe.ALIAS_VISIBLE,
            "provider_http_status": 200,
        })
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.path, "/v1/models/gpt-5.6")
        self.assertEqual(request.content, b"")
        rendered = repr(result)
        self.assertNotIn(KEY, rendered)
        self.assertNotIn(URL, rendered)
        self.assertNotIn(CURRENT_MODEL, rendered)

    async def test_404_is_alias_not_visible_without_catalog_fallback(self):
        requests = []

        def handler(request: httpx.Request):
            requests.append(request)
            return httpx.Response(404, stream=ExplodingBody())

        result = await self._run(handler)
        self.assertEqual(result["capability"], probe.ALIAS_NOT_VISIBLE)
        self.assertEqual(result["provider_http_status"], 404)
        self.assertEqual(len(requests), 1)

    async def test_other_statuses_use_existing_safe_classes(self):
        cases = {
            302: probe.PROBE_UNSUPPORTED,
            400: probe.EXPLICIT_REJECTION,
            401: probe.AUTH_REJECTED,
            403: probe.AUTH_REJECTED,
            405: probe.PROBE_UNSUPPORTED,
            408: probe.PROVIDER_TIMEOUT,
            429: probe.RATE_LIMITED,
            500: probe.UPSTREAM_UNAVAILABLE,
            501: probe.PROBE_UNSUPPORTED,
            503: probe.UPSTREAM_UNAVAILABLE,
        }
        for status, capability in cases.items():
            with self.subTest(status=status):
                def handler(_request: httpx.Request, status=status):
                    return httpx.Response(status, stream=ExplodingBody())

                result = await self._run(handler)
                self.assertEqual(result["capability"], capability)
                self.assertEqual(result["provider_http_status"], status)

    async def test_transport_failure_is_fixed(self):
        def handler(request: httpx.Request):
            raise httpx.ConnectError("transport failed", request=request)

        with self.assertRaises(probe.ProviderModelCapabilityProbeError) as raised:
            await self._run(handler)
        self.assertEqual(raised.exception.category, probe.UNAVAILABLE)
        self.assertEqual(raised.exception.status_code, 503)

    def test_p3_route_authenticates_before_body_read_and_probe(self):
        root = Path(__file__).resolve().parents[2]
        source = (root / "backend" / "p3_relay_app.py").read_text(encoding="utf-8")
        start = source.index('@app.post("/app/provider/gpt56-alias-visibility-probe")')
        end = source.index(
            'relay_app._P3_PROVIDER_GPT56_ALIAS_VISIBILITY_PROBE_INSTALLED = True',
            start,
        )
        block = source[start:end]
        self.assertLess(block.index("relay_app.check_auth(request)"), block.index("await request.body()"))
        self.assertLess(block.index("await request.body()"), block.index("probe_gpt56_alias_visibility"))
        for forbidden in ('"url"', '"key"', '"model"', '"messages"', '"prompt"'):
            self.assertNotIn(forbidden, block)


if __name__ == "__main__":
    unittest.main()
