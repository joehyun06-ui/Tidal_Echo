from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import codex_live_qualification as harness


class FakeResponse:
    def __init__(self, payload: object):
        self.raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, maximum: int):
        return self.raw[:maximum]


class QueueOpener:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if not self.payloads:
            raise AssertionError("unexpected request")
        item = self.payloads.pop(0)
        if isinstance(item, BaseException):
            raise item
        return FakeResponse(item)


class CodexLiveQualificationHarnessTest(unittest.TestCase):
    def client(self, payloads):
        opener = QueueOpener(payloads)
        return (
            harness.RelayClient(
                "https://api.example.invalid/relay",
                "relay-secret",
                timeout_seconds=5,
                opener=opener,
            ),
            opener,
        )

    def test_default_plan_is_network_free_and_does_not_require_secret(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = harness.main([], environ={})
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["network"])
        self.assertGreaterEqual(len(payload["steps"]), 10)
        self.assertTrue(any("manually" in step for step in payload["steps"]))

    def test_base_url_accepts_https_prefix_and_local_http_only(self):
        self.assertEqual(
            harness.normalize_base_url("https://api.example.invalid/relay/"),
            "https://api.example.invalid/relay",
        )
        self.assertEqual(
            harness.normalize_base_url("http://127.0.0.1:3011"),
            "http://127.0.0.1:3011",
        )
        for value in (
            "http://api.example.invalid",
            "https://user:pass@example.invalid",
            "https://example.invalid/?secret=x",
            "https://example.invalid/a/../b",
            "https://example.invalid/a/%2e%2e/b",
            " https://example.invalid",
        ):
            with self.subTest(value=value), self.assertRaises(harness.QualificationError):
                harness.normalize_base_url(value)

    def test_default_redirect_handler_refuses_redirect_requests(self):
        handler = harness._RejectRedirectHandler()
        request = object()
        self.assertIsNone(
            handler.redirect_request(request, None, 302, "Found", {}, "https://other.invalid")
        )

    def test_secret_is_read_from_named_environment_only(self):
        env = {"RELAY_SECRET": "abcDEF123!"}
        self.assertEqual(harness.load_secret(env), "abcDEF123!")
        with self.assertRaisesRegex(harness.QualificationError, "qualification_secret_missing"):
            harness.load_secret({})
        with self.assertRaisesRegex(harness.QualificationError, "qualification_secret_env_invalid"):
            harness.load_secret(env, "bad-name")

    def test_provider_status_forces_api_authority(self):
        client, opener = self.client([{"connected": True, "generation_provider": "api", "account_type": "chatgpt"}])
        self.assertEqual(
            client.provider_status(),
            {"connected": True, "generation_provider": "api"},
        )
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "https://api.example.invalid/relay/provider/status")
        self.assertEqual(request.get_header("Authorization"), "Bearer relay-secret")
        self.assertEqual(timeout, 5)

        bad, _ = self.client([{"connected": True, "generation_provider": "codex"}])
        with self.assertRaisesRegex(harness.QualificationError, "qualification_generation_authority_changed"):
            bad.provider_status()

    def test_login_start_matches_p1_external_wire_and_rejects_credentialed_url(self):
        client, _ = self.client([{
            "verification_url": "https://example.invalid/device",
            "user_code": "ABCD-1234",
            "status": "pending",
        }])
        self.assertEqual(client.login_start()["user_code"], "ABCD-1234")

        bad, _ = self.client([{
            "verification_url": "https://user:pass@example.invalid/device",
            "user_code": "ABCD-1234",
            "status": "pending",
        }])
        with self.assertRaisesRegex(harness.QualificationError, "qualification_login_response_invalid"):
            bad.login_start()

    def test_account_check_requires_connected_and_usage(self):
        client, _ = self.client([
            {"connected": True, "generation_provider": "api"},
            {"lifetime_tokens": 12, "daily_usage_buckets": []},
        ])
        self.assertEqual(harness.account_check(client), {
            "connected": True,
            "generation_provider": "api",
            "usage_available": True,
        })

    def test_canary_create_status_and_retire_are_strongly_correlated(self):
        client, opener = self.client([
            {
                "ok": True,
                "provider": "codex",
                "created": {"api_session": "api-canary-1", "title": "trial"},
            },
            {
                "ok": True,
                "provider": "codex",
                "session": {
                    "api_session": "api-canary-1",
                    "status": "active",
                    "model": "gpt-test",
                    "model_provider": "chatgpt",
                    "reasoning_effort": "high",
                    "thread_bound": True,
                },
            },
            {
                "ok": True,
                "provider": "api",
                "retired": {"api_session": "api-canary-1", "status": "retired"},
            },
        ])
        created = client.create_canary("trial")
        self.assertEqual(created["api_session"], "api-canary-1")
        state = client.canary_status(created["api_session"])
        self.assertTrue(state.thread_bound)
        self.assertEqual(state.model_provider, "chatgpt")
        self.assertEqual(client.retire_canary(created["api_session"])["status"], "retired")
        self.assertEqual(opener.requests[2][0].method, "POST")

    def test_http_error_preserves_only_fixed_category(self):
        body = io.BytesIO(json.dumps({"detail": "codex_control_disabled"}).encode())
        error = urllib.error.HTTPError(
            "https://api.example.invalid/provider/status", 503, "err", {}, body,
        )
        client, _ = self.client([error])
        with self.assertRaises(harness.QualificationError) as raised:
            client.provider_status()
        self.assertEqual(raised.exception.category, "codex_control_disabled")
        self.assertEqual(raised.exception.status_code, 503)

        unsafe_body = io.BytesIO(json.dumps({"detail": "token stderr path"}).encode())
        unsafe = urllib.error.HTTPError(
            "https://api.example.invalid/provider/status", 502, "err", {}, unsafe_body,
        )
        client, _ = self.client([unsafe])
        with self.assertRaises(harness.QualificationError) as raised:
            client.provider_status()
        self.assertEqual(raised.exception.category, "qualification_remote_error")
        self.assertNotIn("token", repr(raised.exception))

    def test_wait_bound_requires_resolved_provider_after_first_thread(self):
        client, _ = self.client([{
            "ok": True,
            "provider": "codex",
            "session": {
                "api_session": "api-canary-1",
                "status": "active",
                "model": "gpt-test",
                "model_provider": "unresolved",
                "reasoning_effort": "high",
                "thread_bound": True,
            },
        }])
        with self.assertRaisesRegex(
            harness.QualificationError,
            "qualification_provider_unresolved_after_thread",
        ):
            harness.wait_thread_bound(client, "api-canary-1", timeout_seconds=1, sleeper=lambda _x: None)

    def test_receipt_contains_no_secret_or_thread_identifier_is_atomic_and_mode_600(self):
        client, _ = self.client([
            {"connected": True, "generation_provider": "api", "account_type": "chatgpt"},
            {"lifetime_tokens": 12},
            {
                "ok": True,
                "provider": "codex",
                "session": {
                    "api_session": "api-canary-1",
                    "status": "active",
                    "model": "gpt-test",
                    "model_provider": "chatgpt",
                    "reasoning_effort": "high",
                    "thread_bound": True,
                    "thread_id": "must-not-escape",
                    "persona_hash": "must-not-escape",
                },
            },
        ])
        receipt = harness.build_receipt(client, "api-canary-1")
        encoded = json.dumps(receipt)
        self.assertNotIn("relay-secret", encoded)
        self.assertNotIn("thread_id", encoded)
        self.assertNotIn("persona_hash", encoded)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "receipt.json"
            harness.write_receipt(path, receipt)
            self.assertEqual(harness.load_receipt(path), receipt)
            self.assertEqual(list(path.parent.glob(".receipt.json.*.tmp")), [])
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_verify_after_restart_requires_same_pinned_contract(self):
        receipt = {
            "version": 1,
            "account": {"connected": True, "generation_provider": "api", "usage_available": True},
            "canary": {
                "api_session": "api-canary-1",
                "status": "active",
                "model": "gpt-test",
                "model_provider": "chatgpt",
                "reasoning_effort": "high",
                "thread_bound": True,
            },
        }
        client, _ = self.client([
            {"connected": True, "generation_provider": "api"},
            {"ok": True},
            {
                "ok": True,
                "provider": "codex",
                "session": dict(receipt["canary"]),
            },
        ])
        result = harness.verify_after_restart(client, receipt)
        self.assertTrue(result["restart_persistence"])

        changed = dict(receipt)
        changed["canary"] = dict(receipt["canary"])
        changed["canary"]["model"] = "different-model"
        client, _ = self.client([
            {"connected": True, "generation_provider": "api"},
            {"ok": True},
            {
                "ok": True,
                "provider": "codex",
                "session": dict(receipt["canary"]),
            },
        ])
        with self.assertRaisesRegex(harness.QualificationError, "qualification_restart_contract_changed"):
            harness.verify_after_restart(client, changed)

    def test_rollback_check_accepts_expected_control_disabled_after_health(self):
        error_body = io.BytesIO(json.dumps({"detail": "codex_control_disabled"}).encode())
        disabled = urllib.error.HTTPError(
            "https://api.example.invalid/provider/status", 503, "err", {}, error_body,
        )
        client, _ = self.client([
            {"ok": True},
            {"ready": True},
            disabled,
        ])
        result = harness.rollback_check(client, expect_control_disabled=True)
        self.assertEqual(result, {
            "healthz": True,
            "readyz": True,
            "codex_control_disabled": True,
        })

    def test_main_live_command_does_not_echo_secret(self):
        opener = QueueOpener([{"connected": True, "generation_provider": "api"}])
        with mock.patch.object(harness, "_open_no_redirect", opener):
            output = io.StringIO()
            with redirect_stdout(output):
                code = harness.main(
                    ["--base-url", "https://api.example.invalid", "status"],
                    environ={"RELAY_SECRET": "super-secret-token"},
                )
        self.assertEqual(code, 0)
        self.assertNotIn("super-secret-token", output.getvalue())


if __name__ == "__main__":
    unittest.main()
