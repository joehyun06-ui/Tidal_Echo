from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import provider_route_classification_probe as probe


KEY = "server-secret-never-returned"
MODEL = "gpt-5.6-sol"


def config_payload(url: str) -> dict:
    return {
        "history_n": 24,
        "main_chain": [
            {"url": url, "key": KEY, "model": MODEL},
        ],
        "sessions": [],
        "active_session": "",
    }


class ProviderRouteClassificationProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "loop.json"

    def _write(self, url: str) -> None:
        self.path.write_text(
            json.dumps(config_payload(url), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_exact_official_openai_routes_classify_official(self):
        for url in (
            "https://api.openai.com/v1",
            "https://api.openai.com/v1/",
            "https://api.openai.com:443/v1",
            "https://API.OPENAI.COM/v1",
        ):
            with self.subTest(url=url):
                self._write(url)
                result = probe.classify_authoritative_provider_route(self.path)
                self.assertEqual(result, {
                    "ok": True,
                    "contract_version": probe.CONTRACT_VERSION,
                    "probe": probe.PROBE_VERSION,
                    "classification": probe.OFFICIAL_OPENAI,
                })
                rendered = repr(result)
                self.assertNotIn(KEY, rendered)
                self.assertNotIn(url, rendered)
                self.assertNotIn(MODEL, rendered)

    def test_spoofed_or_noncanonical_routes_never_classify_official(self):
        cases = (
            "http://api.openai.com/v1",
            "https://api.openai.com.evil.invalid/v1",
            "https://openai.com/v1",
            "https://api.openai.com/v1/responses",
            "https://api.openai.com/v1?proxy=1",
            "https://api.openai.com/v1#fragment",
            "https://user@api.openai.com/v1",
            "https://api.openai.com:444/v1",
            "https://api.openai.com./v1",
        )
        for url in cases:
            with self.subTest(url=url):
                self._write(url)
                result = probe.classify_authoritative_provider_route(self.path)
                self.assertEqual(
                    result["classification"],
                    probe.OPENAI_COMPATIBLE_OTHER,
                )
                rendered = repr(result)
                self.assertNotIn(KEY, rendered)
                self.assertNotIn(url, rendered)
                self.assertNotIn(MODEL, rendered)

    def test_materialized_loop_config_remains_authoritative_over_env(self):
        self._write("https://provider.example.invalid/v1")
        env = {
            "LLM_API_BASE": "https://api.openai.com/v1",
            "LLM_API_KEY": "ENV-SECRET",
            "LLM_MODEL": MODEL,
        }
        result = probe.classify_authoritative_provider_route(
            self.path,
            environ=env,
        )
        self.assertEqual(
            result["classification"],
            probe.OPENAI_COMPATIBLE_OTHER,
        )
        rendered = repr(result)
        self.assertNotIn("ENV-SECRET", rendered)
        self.assertNotIn("provider.example.invalid", rendered)

    def test_env_backed_primary_route_is_supported_when_config_is_absent(self):
        env = {
            "LLM_API_BASE": "https://api.openai.com/v1",
            "LLM_API_KEY": "ENV-SECRET",
            "LLM_MODEL": MODEL,
        }
        result = probe.classify_authoritative_provider_route(
            self.path,
            environ=env,
        )
        self.assertEqual(result["classification"], probe.OFFICIAL_OPENAI)
        rendered = repr(result)
        self.assertNotIn("ENV-SECRET", rendered)
        self.assertNotIn("api.openai.com", rendered)
        self.assertNotIn(MODEL, rendered)

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
                probe.ProviderRouteClassificationProbeError
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

    def test_authority_failure_returns_only_fixed_unavailable_error(self):
        env = {
            "LLM_API_BASE": "https://api.openai.com/v1",
            "LLM_API_KEY": "",
            "LLM_MODEL": MODEL,
        }
        with self.assertRaises(probe.ProviderRouteClassificationProbeError) as raised:
            probe.classify_authoritative_provider_route(self.path, environ=env)
        self.assertEqual(raised.exception.category, probe.UNAVAILABLE)
        self.assertEqual(raised.exception.status_code, 503)
        rendered = repr(raised.exception)
        self.assertNotIn("api.openai.com", rendered)
        self.assertNotIn(MODEL, rendered)

    def test_p3_route_authenticates_before_body_read_and_classification(self):
        root = Path(__file__).resolve().parents[2]
        source = (root / "backend" / "p3_relay_app.py").read_text(encoding="utf-8")
        start = source.index('@app.post("/app/provider/route-classification-probe")')
        end = source.index(
            'relay_app._P3_PROVIDER_ROUTE_CLASSIFICATION_PROBE_INSTALLED = True',
            start,
        )
        block = source[start:end]
        auth_index = block.index("relay_app.check_auth(request)")
        body_index = block.index("await request.body()")
        classify_index = block.index("classify_authoritative_provider_route")
        self.assertLess(auth_index, body_index)
        self.assertLess(body_index, classify_index)
        for forbidden in (
            '"url"',
            '"key"',
            '"model"',
            '"host"',
            "LLM_API_BASE",
            "LLM_API_KEY",
        ):
            self.assertNotIn(forbidden, block)


if __name__ == "__main__":
    unittest.main()
