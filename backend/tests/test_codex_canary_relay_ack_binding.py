from __future__ import annotations

import unittest

from backend import codex_canary_relay_integration as integration


class CodexCanaryRelayAckBindingTest(unittest.TestCase):
    def test_wrong_generation_id_is_rejected_even_when_other_correlation_matches(self):
        msg = {
            "id": 41,
            "text": "hello",
            "meta": {"api_session": "api-canary"},
        }
        payload = {
            "ok": True,
            "queued": True,
            "provider": "codex",
            "generation_provider": "codex",
            "generation_id": "codex-gen-999",
            "api_session": "api-canary",
            "canonical_message_id": 41,
            "status": "queued",
        }
        with self.assertRaisesRegex(
            integration.CodexCanaryRelayIntegrationError,
            "loop_queued_ack_correlation_mismatch",
        ):
            integration._queued_ack(payload, msg=msg)

    def test_exact_generation_id_is_accepted(self):
        msg = {"id": 41, "text": "hello", "meta": {"api_session": "api-canary"}}
        payload = {
            "ok": True,
            "queued": True,
            "provider": "codex",
            "generation_provider": "codex",
            "generation_id": "codex-gen-41",
            "api_session": "api-canary",
            "canonical_message_id": 41,
            "status": "queued",
        }
        self.assertEqual(integration._queued_ack(payload, msg=msg), payload)


if __name__ == "__main__":
    unittest.main()
