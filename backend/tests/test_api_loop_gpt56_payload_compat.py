from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class ApiLoopGpt56PayloadCompatTests(unittest.TestCase):
    def setUp(self):
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
            "LLM_MODEL": "legacy-model",
            "LLM_MAX_TOKENS": "2000",
            "LLM_TEMPERATURE": "0.7",
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
        self.messages = [{"role": "user", "content": "hello"}]

    def test_gpt56_nonstream_uses_completion_budget_and_omits_temperature(self):
        body = self.module._chat_completion_body(
            {"model": "gpt-5.6-sol"},
            self.messages,
            stream=False,
        )
        self.assertEqual(body, {
            "model": "gpt-5.6-sol",
            "messages": self.messages,
            "stream": False,
            "max_completion_tokens": 2000,
        })
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("temperature", body)

    def test_gpt56_stream_uses_same_compatibility_contract(self):
        body = self.module._chat_completion_body(
            {"model": "gpt-5.6-sol"},
            self.messages,
            stream=True,
        )
        self.assertEqual(body["stream"], True)
        self.assertEqual(body["max_completion_tokens"], 2000)
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("temperature", body)

    def test_gpt56_explicit_kelivo_budget_maps_without_temperature(self):
        body = self.module._chat_completion_body(
            {"model": "gpt-5.6-sol"},
            self.messages,
            stream=False,
            temperature=0.4,
            max_tokens=123,
        )
        self.assertEqual(body["max_completion_tokens"], 123)
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("temperature", body)

    def test_legacy_model_payload_is_byte_contract_equivalent_in_fields(self):
        body = self.module._chat_completion_body(
            {"model": "legacy-model"},
            self.messages,
            stream=False,
            temperature=0.4,
            max_tokens=123,
        )
        self.assertEqual(body, {
            "model": "legacy-model",
            "messages": self.messages,
            "temperature": 0.4,
            "max_tokens": 123,
            "stream": False,
        })
        self.assertNotIn("max_completion_tokens", body)

    def test_only_exact_gpt56_sol_target_gets_compatibility_payload(self):
        body = self.module._chat_completion_body(
            {"model": "gpt-5.6-sol-preview"},
            self.messages,
            stream=True,
        )
        self.assertEqual(body["temperature"], 0.7)
        self.assertEqual(body["max_tokens"], 2000)
        self.assertNotIn("max_completion_tokens", body)


if __name__ == "__main__":
    unittest.main()
